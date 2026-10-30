"""Generate scripts for an existing Ubuntu server. No AWS calls in this module."""
import base64
import json
from pathlib import Path
import re
import shlex

from publisher import HEALTH_BODY


def put(path, content, mode='600'):
    if isinstance(content, dict):
        content = json.dumps(content, indent=2) + '\n'
    encoded = base64.b64encode(content.encode()).decode()
    return f'printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}\nchmod {mode} {shlex.quote(path)}\n'


def bootstrap(cfg):
    s = cfg['server']
    if not re.fullmatch(r'v[0-9][0-9.]+', s['xray_version']) or not re.fullmatch(r'[a-fA-F0-9]{64}', s['xray_sha256']):
        raise ValueError('Set the exact Xray release tag and independently verified SHA256 before bootstrap')
    if s['xray_archive'] not in ('Xray-linux-64.zip', 'Xray-linux-arm64-v8a.zip'):
        raise ValueError('Choose the x86_64 or ARM64 Linux archive')
    architecture = 'x86_64' if s['xray_archive'] == 'Xray-linux-64.zip' else 'aarch64'
    url = f"https://github.com/XTLS/Xray-core/releases/download/{s['xray_version']}/{s['xray_archive']}"
    # Do not replace a binary another deployment may already be using.
    return f'''set -euo pipefail
test "$(uname -m)" = {architecture}
if test -e /usr/local/bin/xray; then echo 'Xray already exists; inspect it and skip bootstrap or upgrade it explicitly.' >&2; exit 1; fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx certbot python3-boto3 curl unzip ca-certificates
id xray >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin xray
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
curl --fail --location --proto '=https' --tlsv1.2 --max-time 180 {shlex.quote(url)} -o "$tmp/xray.zip"
printf '%s  %s\\n' {s['xray_sha256']} "$tmp/xray.zip" | sha256sum --check --status
unzip -q "$tmp/xray.zip" -d "$tmp/xray"
install -m 755 "$tmp/xray/xray" /usr/local/bin/xray
/usr/local/bin/xray version
systemctl enable --now nginx certbot.timer
'''


