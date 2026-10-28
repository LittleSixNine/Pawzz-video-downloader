import json
from pathlib import Path
import tempfile
import time
from unittest.mock import Mock, patch
import unittest

from direct_exporter import export_cycle
from web_settings import Console, SETTING_GROUPS
import test_web_settings
from test_web_settings import VALID


class SettingsFlowTests(unittest.TestCase):
    setUp = test_web_settings.WebSettingsTests.setUp
    tearDown = test_web_settings.WebSettingsTests.tearDown
    request = test_web_settings.WebSettingsTests.request

    def body(self, group, **values):
        return {name: values.get(name, VALID[name]) for name in SETTING_GROUPS[group]}

    def test_missing_ffmpeg_blocks_repair_before_save_or_launch_but_allows_original_export(self):
        with patch('web_settings.ffmpeg_status', return_value=dict(available=False, source=None, version=None, message='unavailable')):
            code, payload = self.request('/api/settings/archive', self.body('archive', repair_video=True))
            self.assertEqual(code, 400)
            self.assertEqual(json.loads(payload)['field'], 'repair_video')
            self.assertFalse(self.console.settings['repair_video'])
            self.assertFalse(self.console.snapshot()['ffmpeg']['available'])
            self.assertEqual(self.request('/api/settings/archive', self.body('archive', repair_video=False))[0], 200)
            self.console.settings = dict(VALID, repair_video=True)
            with patch('web_settings.subprocess.Popen') as launch:
                with self.assertRaisesRegex(ValueError, 'FFmpeg'):
                    self.console.start()
                with self.assertRaisesRegex(ValueError, 'FFmpeg'):
                    self.console.automation({'enabled': True})
                launch.assert_not_called()

    def test_groups_save_independently_even_before_device_setup(self):
        archive = self.body('archive', interval='3h', start_date='2026-10-01')
        self.assertEqual(self.request('/api/settings/archive', archive)[0], 200)
        self.assertFalse(self.console.snapshot()['configured'])
        self.assertIsNone(self.console.next_run)
        self.console.close()
        self.console = Console(self.root / 'config', self.env)
        self.assertIsNone(self.console.problem)
        self.assertEqual(self.console.settings['interval'], '3h')
        self.assertFalse(self.console.snapshot()['configured'])
        self.console.save_group('device', self.body('device'))
        self.assertTrue(self.console.snapshot()['configured'])
        self.assertEqual(self.console.settings['start_date'], '2026-10-01')
        self.assertEqual(self.console.settings['interval'], '3h')

    def test_groups_cannot_change_automation_or_other_group_fields(self):
        self.assertEqual(self.request('/api/settings/device', self.body('device'), auth=False)[0], 401)
        self.assertEqual(self.request('/api/settings/archive', self.body('archive'), origin='http://evil.example')[0], 403)
        for group, body in (('device', dict(self.body('device'), automatic=True)),
                            ('archive', dict(self.body('archive'), device_ip='192.168.1.99')),
                            ('other', {})):
            self.assertEqual(self.request('/api/settings/' + group, body)[0], 400)
        self.assertFalse(self.console.settings['automatic'])

    def test_field_errors_are_specific_and_do_not_save_invalid_values(self):
        for field, value in (('device_mac', 'bad'), ('device_ip', '8.8.8.8'),
                             ('callback_ip', 'bad'), ('callback_port', 8080), ('serial', 'bad;id')):
            code, payload = self.request('/api/settings/device', self.body('device', **{field:value}))
            self.assertEqual(code, 400)
            self.assertEqual(json.loads(payload)['field'], field)
        code, payload = self.request('/api/settings/archive', self.body('archive', start_date='2026-02-30'))
        self.assertEqual(code, 400)
        self.assertEqual(json.loads(payload)['field'], 'start_date')
        self.assertFalse((self.root / 'config/settings.json').exists())

    def test_saving_never_triggers_immediate_run_and_preserves_existing_deadline(self):
        self.session.write_text('{}')
        self.console.save(dict(VALID, automatic=True))
        self.assertGreater(self.console.next_run - time.time(), 86390)
        deadline = self.console.next_run
        self.console.save_group('device', self.body('device', device_ip='192.168.1.51'))
        self.assertEqual(self.console.next_run, deadline)
        self.console.save_group('archive', self.body('archive', interval='3h'))
        self.assertGreater(self.console.next_run - time.time(), 10790)
        self.assertEqual(self.console.history, [])

    def test_explicit_enable_is_idempotent_and_pause_does_not_stop_current_task(self):
        self.session.write_text('{}'); self.console.save(VALID)
        with self.console.lock:
            self.assertEqual(self.request('/api/automation', {'enabled':True}, auth=False)[0], 401)
            self.console.automation({'enabled':True})
            deadline = self.console.next_run
            self.assertLess(abs(deadline - time.time()), 1)
            self.console.automation({'enabled':True})
            self.assertEqual(self.console.next_run, deadline)
            process = Mock()
            self.console.process = process
            try:
                self.console.automation({'enabled':False})
                process.terminate.assert_not_called()
                self.assertFalse(self.console.settings['automatic'])
                self.assertIsNone(self.console.next_run)
                self.console.automation({'enabled':True})
                self.assertIsNone(self.console.next_run)
                self.console.stop()
                process.terminate.assert_called_once()
                self.assertTrue(self.console.settings['automatic'])
                self.console.schedule_next()
                self.assertGreater(self.console.next_run - time.time(), 86390)
            finally:
                self.console.process = None
                self.console.automation({'enabled':False})

    def test_save_during_run_rejected_but_pause_and_logout_still_disable_plan(self):
        self.session.write_text('{}'); self.console.save(VALID)
        self.console.process = Mock()
        try:
            self.assertEqual(self.request('/api/settings/archive', self.body('archive'))[0], 409)
            self.console.settings['automatic'] = True
            self.assertEqual(self.request('/api/automation', {'enabled':False})[0], 200)
            self.console.process.terminate.assert_not_called()
            self.console.settings['automatic'] = True
            self.console.account_action('logout', {})
            self.assertFalse(self.console.settings['automatic'])
            self.console.process.terminate.assert_called_once()
        finally:
            self.console.process = None


