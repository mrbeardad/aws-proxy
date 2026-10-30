"""Discover configured CloudFront candidates and publish validated JSON (valid YAML) to private S3."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
HEALTH_BODY = 'clash-cf-ready-v1'


def atomic_json(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_bytes((json.dumps(value, indent=2) + '\n').encode())
    tmp.replace(path)


def public_ipv4(value, options):
    try:
        ip = ipaddress.ip_address(value)
        return ip.version == 4 and ip.is_global and value not in options['excluded_ips']
    except ValueError:
        return False


def json_get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read(4 * 1024 * 1024))


def discover(host, server_ip, previous, options):
    seeds = options['seed_ips'] + options['preferred_ips']
    preferred = options['preferred_ips']
    candidates = set(seeds + [r['ip'] for r in previous])
    try:
        candidates.update(r[4][0] for r in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM))
    except OSError:
        pass
    hints = [server_ip + '/32']
    try:
        ranges = json_get('https://ip-ranges.amazonaws.com/ip-ranges.json')
        nets = sorted({p['ip_prefix'] for p in ranges['prefixes'] if p['region'] in options['hint_regions'] and p['service'] == 'EC2'})
        for prefix in nets[::max(1, len(nets) // 4)][:4]:
            hints.append(str(ipaddress.ip_network(prefix).network_address + 1) + '/32')
    except (OSError, ValueError, KeyError):
        pass
    for hint in [None] + hints:
        query = {'name': host, 'type': 'A'}
        if hint:
            query['edns_client_subnet'] = hint
        try:
            data = json_get('https://dns.google/resolve?' + urllib.parse.urlencode(query))
            candidates.update(a['data'] for a in data.get('Answer', []) if a.get('type') == 1)
        except (OSError, ValueError, KeyError):
            continue
    # Preserve known candidates when a DNS response would otherwise fill the probe budget.
    known = set(seeds + [r['ip'] for r in previous])
    return sorted((ip for ip in candidates if public_ipv4(ip, options)), key=lambda ip: (ip not in known, ip not in preferred, int(ipaddress.ip_address(ip))))[:options['probe_budget']]


def probe(host, ip, options):
    with tempfile.TemporaryDirectory() as td:
        header, body = Path(td) / 'headers', Path(td) / 'body'
        command = ['curl', '--noproxy', '*', '--silent', '--show-error', '--connect-timeout', '4', '--max-time', '5', '--retry', '1', '--retry-all-errors', '--retry-delay', '1', '--retry-max-time', '12', '--max-filesize', '2048',
                   '--resolve', f'{host}:443:{ip}', '--dump-header', str(header), '--output', str(body), '--write-out', '%{http_code}', f'https://{host}/health']
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            if result.returncode or result.stdout != '200' or body.read_text().strip() != HEALTH_BODY:
                return None
            pops = re.findall(r'^x-amz-cf-pop:\s*([A-Z0-9-]+)\s*$', header.read_text(), re.I | re.M)
            pop = pops[-1].upper() if pops else ''
            return {'ip': ip, 'pop': pop} if allowed_pop(pop, options) else None
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            return None


def choose(rows, options):
    limit = options['max_candidates']
    preferred = options['preferred_ips']
    eligible = {r['ip']: r for r in rows if r and public_ipv4(r['ip'], options) and allowed_pop(r['pop'], options)}
    ordered = sorted(eligible.values(), key=lambda r: (r['ip'] not in preferred, preferred.index(r['ip']) if r['ip'] in preferred else 99, r['pop'], int(ipaddress.ip_address(r['ip']))))
    by_pop = {}
    for row in ordered:
        by_pop.setdefault(row['pop'], []).append(row)
    # Balance the slots across observed POPs instead of filling from one POP first.
    return [group[i] for i in range(limit) for group in by_pop.values() if i < len(group)][:limit]


def node(row, settings):
    return {'name': f"CF-{row['pop']}-{row['ip']}", 'type': 'vless', 'server': row['ip'], 'port': 443,
            'uuid': settings['uuid'], 'network': 'ws', 'tls': True, 'udp': True, 'packet-encoding': 'xudp',
            'servername': settings['proxyDomain'], 'ws-opts': {'path': settings['wsPath'], 'headers': {'Host': settings['proxyDomain']}}}


def allowed_pop(pop, options):
    match = re.fullmatch(r'([A-Z]{3})\d+-[A-Z]\d+', pop)
    return bool(match and match[1] in options['pop_prefixes'])


def render(base, settings, rows):
    options, clash = settings['discovery'], settings['clash']
    if not 1 <= len(rows) <= options['max_candidates'] or len({r['ip'] for r in rows}) != len(rows):
        raise ValueError('Candidate pool must be nonempty, bounded and unique')
    if any(not public_ipv4(r['ip'], options) or not allowed_pop(r['pop'], options) for r in rows):
        raise ValueError('Invalid candidate IP or POP')
    config = json.loads(json.dumps(base))
    name, auto, select = clash['provider_name'], clash['auto_group'], clash['select_group']
    existing_names = [p['name'] for p in base.get('proxies', [])]
    if len(existing_names) != len(set(existing_names)) or set(existing_names) & {auto, select, 'DIRECT', 'REJECT'}:
        raise ValueError('Duplicate or reserved proxy name')
    if base.get('proxy-groups') or base.get('proxy-providers'):
        raise ValueError('Groups and proxy providers are generated; remove them from the base file')
    health = {'url': clash['health_url'], 'interval': clash['health_interval'],
              'timeout': clash['health_timeout_ms'], 'lazy': False, 'expected-status': clash['health_status']}
    config['proxy-providers'] = {name: {
        'type': 'http', 'url': f"https://{settings['configDomain']}/{settings['prefix']}/nodes.yaml",
        'path': f'./providers/{name}.yaml', 'interval': clash['provider_interval'], 'proxy': 'DIRECT',
        'size-limit': 262144, 'health-check': {'enable': True, **health}}}
    config['proxy-groups'] = [
        {'name': select, 'type': 'select', 'proxies': [auto, *existing_names, 'DIRECT'], 'use': [name]},
        {'name': auto, 'type': 'url-test', 'empty-fallback': 'REJECT', 'use': [name],
         'tolerance': clash['tolerance_ms'], **health}]
    config['rules'] = [f"DOMAIN,{settings['configDomain']},DIRECT"] + base['rules']
    if clash['subscription_dns']:
        config.setdefault('dns', {}).setdefault('nameserver-policy', {})[settings['configDomain']] = clash['subscription_dns']
    for provider in config.get('rule-providers', {}).values():
        provider.setdefault('proxy', auto)
    generated = {key: config.pop(key) for key in ('proxy-groups', 'proxy-providers')}
    config.setdefault('proxies', [])
    ordered = {}
    for key, value in config.items():
        ordered[key] = value
        if key == 'proxies':
            ordered.update(generated)
    return ordered, {'proxies': [node(r, settings) for r in rows]}


def publish(s3, settings, config, provider, root=ROOT):
    changed = []
    # Record each successful write separately so a partial failure retries only what remains.
    for name, value in [('nodes.yaml', provider), ('clash.yaml', config)]:
        body = (json.dumps(value, indent=2) + '\n').encode()
        path = root / name
        if path.exists() and path.read_bytes() == body:
            continue
        s3.put_object(Bucket=settings['bucket'], Key=f"{settings['prefix']}/{name}", Body=body, ContentType='application/yaml', CacheControl='public, max-age=300', ServerSideEncryption='AES256', Metadata={'sha256': hashlib.sha256(body).hexdigest()})
        atomic_json(path, value)
        changed.append(name)
    return changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--discover-only', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    root = args.root
    settings = json.loads((root / 'publisher.json').read_text(encoding='utf-8'))
    base = json.loads((root / 'base.json').read_text(encoding='utf-8'))
    previous_path = root / 'candidates.json'
    previous = json.loads(previous_path.read_text(encoding='utf-8')) if previous_path.exists() else []
    ips = discover(settings['proxyDomain'], settings['serverIp'], previous, settings['discovery'])
    with ThreadPoolExecutor(max_workers=4) as pool:
        selected = choose(list(pool.map(lambda ip: probe(settings['proxyDomain'], ip, settings['discovery']), ips)), settings['discovery'])
    if not selected:
        raise RuntimeError('No verified candidates; keeping the last published files')
    config, provider = render(base, settings, selected)
    if args.discover_only:
        print(json.dumps({'candidates': selected}))
        return
    import boto3
    s3 = boto3.client('s3', region_name=settings['region'])
    changed = publish(s3, settings, config, provider, root)
    atomic_json(previous_path, selected)
    status = {'last_success_utc': datetime.now(timezone.utc).isoformat(), 'uploaded_objects': changed, 'discovered_count': len(ips), 'published_count': len(selected), 'pops': sorted({r['pop'] for r in selected}), 'candidates': selected}
    atomic_json(root / 'status.json', status)
    print(json.dumps(status), flush=True)


if __name__ == '__main__':
    main()