def origin(cfg, state):
    project, s = cfg['project'], cfg['server']
    root = '/opt/' + project
    config = {'log': {'loglevel': 'warning'}, 'inbounds': [{
        'listen': '127.0.0.1', 'port': s['ws_port'], 'protocol': 'vless',
        'settings': {'clients': [{'id': state['uuid']}], 'decryption': 'none'},
        'streamSettings': {'network': 'ws', 'security': 'none', 'wsSettings': {'path': state['wsPath']}}}],
        'outbounds': [{'tag': 'internet', 'protocol': 'freedom', 'streamSettings': {'sockopt': {'domainStrategy': 'UseIPv4'}}}, {'tag': 'blocked', 'protocol': 'blackhole'}],
        'routing': {'domainStrategy': 'IPIfNonMatch', 'rules': [{'type': 'field', 'ip': [
            '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16',
            '172.16.0.0/12', '192.168.0.0/16', '224.0.0.0/4', '240.0.0.0/4', '::1/128', 'fc00::/7', 'fe80::/10'], 'outboundTag': 'blocked'}]}}
    acme = f'''server {{
 listen 80;
 server_name {s['origin_hostname']};
 access_log off;
 location ^~ /.well-known/acme-challenge/ {{ root /var/www/{project}-acme; }}
 location / {{ return 404; }}
}}
'''
    nginx = f'''server {{
 listen {s['https_port']} ssl;
 server_name {s['origin_hostname']};
 server_tokens off;
 ssl_certificate /etc/letsencrypt/live/{project}/fullchain.pem;
 ssl_certificate_key /etc/letsencrypt/live/{project}/privkey.pem;
 ssl_protocols TLSv1.2 TLSv1.3;
 access_log off;
 error_log /var/log/nginx/{project}-error.log crit;
 if ($http_x_origin_token != "{state['originSecret']}") {{ return 403; }}
 location = {state['wsPath']} {{
  proxy_pass http://127.0.0.1:{s['ws_port']};
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_set_header Host $host;
  proxy_read_timeout 300s;
  proxy_send_timeout 300s;
  proxy_buffering off;
 }}
 location = /health {{ default_type text/plain; add_header Cache-Control "no-store" always; return 200 "{HEALTH_BODY}\\n"; }}
 location / {{ return 404; }}
}}
'''
    unit = f'''[Unit]
Description=CloudFront VLESS WebSocket origin
After=network-online.target
Wants=network-online.target
[Service]
User=xray
Group=xray
ExecStart=/usr/local/bin/xray run -config {root}/server.json
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
LimitNOFILE=65536
[Install]
WantedBy=multi-user.target
'''
    script = f'''set -euo pipefail
umask 077
command -v certbot >/dev/null
id xray >/dev/null
install -d -m 750 -o root -g xray {root}
install -d -m 755 /var/www/{project}-acme
'''
    script += put(root + '/server.json.new', config, '640')
    script += f'''chown root:xray {root}/server.json.new
/usr/local/bin/xray run -test -config {root}/server.json.new
if test -f {root}/server.json; then cp -p {root}/server.json {root}/server.json.previous; fi
mv {root}/server.json.new {root}/server.json
'''
    script += put(f'/etc/nginx/conf.d/{project}-acme.conf', acme)
    script += f'''nginx -t
systemctl reload nginx
certbot certonly --non-interactive --agree-tos --email {shlex.quote(s['certificate_email'])} --webroot -w /var/www/{project}-acme -d {s['origin_hostname']} --cert-name {project}
'''
    script += put(f'/etc/letsencrypt/renewal-hooks/deploy/{project}-nginx', '#!/bin/sh\nnginx -t && systemctl reload nginx\n', '755')
    script += put(f'/etc/nginx/conf.d/{project}.conf', nginx)
    script += put(f'/etc/systemd/system/{project}.service', unit, '644')
    script += f'''nginx -t
systemctl daemon-reload
systemctl enable {project}.service
systemctl restart {project}.service
systemctl reload nginx
systemctl is-active {project}.service nginx
curl --fail --silent --show-error --retry 3 --retry-all-errors --max-time 10 --resolve {s['origin_hostname']}:{s['https_port']}:127.0.0.1 -H 'X-Origin-Token: {state['originSecret']}' https://{s['origin_hostname']}:{s['https_port']}/health
'''
    return script


def publisher(cfg, base, state, credentials=None):
    project = cfg['project']
    root = '/opt/' + project
    settings = {k: state[k] for k in ('bucket', 'prefix', 'uuid', 'wsPath', 'proxyDomain', 'configDomain')}
    settings.update(serverIp=cfg['server']['public_ip'], region=cfg['aws']['region'], discovery=cfg['discovery'], clash=cfg['clash'])
    unit = f'''[Unit]
Description=Publish verified CloudFront candidate pool
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
User=root
UMask=0077
Environment=AWS_SHARED_CREDENTIALS_FILE={root}/publisher-credentials
Environment=AWS_EC2_METADATA_DISABLED=true
ExecStart=/usr/bin/flock -n {root}/publisher.lock /usr/bin/python3 {root}/publisher.py --root {root}
TimeoutStartSec=420
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={root}
'''
    timer = '''[Unit]
Description=Hourly candidate discovery
[Timer]
OnCalendar=hourly
RandomizedDelaySec=60
Persistent=true
[Install]
WantedBy=timers.target
'''
    script = f'set -euo pipefail\numask 077\npython3 -c "import boto3"\nsystemctl stop {project}-publish.timer 2>/dev/null || true\nsystemctl stop {project}-publish.service 2>/dev/null || true\n'
    if credentials:
        script += put(root + '/publisher-credentials', credentials)
    script += f'test -s {root}/publisher-credentials\n'
    script += put(root + '/publisher.json', settings)
    script += put(root + '/base.json', base)
    script += put(root + '/publisher.py', Path(__file__).with_name('publisher.py').read_text(encoding='utf-8'))
    script += put(f'/etc/systemd/system/{project}-publish.service', unit, '644')
    script += put(f'/etc/systemd/system/{project}-publish.timer', timer, '644')
    script += f'''systemctl daemon-reload
systemctl enable --now {project}-publish.timer
systemctl start {project}-publish.service
cat {root}/status.json
'''
    return script