class AddressPriorityTests(unittest.TestCase):
    def cycle(self, callback, lookup):
        with tempfile.TemporaryDirectory() as directory:
            cfg=dict(output=Path(directory),session=Path('unused'),serial='DEMO',source='',start='',
                     device='192.168.1.50',device_mac='02:11:22:33:44:55',advertise='192.168.1.10',bind='0.0.0.0',port=10003)
            with patch('direct_exporter.read_session', return_value=('demo','demo')), \
                 patch('direct_exporter.DeviceClient') as device, patch('direct_exporter.publish'), \
                 patch('direct_exporter.Receiver') as receiver, patch('direct_exporter.request',side_effect=callback), \
                 patch('direct_exporter.discover_device',side_effect=lookup) as discover, patch('direct_exporter.time.sleep'):
                receiver.return_value.__enter__.return_value.data=b'CATALOG\n'
                self.assertEqual(export_cycle(cfg), 0)
                self.assertEqual(cfg['device'],'192.168.1.50')
                self.assertEqual(device.call_count,1)
                return receiver.call_args_list, discover.call_count

    def test_ip_success_never_uses_mac_discovery(self):
        calls, count=self.cycle(lambda *args:None, AssertionError('MAC must remain fallback'))
        self.assertEqual([call.args[0] for call in calls],['192.168.1.50'])
        self.assertEqual(count,0)

    def test_timeout_falls_back_to_new_mac_address_in_same_connection(self):
        attempts=[]
        def callback(_device,cfg,_receiver,_operation):
            attempts.append(cfg['device'])
            if len(attempts)==1: raise TimeoutError()
        calls,count=self.cycle(callback,lambda *args:'192.168.1.77')
        self.assertEqual(attempts,['192.168.1.50','192.168.1.77'])
        self.assertEqual([call.args[0] for call in calls],attempts)
        self.assertEqual(count,1)


if __name__ == '__main__':
    unittest.main()
