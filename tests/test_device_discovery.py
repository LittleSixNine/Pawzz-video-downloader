import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from device_discovery import DiscoveryError, discover_device, ip_rows, normalize_mac
from direct_exporter import config, export_cycle, failure_hint
from web_settings import DEFAULTS, environment, validate

MAC = '02:11:22:33:44:55'
LOCAL = '192.168.1.10'
INTERFACES = [dict(ifname='eth0', addr_info=[dict(local=LOCAL, prefixlen=24)])]


def neighbor(ip, mac=MAC, state='REACHABLE'):
    return dict(dst=ip, lladdr=mac, state=[state])


class DeviceDiscoveryTests(unittest.TestCase):
    def discover(self, rows, interfaces=INTERFACES):
        with patch('device_discovery.ip_rows', side_effect=[interfaces, rows]), \
             patch('device_discovery.socket.socket') as socket, \
             patch('device_discovery.time.sleep'):
            found = discover_device(MAC, LOCAL)
            probe = socket.return_value.__enter__.return_value
            probe.bind.assert_called_once_with((LOCAL, 0))
            addresses = [call.args[1][0] for call in probe.sendto.call_args_list]
            self.assertEqual(len(addresses), 253)
            self.assertNotIn(LOCAL, addresses)
            return found

    def test_changed_ip_ignores_old_mapping_and_other_devices(self):
        rows = [neighbor('192.168.1.50', state='FAILED'),
                neighbor('192.168.1.51', state='STALE'),
                neighbor('192.168.1.52', '02:00:00:00:00:02'),
                neighbor('192.168.2.53'), neighbor(LOCAL),
                neighbor('192.168.1.255'), neighbor('192.168.1.77')]
        self.assertEqual(self.discover(rows), '192.168.1.77')

    def test_no_match_stale_only_and_ambiguous_mapping_fail_closed(self):
        for rows, reason in (([], 'device_not_found'),
                             ([neighbor('192.168.1.50', state='STALE')], 'device_not_found'),
                             ([neighbor('192.168.1.50'), neighbor('192.168.1.51')], 'device_ambiguous')):
            with self.subTest(rows=rows), self.assertRaisesRegex(DiscoveryError, '^' + reason + '$'):
                self.discover(rows)

    def test_wrong_local_address_or_large_subnet_never_probes(self):
        for rows in ([], [dict(ifname='eth0', addr_info=[dict(local=LOCAL, prefixlen=16)])]):
            with patch('device_discovery.ip_rows', return_value=rows), \
                 patch('device_discovery.socket.socket') as socket:
                with self.assertRaisesRegex(DiscoveryError, 'device_network_unsupported'):
                    discover_device(MAC, LOCAL)
                socket.assert_not_called()

    def test_missing_command_bad_json_and_timeout_hide_raw_errors(self):
        for problem in (FileNotFoundError('PRIVATE_PATH'),
                        subprocess.TimeoutExpired('ip', 3),
                        subprocess.CalledProcessError(1, 'ip', stderr='PRIVATE_DATA')):
            with patch('device_discovery.subprocess.run', side_effect=problem):
                with self.assertRaisesRegex(DiscoveryError, '^device_discovery_unavailable$'):
                    ip_rows('address', 'show')
        for value in ('not json', '{}', '[1]'):
            with patch('device_discovery.subprocess.run', return_value=Mock(stdout=value)):
                with self.assertRaises(DiscoveryError):
                    ip_rows('address', 'show')

    def test_mac_validation_and_old_settings_compatibility(self):
        self.assertEqual(normalize_mac(' 02-AB-22-33-44-55 '), '02:ab:22:33:44:55')
        for value in ('ff:ff:ff:ff:ff:ff', '01:11:22:33:44:55', '00:00:00:00:00:00', MAC + ';id', ''):
            with self.assertRaises(ValueError):
                normalize_mac(value)
        old = dict(DEFAULTS, device_ip='192.168.1.50', callback_ip=LOCAL, serial='TEST')
        del old['device_mac']
        self.assertEqual(validate(old)['device_mac'], '')
        settings = validate(dict(DEFAULTS, device_mac=MAC, callback_ip=LOCAL, serial='TEST'))
        cfg = config(environment(settings, {}, allow_for_validation=True))
        self.assertIsNone(cfg['device'])
        self.assertEqual(cfg['device_mac'], MAC)
        for changes in ({'device_mac': ''}, {'device_mac': 'broken'}, {'device_ip': '8.8.8.8'}):
            with self.assertRaises(ValueError):
                validate(dict(settings, **changes))

    def test_each_cycle_resolves_fresh_ip_without_changing_saved_config(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = dict(output=Path(folder), session=Path('unused'), serial='TEST', device=None,
                       device_mac=MAC, advertise=LOCAL, bind='0.0.0.0', port=10003, source='', start='')
            with patch('direct_exporter.read_session', return_value=('user', 'token')), \
                 patch('direct_exporter.discover_device', side_effect=['192.168.1.77', '192.168.1.88']) as discover, \
                 patch('direct_exporter.DeviceClient'), patch('direct_exporter.publish'), \
                 patch('direct_exporter.Receiver') as receiver, patch('direct_exporter.request'):
                receiver.return_value.__enter__.return_value.data = b'CATALOG\n'
                self.assertEqual(export_cycle(cfg), 0)
                self.assertEqual(export_cycle(cfg), 0)
                self.assertEqual([call.args[0] for call in receiver.call_args_list], ['192.168.1.77', '192.168.1.88'])
                self.assertEqual(discover.call_count, 2)
                self.assertIsNone(cfg['device'])
            with patch('direct_exporter.read_session', return_value=('user', 'token')), \
                 patch('direct_exporter.discover_device', side_effect=DiscoveryError('device_not_found')), \
                 patch('direct_exporter.DeviceClient') as device:
                with self.assertRaises(DiscoveryError) as caught:
                    export_cycle(cfg)
                device.assert_not_called()
                self.assertTrue(failure_hint(caught.exception).startswith('device_not_found:'))


if __name__ == '__main__':
    unittest.main()
