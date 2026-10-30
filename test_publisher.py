import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import publisher as p


class PublisherTests(unittest.TestCase):
    def config(self):
        return json.loads(Path(__file__).with_name('config').joinpath('deployment.example.json').read_text(encoding='utf-8'))

    def options(self):
        options = self.config()['discovery']
        options['preferred_ips'] = ['3.173.250.23']
        options['excluded_ips'] = ['13.225.163.125']
        return options

    def settings(self):
        return {'uuid': '11111111-1111-4111-8111-111111111111', 'proxyDomain': 'proxy.cloudfront.net', 'configDomain': 'config.cloudfront.net', 'prefix': 'test-only', 'wsPath': '/ws/test', 'bucket': 'test-only', 'serverIp': '203.0.113.10', 'region': 'ap-northeast-1', 'discovery': self.options(), 'clash': self.config()['clash']}

    def test_diversity_and_reject_invalid_addresses(self):
        rows = [{'ip': f'13.225.163.{i}', 'pop': 'NRT20-P7'} for i in range(1, 13)]
        rows += [{'ip': '3.173.250.23', 'pop': 'NRT20-P8'}, {'ip': '54.248.1.1', 'pop': 'NRT12-P1'}, {'ip': '54.248.1.2', 'pop': 'HND1-P1'}, {'ip': '127.0.0.1', 'pop': 'NRT20-P7'}, {'ip': '13.225.163.125', 'pop': 'NRT20-P7'}, {'ip': '54.248.1.3', 'pop': 'HKG1-P1'}]
        result = p.choose(rows, self.options())
        self.assertEqual(len(result), 8)
        self.assertEqual({r['pop'] for r in result}, {'NRT20-P7', 'NRT20-P8', 'NRT12-P1', 'HND1-P1'})
        self.assertNotIn('13.225.163.125', {r['ip'] for r in result})

    def test_config_preserves_rules_and_stays_static_when_candidates_change(self):
        base = json.loads(Path(__file__).with_name('config').joinpath('clash.example.json').read_text(encoding='utf-8'))
        before = json.dumps(base, sort_keys=True)
        cfg, nodes = p.render(base, self.settings(), [{'ip': '3.173.250.23', 'pop': 'NRT20-P8'}, {'ip': '54.248.1.1', 'pop': 'NRT12-P1'}])
        self.assertEqual(cfg['rules'][1:], base['rules'])
        self.assertEqual(json.dumps(base, sort_keys=True), before)
        self.assertEqual(cfg['proxy-providers']['EdgeCandidates']['proxy'], 'DIRECT')
        self.assertNotIn('proxies', cfg['proxy-groups'][1])
        self.assertEqual(cfg['proxy-groups'][1]['empty-fallback'], 'REJECT')
        self.assertEqual(cfg['proxies'], base['proxies'])
        other, _ = p.render(base, self.settings(), [{'ip': '13.225.163.201', 'pop': 'NRT20-P7'}])
        self.assertEqual(cfg, other)
        self.assertTrue(all(r['proxy'] == 'AUTO' for r in cfg['rule-providers'].values()))
        self.assertEqual(nodes['proxies'][1]['server'], '54.248.1.1')
        self.assertNotIn('flow', nodes['proxies'][0])

    def test_generated_sections_follow_proxies_and_preserve_other_key_order(self):
        base = json.loads(Path(__file__).with_name('config').joinpath('clash.example.json').read_text(encoding='utf-8'))
        for placeholders in (True, False):
            if not placeholders:
                base.pop('proxy-groups')
                base.pop('proxy-providers')
            with self.subTest(placeholders=placeholders):
                cfg, _ = p.render(base, self.settings(), [{'ip': '3.173.250.23', 'pop': 'NRT20-P8'}])
                keys = list(json.loads(json.dumps(cfg)))
                start = keys.index('proxies')
                self.assertEqual(keys[start:start + 3], ['proxies', 'proxy-groups', 'proxy-providers'])
                generated = {'proxy-groups', 'proxy-providers'}
                self.assertEqual([key for key in keys if key not in generated], [key for key in base if key not in generated])

    def test_failed_discovery_keeps_previous_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'publisher.json').write_text(json.dumps(self.settings()))
            (root / 'base.json').write_text(Path(__file__).with_name('config').joinpath('clash.example.json').read_text(encoding='utf-8'))
            (root / 'clash.yaml').write_text('last-good')
            with patch('sys.argv', ['publisher.py', '--root', str(root)]), patch.object(p, 'discover', return_value=[]), patch.object(p, 'publish') as publish:
                with self.assertRaisesRegex(RuntimeError, 'keeping the last published'):
                    p.main()
                publish.assert_not_called()
            self.assertEqual((root / 'clash.yaml').read_text(), 'last-good')

    def test_empty_provider_is_rejected(self):
        with self.assertRaises(ValueError):
            p.render({}, self.settings(), [])

    def test_partial_upload_failure_is_not_reported_as_success(self):
        class S3:
            calls = 0
            def put_object(self, **kwargs):
                self.calls += 1
                if self.calls == 2:
                    raise OSError('upload interrupted')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            s3 = S3()
            with self.assertRaises(OSError):
                p.publish(s3, self.settings(), {'config': 'validated'}, {'proxies': []}, root)
            self.assertTrue((root / 'nodes.yaml').exists())
            self.assertFalse((root / 'clash.yaml').exists())
            self.assertEqual(p.publish(s3, self.settings(), {'config': 'validated'}, {'proxies': []}, root), ['clash.yaml'])

    def test_uploads_only_changed_objects(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            s3 = Mock()
            config, nodes = {'config': 'stable'}, {'proxies': ['one']}
            self.assertEqual(p.publish(s3, self.settings(), config, nodes, root), ['nodes.yaml', 'clash.yaml'])
            s3.reset_mock()
            self.assertEqual(p.publish(s3, self.settings(), config, nodes, root), [])
            s3.put_object.assert_not_called()
            self.assertEqual(p.publish(s3, self.settings(), config, {'proxies': ['two']}, root), ['nodes.yaml'])
            self.assertEqual(s3.put_object.call_count, 1)

    def test_balances_multiple_addresses_per_pop(self):
        rows = [{'ip': f'13.225.163.{i}', 'pop': pop} for i, pop in enumerate(['NRT20-P2'] * 5 + ['NRT20-P7'] * 5 + ['NRT20-P8'] * 5, start=1)]
        selected = p.choose(rows, self.options())
        counts = [sum(r['pop'] == pop for r in selected) for pop in ['NRT20-P2', 'NRT20-P7', 'NRT20-P8']]
        self.assertLessEqual(max(counts) - min(counts), 1)

    def test_discovery_keeps_known_ips_during_dns_failure_or_large_answer(self):
        previous = [{'ip': '99.99.99.99', 'pop': 'NRT12-P1'}]
        with patch.object(p.socket, 'getaddrinfo', side_effect=OSError), patch.object(p, 'json_get', side_effect=OSError):
            ips = p.discover('test.cloudfront.net', '203.0.113.10', previous, self.options())
            self.assertIn('99.99.99.99', ips)
            self.assertTrue(set(self.options()['preferred_ips']).issubset(ips))
        answers = {'Answer': [{'type': 1, 'data': f'1.1.1.{i}'} for i in range(1, 101)]}
        with patch.object(p.socket, 'getaddrinfo', return_value=[]), patch.object(p, 'json_get', return_value=answers):
            ips = p.discover('test.cloudfront.net', '203.0.113.10', previous, self.options())
            self.assertEqual(len(ips), 64)
            self.assertIn('99.99.99.99', ips)


if __name__ == '__main__':
    unittest.main()
