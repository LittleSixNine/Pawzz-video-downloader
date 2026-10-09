import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
from http.server import ThreadingHTTPServer

import direct_exporter
import web_settings
from web_settings import Console, DEFAULTS, environment, handler_for, validate, RETRY_DELAY, MAX_RETRIES, make_server

VALID = dict(DEFAULTS, device_ip='192.168.1.50', callback_ip='192.168.1.10',
             serial='TEST_SERIAL', allow_maintenance=True)


class WebSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = self.root / 'session.json'
        self.env = dict(os.environ, PAWZZ_SESSION_KEY_FILE=str(self.root / 'keys/session.key'), PAWZZ_SESSION_FILE=str(self.session),
                        PAWZZ_OUTPUT_DIR=str(self.root / 'videos'), PAWZZ_MODE=getattr(self, 'mode', 'server'))
        (self.root / 'config').mkdir()
        (self.root / 'config/admin-token').write_text('SYNTHETIC_MANAGEMENT_PASSWORD_FOR_TEST')
        self.console = Console(self.root / 'config', self.env)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.console))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.http = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.console.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, body=None, auth=True, origin=None):
        headers = {'Content-Type': 'application/json'}
        if auth:
            headers['Authorization'] = 'Bearer ' + self.console.token
        if origin:
            headers['Origin'] = origin
        request = Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with self.http.open(request, timeout=5) as response:
                return response.status, response.read()
        except HTTPError as error:
            with error:
                return error.code, error.read()

    def test_auth_origin_and_static_files(self):
        self.assertEqual(self.request('/api/status', auth=False)[0], 401)
        self.assertEqual(self.request('/api/settings', VALID, origin='http://evil.example')[0], 403)
        self.assertEqual(self.request('/api/settings', VALID, origin=self.base)[0], 200)
        for path in ('/', '/settings.js', '/settings.css'):
            code, data = self.request(path, auth=False)
            self.assertEqual(code, 200)
            self.assertNotIn(self.console.token.encode(), data)
        self.assertEqual(self.request('/../config/admin-token')[0], 404)

    def test_validation_and_private_persistence(self):
        for update in ({'device_ip': '8.8.8.8'}, {'source': '/tmp/x;id'},
                       {'callback_port': 8080}, {'callback_port': -1},
                       {'start_date': '2026-02-30'}, {'automatic': True, 'allow_maintenance': False},
                       {'serial': 'x\nPAWZZ_OUTPUT_DIR=/etc'}):
            self.assertEqual(self.request('/api/settings', dict(VALID, **update))[0], 400)
        self.assertEqual(self.request('/api/settings', VALID)[0], 200)
        path = self.root / 'config' / 'settings.json'
        self.assertEqual(json.loads(path.read_text()), VALID)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((path.parent / 'admin-token').stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.console.snapshot()['running'])
        self.assertEqual(self.request('/api/run', {})[0], 400)
        self.console.close()
        self.console = Console(path.parent, self.env)
        self.assertEqual(self.console.settings, VALID)
        self.assertIsNone(self.console.process)

    def test_actual_worker_failure_keeps_web_server_alive(self):
        # Malformed synthetic session fails before any MQTT or device connection.
        self.session.write_text('{"token":"SYNTHETIC_DO_NOT_LOG"}')
        self.console.save(VALID)
        self.assertEqual(self.request('/api/run', {})[0], 200)
        deadline = time.monotonic() + 8
        while self.console.snapshot()['running'] and time.monotonic() < deadline:
            time.sleep(.05)
        code, payload = self.request('/api/status')
        status = json.loads(payload)
        self.assertEqual(code, 200)
        self.assertFalse(status['running'])
        self.assertEqual(status['last_exit'], 1)
        self.assertNotIn(b'SYNTHETIC_DO_NOT_LOG', payload)
        self.assertIn('配置或校验', status['last_result'])

    def test_stop_disables_automatic_and_rejects_parallel_job(self):
        import subprocess
        import sys
        # Substitute a harmless sleeping child, not a device-connected worker.
        self.session.write_text('{}')
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        with patch('web_settings.subprocess.Popen', return_value=child):
            self.console.save(dict(VALID, automatic=True))
            self.console.start()
            with self.assertRaises(RuntimeError):
                self.console.start()
            with self.assertRaises(RuntimeError):
                self.console.save(VALID)
            self.console.stop()
            self.console.monitor.join(timeout=5)
        self.assertIsNone(self.console.process)
        self.assertFalse(self.console.settings['automatic'])
        self.assertIsNone(self.console.next_run)

    def test_history_survives_restart_without_private_worker_output(self):
        self.test_actual_worker_failure_keeps_web_server_alive()
        history_path = self.root / 'config/history.json'
        records = json.loads(history_path.read_text())
        self.assertEqual(records[0]['state'], 'failed')
        self.assertEqual(records[0]['exit_code'], 1)
        self.assertNotIn('SYNTHETIC_DO_NOT_LOG', history_path.read_text())
        self.assertEqual(history_path.stat().st_mode & 0o777, 0o600)
        self.console.close()
        self.console = Console(history_path.parent, self.env)
        snapshot = self.console.snapshot()
        self.assertEqual(snapshot['last_exit'], 1)
        self.assertEqual(snapshot['history'][0]['id'], records[0]['id'])
        self.assertFalse(snapshot['running'])

    def test_interrupted_history_is_not_reported_as_success(self):
        self.console.close()
        record = dict(id='interrupted-test', started_at=100, state='running',
                      new_files=3, result='正在执行', finished_at=None, exit_code=None)
        path = self.root / 'config/history.json'
        path.write_text(json.dumps([record]))
        self.console = Console(path.parent, self.env)
        restored = self.console.snapshot()['history'][0]
        self.assertEqual(restored['state'], 'interrupted')
        self.assertIsNone(restored['new_files'])
        self.assertIsNone(restored['exit_code'])
        self.assertIn('未确认', self.console.last_result)

    def test_corrupt_history_is_preserved_and_blocks_new_jobs(self):
        self.console.close()
        path = self.root / 'config/history.json'
        original = '{broken history'
        path.write_text(original)
        self.console = Console(path.parent, self.env)
        self.session.write_text('{}')
        self.console.save(VALID)
        with self.assertRaises(ValueError):
            self.console.start()
        self.assertFalse(self.console.snapshot()['history_valid'])
        self.assertEqual(path.read_text(), original)

    def test_history_limit_and_success_count_from_actual_child(self):
        import subprocess
        import sys
        self.console.history = [dict(id=str(i), started_at=i, state='success',
                                     result='本轮完成', new_files=0) for i in range(100)]
        self.session.write_text('{}')
        self.console.save(VALID)
        child = subprocess.Popen([sys.executable, '-c',
                                  'print("verified original: PRIVATE_FILENAME"); print("cycle complete: 1 original files")'],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        with patch('web_settings.subprocess.Popen', return_value=child):
            self.console.start()
        self.console.monitor.join(timeout=5)
        records = self.console.snapshot()['history']
        self.assertEqual(len(records), 100)
        self.assertEqual(records[0]['new_files'], 1)
        self.assertEqual(records[0]['state'], 'success')
        self.assertNotIn('PRIVATE_FILENAME', json.dumps(records))

    def test_storage_and_readiness_are_local_observations(self):
        snapshot = self.console.snapshot()
        self.assertFalse(snapshot['configured'])
        self.assertFalse(snapshot['storage']['available'])
        (self.root / 'videos').mkdir()
        self.console.save(VALID)
        snapshot = self.console.snapshot()
        self.assertTrue(snapshot['configured'])
        self.assertTrue(snapshot['storage']['available'])
        self.assertGreater(snapshot['storage']['total'], 0)
        self.assertNotIn('device_online', snapshot)

    def test_retry_budget_success_reset_and_manual_failure(self):
        self.console.settings = dict(VALID, automatic=True)
        with patch('web_settings.time.time', return_value=1000):
            for attempt in range(1, MAX_RETRIES + 1):
                self.console.schedule_next(failed=True)
                self.assertEqual(self.console.retry_count, attempt)
                self.assertEqual(self.console.next_kind, 'retry')
                self.assertEqual(self.console.next_run, 1000 + RETRY_DELAY)
            self.console.schedule_next(failed=True)
            self.assertEqual(self.console.next_run, 1000 + 86400)
            self.assertEqual(self.console.next_kind, 'scheduled')
            self.assertEqual(self.console.retry_count, 0)
            self.console.schedule_next(failed=True)
            self.console.schedule_next()
            self.assertEqual(self.console.retry_count, 0)
            self.console.settings['automatic'] = False
            self.console.schedule_next(failed=True)
            self.assertIsNone(self.console.next_run)

    def test_low_storage_blocks_launch_then_recovers_without_losing_settings(self):
        self.session.write_text('{}')
        self.console.settings = dict(VALID, automatic=True)
        with patch('direct_exporter.shutil.disk_usage', return_value=SimpleNamespace(free=1)), \
             patch('web_settings.subprocess.Popen') as spawn:
            self.assertEqual(self.request('/api/run', {})[0], 409)
            spawn.assert_not_called()
        self.assertEqual(self.console.next_kind, 'storage')
        self.assertEqual(self.console.retry_count, 0)
        self.assertTrue(self.console.settings['automatic'])
        self.assertEqual(self.console.history, [])
        import subprocess
        import sys
        child = subprocess.Popen([sys.executable, '-c', 'print("cycle complete: 0 original files")'],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        with patch('web_settings.subprocess.Popen', return_value=child):
            self.console.start(trigger='automatic')
        self.console.monitor.join(timeout=5)
        self.assertEqual(self.console.last_exit, 0)
        self.assertIsNone(self.console.problem)
        self.assertEqual(self.console.next_kind, 'scheduled')

    def test_failed_and_space_paused_workers_choose_different_schedules(self):
        self.console.settings = dict(VALID, automatic=True)
        for code, expected in ((1, 'retry'), (3, 'storage'), (0, 'scheduled')):
            record = dict(new_files=0)
            process = SimpleNamespace(stdout=io.StringIO(''), wait=lambda: code)
            self.console.watch(process, record)
            self.assertEqual(self.console.next_kind, expected)
            if code == 3:
                self.assertEqual(record['state'], 'paused')
        self.console.stop()
        self.assertIsNone(self.console.next_run)
        self.assertEqual(self.console.retry_count, 0)

    def test_scheduler_runs_exactly_three_retries_after_initial_failure(self):
        self.session.write_text('{}')  # Invalid session fails before any external connection.
        self.console.save(dict(VALID, automatic=True))
        for attempt in range(4):
            with self.console.lock:
                self.console.next_run = 0  # Advance the deadline, not the production delay.
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                snapshot = self.console.snapshot()
                if len(snapshot['history']) == attempt + 1 and not snapshot['running']:
                    break
                time.sleep(.02)
            self.assertEqual(len(snapshot['history']), attempt + 1)
            self.assertEqual(snapshot['history'][0]['retry_attempt'], attempt)
            self.assertEqual(snapshot['history'][0]['trigger'], 'retry' if attempt else 'automatic')
            self.assertEqual(snapshot['last_exit'], 1)
        self.assertEqual(snapshot['next_kind'], 'scheduled')
        self.assertGreater(snapshot['next_run'] - time.time(), 86390)
        self.console.stop()


class DesktopModeTests(unittest.TestCase):
    mode = 'desktop'
    setUp = WebSettingsTests.setUp
    tearDown = WebSettingsTests.tearDown
    request = WebSettingsTests.request

    def test_one_use_connection_code_expiry_origin_and_host(self):
        code = self.console.new_connect_code()
        body = {'code': code}
        self.assertEqual(self.request('/api/connect', body, auth=False, origin='http://evil.example')[0], 403)
        status, payload = self.request('/api/connect', body, auth=False, origin=self.base)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)['token'], self.console.token)
        self.assertEqual(self.request('/api/connect', body, auth=False)[0], 400)
        code = self.console.new_connect_code()
        self.console.connect_deadline = 0
        self.assertEqual(self.request('/api/connect', {'code': code}, auth=False)[0], 400)
        request = Request(self.base + '/api/mode', headers={'Host': 'evil.example'})
        with self.assertRaises(HTTPError) as caught:
            self.http.open(request)
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(ValueError):
            make_server(self.console, '0.0.0.0', 0)

    def test_desktop_never_resumes_or_accepts_automatic_jobs(self):
        self.assertEqual(self.request('/api/settings', dict(VALID, automatic=True))[0], 400)
        self.console.close()
        path = self.root / 'config/settings.json'
        path.write_text(json.dumps(dict(VALID, automatic=True)))
        self.console = Console(path.parent, self.env)
        self.assertFalse(self.console.settings['automatic'])
        self.assertIsNone(self.console.next_run)
        self.console.settings['automatic'] = True  # Guard even if state changes later.
        self.console.schedule_next(failed=True)
        self.assertIsNone(self.console.next_run)

    def test_local_import_is_temporary_output_survives_restart_without_secret_in_response(self):
        credential = 'SYNTHETIC_DESKTOP_SECRET'
        code, payload = self.request('/api/local', {'session': {'user_id': 'demo', 'token': credential, 'extra': 'discard'}})
        self.assertEqual(code, 200)
        self.assertNotIn(credential.encode(), payload)
        self.assertFalse(self.session.exists())
        self.assertEqual(self.console.account_session, {'user_id': 'demo', 'token': credential})
        output = str((self.root / 'selected videos').resolve())
        self.assertEqual(self.request('/api/local', {'output_dir': output})[0], 200)
        self.console.close()
        self.console = Console(self.root / 'config', self.env)
        self.assertEqual(self.console.snapshot()['output'], output)
        self.assertFalse(self.console.snapshot()['settings']['automatic'])
        self.assertFalse(self.console.snapshot()['session_ready'])
        for invalid in ({'session': {'token': 'missing-user'}}, {'output_dir': 'relative'},
                        {'output_dir': str(Path(web_settings.__file__).resolve().parent.parent / 'videos')},
                        {'output_dir': '/tmp/line\nbreak'}, {'session': []}):
            with self.assertRaises(ValueError):
                self.console.local_setup(invalid)
        self.assertFalse(self.session.exists())
        self.console.process = object()
        with self.assertRaises(RuntimeError):
            self.console.local_setup({'output_dir': output})
        self.console.process = None

    def test_server_does_not_accept_local_setup_or_bootstrap(self):
        self.console.mode = 'server'
        self.assertEqual(self.request('/api/connect', {'code': self.console.new_connect_code()}, auth=False)[0], 401)
        self.assertEqual(self.request('/api/local', {'session': {'user_id': 'demo', 'token': 'fake'}})[0], 403)
        self.assertFalse(self.session.exists())

    def test_desktop_worker_is_once_and_failure_never_schedules_retry(self):
        self.session.write_text('{}')  # Fails before any network request.
        self.console.save(VALID)
        self.assertEqual(environment(VALID, self.env)['PAWZZ_ONCE'], '1')
        self.assertEqual(self.request('/api/run', {})[0], 200)
        self.console.monitor.join(timeout=8)
        status = self.console.snapshot()
        self.assertFalse(status['running'])
        self.assertEqual(status['history'][0]['state'], 'failed')
        self.assertIsNone(status['next_run'])


class ExitStatusTests(unittest.TestCase):
    def test_configuration_success_and_failure_are_distinct(self):
        env = environment(VALID, {})
        for setup, outcome, expected in (({}, None, 2), (env, 0, 0),
                                         (env, FileNotFoundError('PRIVATE_PATH'), 1),
                                         (env, direct_exporter.LowStorageError(), 3)):
            output = io.StringIO()
            with patch.dict(os.environ, setup, clear=True), contextlib.redirect_stdout(output), \
                 patch('direct_exporter.export_cycle', side_effect=outcome if isinstance(outcome, Exception) else None,
                       return_value=outcome):
                self.assertEqual(direct_exporter.main(), expected)
            self.assertNotIn('PRIVATE_PATH', output.getvalue())
            self.assertNotIn('Traceback', output.getvalue())


if __name__ == '__main__':
    unittest.main()
