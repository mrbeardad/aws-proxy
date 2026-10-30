import base64
import copy
import json
from pathlib import Path
import shutil
import re
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import deploy
import publisher
import server
from settings import load


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.base = load(Path(__file__).with_name('config') / 'deployment.example.json')
        self.state = {'bucket': 'example-only-bucket', 'prefix': 'a' * 64,
                      'uuid': '00000000-0000-4000-8000-000000000000', 'wsPath': '/ws/' + 'b' * 48,
                      'originSecret': 'c' * 64, 'proxyDomain': 'proxy.example.com', 'configDomain': 'config.example.com'}

    def test_other_region_and_custom_clash_preserve_user_settings(self):
        opts = self.cfg['discovery']
        opts['pop_prefixes'] = ['ICN', 'HKG']
        opts['hint_regions'] = ['ap-northeast-2', 'ap-east-1']
        self.cfg['clash'].update(provider_name='MyEdges', select_group='Manual', auto_group='Fastest')
        self.base['rules'] = ['GEOIP,LAN,DIRECT', 'MATCH,Manual']
        self.base['mixed-port'], self.base['ipv6'] = 12345, True
        self.base['proxies'] = [{'name': 'DirectServer', 'type': 'socks5', 'server': '203.0.113.10', 'port': 1080}]
        settings = {**self.state, 'discovery': opts, 'clash': self.cfg['clash']}
        cfg, nodes = publisher.render(self.base, settings, [{'ip': '1.1.1.1', 'pop': 'ICN1-P1'}])
        self.assertEqual(cfg['mixed-port'], 12345)
        self.assertTrue(cfg['ipv6'])
        self.assertEqual(cfg['rules'][1:], self.base['rules'])
        self.assertEqual(cfg['proxy-groups'][0]['proxies'], ['Fastest', 'DirectServer', 'DIRECT'])
        self.assertIn('MyEdges', cfg['proxy-providers'])
        self.assertEqual(nodes['proxies'][0]['servername'], self.state['proxyDomain'])
        self.assertTrue(publisher.allowed_pop('HKG1-P1', opts))
        self.assertFalse(publisher.allowed_pop('NRT1-P1', opts))

    def test_config_rejects_injection_and_unsafe_limits(self):
        changes = [('server', 'ssh_target', '-oProxyCommand=evil'),
                   ('server', 'origin_hostname', 'example.com; evil'),
                   ('project', None, '../other'), ('discovery', 'probe_budget', 1000),
                   ('clash', 'provider_name', '../escape')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'clash.example.json').write_text(json.dumps(self.base), encoding='utf-8')
            for section, key, value in changes:
                cfg = copy.deepcopy(self.cfg)
                if key:
                    cfg[section][key] = value
                else:
                    cfg[section] = value
                path = root / 'deployment.json'
                path.write_text(json.dumps(cfg), encoding='utf-8')
                with self.subTest(section=section, key=key), self.assertRaises(ValueError):
                    load(path)

    def test_hosting_private_s3_and_us_east_1_creation(self):
        for region in ('us-east-1', 'ap-northeast-2'):
            cfg, state = copy.deepcopy(self.cfg), copy.deepcopy(self.state)
            cfg['aws']['region'] = region
            s3, cf, save = Mock(), Mock(), Mock()
            cf.create_origin_access_control.return_value = {'OriginAccessControl': {'Id': 'oac-example'}}
            cf.create_distribution.return_value = {'Distribution': {'Id': 'distribution-example', 'DomainName': 'config.example.com'}}
            deploy.hosting(cfg, state, save, s3, cf)
            args = s3.create_bucket.call_args.kwargs
            self.assertEqual('CreateBucketConfiguration' in args, region != 'us-east-1')
            self.assertTrue(all(s3.put_public_access_block.call_args.kwargs['PublicAccessBlockConfiguration'].values()))
            policy = json.loads(s3.put_bucket_policy.call_args.kwargs['Policy'])
            self.assertEqual(policy['Statement'][0]['Principal'], {'Service': 'cloudfront.amazonaws.com'})
            self.assertIn('AWS:SourceArn', policy['Statement'][0]['Condition']['StringEquals'])
            s3.reset_mock()
            deploy.hosting(cfg, state, save, s3, cf)
            s3.create_bucket.assert_not_called()

    def test_proxy_tls_websocket_headers_and_no_cache(self):
        cf = Mock()
        cf.create_distribution.return_value = {'Distribution': {'Id': 'proxy-example', 'DomainName': 'proxy.example.com'}}
        with patch.object(deploy, 'run_server'):
            deploy.origin(self.cfg, self.state, Mock(), cf)
        d = cf.create_distribution.call_args.kwargs['DistributionConfig']
        source = d['Origins']['Items'][0]
        self.assertEqual(source['CustomOriginConfig']['OriginProtocolPolicy'], 'https-only')
        self.assertEqual(source['CustomHeaders']['Items'][0]['HeaderValue'], self.state['originSecret'])
        self.assertEqual(d['DefaultCacheBehavior']['MaxTTL'], 0)
        self.assertIn('Sec-WebSocket-Key', d['DefaultCacheBehavior']['ForwardedValues']['Headers']['Items'])

    def test_update_reuses_installed_key_and_permissions_are_two_objects(self):
        state = {**self.state, 'originReady': True, 'configDistribution': 'example-config', 'proxyDistribution': 'example-proxy',
                 'publisherUser': self.state['bucket'] + '-publisher', 'publisherAccessKeyId': 'example-key', 'publisherInstalled': True}
        iam, cf = Mock(), Mock()
        cf.get_distribution.return_value = {'Distribution': {'Status': 'Deployed', 'DistributionConfig': {'Enabled': True}}}
        with tempfile.TemporaryDirectory() as directory, patch.object(deploy, 'run_server') as run, patch('builtins.print'):
            deploy.publish(self.cfg, self.base, state, Mock(), Path(directory), iam, cf)
        iam.create_user.assert_not_called()
        iam.create_access_key.assert_not_called()
        policy = json.loads(iam.put_user_policy.call_args.kwargs['PolicyDocument'])
        self.assertEqual(policy['Statement'][0]['Action'], 's3:PutObject')
        self.assertEqual(len(policy['Statement'][0]['Resource']), 2)
        self.assertTrue(all('*' not in arn for arn in policy['Statement'][0]['Resource']))
        self.assertIn('test -s /opt/aws-proxy/publisher-credentials', run.call_args.args[1])

    def test_generated_bash_syntax_and_pinned_bootstrap(self):
        with self.assertRaises(ValueError):
            server.bootstrap(self.cfg)
        self.cfg['server'].update(xray_version='v1.2.3', xray_sha256='a' * 64)
        scripts = [server.bootstrap(self.cfg), server.origin(self.cfg, self.state), server.publisher(self.cfg, self.base, self.state)]
        self.assertIn('sha256sum --check --status', scripts[0])
        self.assertIn('renewal-hooks/deploy/', scripts[1])
        files = [base64.b64decode(x).decode() for x in re.findall(r'printf %s ([A-Za-z0-9+/=]+) \| base64', scripts[2])]
        self.assertTrue(any('--root /opt/aws-proxy' in content for content in files))
        if not shutil.which('bash'):
            self.skipTest('Bash is needed for generated shell syntax validation')
        for script in scripts:
            result = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_probe_requires_expected_body_and_configured_pop(self):
        options = self.cfg['discovery']
        def curl(command, **kwargs):
            self.assertNotIn('--insecure', command)
            self.assertIn('--resolve', command)
            Path(command[command.index('--dump-header') + 1]).write_text('x-amz-cf-pop: NRT1-P1\n', encoding='utf-8')
            Path(command[command.index('--output') + 1]).write_text(publisher.HEALTH_BODY, encoding='utf-8')
            return Mock(returncode=0, stdout='200')
        with patch.object(publisher.subprocess, 'run', side_effect=curl):
            self.assertEqual(publisher.probe('proxy.example.com', '1.1.1.1', options)['pop'], 'NRT1-P1')
            options['pop_prefixes'] = ['ICN']
            self.assertIsNone(publisher.probe('proxy.example.com', '1.1.1.1', options))


if __name__ == '__main__':
    unittest.main()
