"""Load local deployment inputs; keep all environment-specific values in JSON."""
import ipaddress
import json
from pathlib import Path
import re


def load(path):
    path = Path(path).resolve()
    cfg = json.loads(path.read_text(encoding='utf-8'))
    def require(condition, message):
        if not condition:
            raise ValueError(message)
    require(re.fullmatch(r'[a-z][a-z0-9-]{2,23}', cfg['project']), 'Invalid project name')
    aws, server, discovery, clash = (cfg[k] for k in ('aws', 'server', 'discovery', 'clash'))
    require(re.fullmatch(r'\d{12}', aws['account_id']), 'AWS account_id must have 12 digits')
    require(re.fullmatch(r'(us|eu|ap|ca|sa|af|me|il|mx)-[a-z]+-\d', aws['region']), 'Use a standard commercial AWS region')
    require(aws['price_class'] in ('PriceClass_All', 'PriceClass_200', 'PriceClass_100'), 'Invalid price_class')
    require(ipaddress.ip_address(server['public_ip']).version == 4, 'Origin requires an IPv4 address')
    require(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9@._-]*', server['ssh_target']), 'Use an SSH alias or user@host, without flags')
    require(re.fullmatch(r'(?=.{1,253}$)[a-zA-Z0-9]+(?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?', server['origin_hostname']), 'Invalid origin hostname')
    require('.' in server['origin_hostname'], 'Origin hostname must be a DNS name')
    require(re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', server['certificate_email']), 'Invalid certificate_email')
    for key in ('https_port', 'ws_port'):
        require(type(server[key]) is int and 1024 <= server[key] <= 65535, f'Invalid {key}')
    require(server['https_port'] != server['ws_port'], 'Origin and WebSocket ports must differ')
    require(discovery['pop_prefixes'] and all(re.fullmatch(r'[A-Z]{3}', x) for x in discovery['pop_prefixes']), 'Specify allowed POP prefixes')
    require(type(discovery['max_candidates']) is int and 1 <= discovery['max_candidates'] <= 32, 'max_candidates must be 1..32')
    require(type(discovery['probe_budget']) is int and discovery['max_candidates'] <= discovery['probe_budget'] <= 64, 'probe_budget must be between max_candidates and 64')
    for key in ('seed_ips', 'preferred_ips', 'excluded_ips'):
        for value in discovery[key]:
            address = ipaddress.ip_address(value)
            require(address.version == 4 and address.is_global, f'{key} requires public IPv4 addresses')
    for key in ('provider_name', 'auto_group', 'select_group'):
        require(re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,40}', clash[key]), f'Invalid {key}')
    require(len({clash[k] for k in ('provider_name', 'auto_group', 'select_group')}) == 3, 'Provider and group names must differ')
    require(not {clash['auto_group'], clash['select_group']} & {'DIRECT', 'REJECT'}, 'Reserved group name')
    for key in ('provider_interval', 'health_interval', 'health_timeout_ms'):
        require(type(clash[key]) is int and clash[key] > 0, f'{key} must be positive')
    require(type(clash['tolerance_ms']) is int and clash['tolerance_ms'] >= 0, 'tolerance_ms must be nonnegative')
    require(type(clash['health_status']) is int and 100 <= clash['health_status'] <= 599, 'Invalid health_status')
    require(clash['health_url'].startswith('https://') and '\n' not in clash['health_url'], 'Use an HTTPS health URL')
    require(isinstance(clash['subscription_dns'], list) and all(isinstance(x, str) for x in clash['subscription_dns']), 'subscription_dns must be a list')
    base = json.loads((path.parent / clash['base_file']).read_text(encoding='utf-8'))
    require(isinstance(base.get('rules'), list) and all(isinstance(x, str) for x in base['rules']), 'Clash base requires a rules list')
    require(not base.get('proxy-groups') and not base.get('proxy-providers'), 'Groups and providers are generated; omit them from the base')
    return cfg, base
