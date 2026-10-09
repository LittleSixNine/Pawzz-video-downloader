import base64
import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request
from urllib.error import HTTPError

import credential_store
from account_login import LoginError
from web_settings import Console, directory_status, validate
import test_web_settings
from test_web_settings import VALID


class ConsoleImprovementsTests(unittest.TestCase):
    mode = 'desktop'
    setUp = test_web_settings.WebSettingsTests.setUp
    tearDown = test_web_settings.WebSettingsTests.tearDown
    request = test_web_settings.WebSettingsTests.request

    def test_new_install_admin_and_existing_plain_password_preserved(self):
        fresh = Console(self.root / 'fresh', self.env)
        try:
            self.assertEqual(fresh.token, 'admin')
            self.assertTrue(fresh.snapshot()['default_password'])
            self.assertEqual((fresh.folder / 'admin-token').stat().st_mode & 0o777, 0o600)
        finally:
            fresh.close()
        self.assertEqual(self.console.token, 'SYNTHETIC_MANAGEMENT_PASSWORD_FOR_TEST')

    def test_password_rotation_unicode_spaces_restart_and_old_auth_revoked(self):
        old = self.console.token
        for password in ('x', ' 密码大小写 aA '):
            code, payload = self.request('/api/password', {'current': self.console.token, 'password': password, 'confirm': password})
            self.assertEqual(code, 200)
            self.assertNotIn(b'"password":', payload)
            header = 'Bearer64 ' + base64.b64encode(password.encode()).decode()
            with self.http.open(Request(self.base + '/api/status', headers={'Authorization': header})) as response:
                self.assertEqual(response.status, 200)
        with self.assertRaises(HTTPError) as caught:
            self.http.open(Request(self.base + '/api/status', headers={'Authorization': 'Bearer ' + old}))
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()
        self.console.close()
        self.console = Console(self.root / 'config', self.env)
        self.assertEqual(self.console.token, ' 密码大小写 aA ')
        self.assertEqual((self.console.folder / 'admin-token').stat().st_mode & 0o777, 0o600)

    def test_password_errors_keep_old_password_and_http_cannot_change_it(self):
        old = self.console.token
        for body in ({'current': old, 'password': '', 'confirm': ''},
                     {'current': 'incorrect', 'password': 'x', 'confirm': 'x'},
                     {'current': old, 'password': 'x', 'confirm': 'X'}):
            self.assertEqual(self.request('/api/password', body)[0], 400)
            self.assertEqual(self.console.token, old)
        body = {'current': old, 'password': 'x', 'confirm': 'x'}
        with patch('web_settings.save_json', side_effect=PermissionError(13, 'PRIVATE_PATH')):
            code, payload = self.request('/api/password', body)
            self.assertEqual(code, 400)
            self.assertNotIn(b'PRIVATE_PATH', payload)
            self.assertEqual(self.console.token, old)
        self.console.mode = 'server'
        self.assertEqual(self.request('/api/password', body)[0], 403)
        self.assertEqual(self.console.token, old)
        self.assertEqual(self.request('/api/password', body, auth=False)[0], 401)
        self.assertEqual(self.request('/api/password', body, origin='https://evil.invalid')[0], 403)

    def test_failed_save_retains_login_and_retries_without_sms_reusing_key(self):
        self.console.mode = 'server'
        self.env['PAWZZ_MODE'] = 'server'
        value = {'user_id': 'demo', 'token': 'PRIVATE_CREDENTIAL_FOR_TEST'}
        original_chmod = os.chmod
        def fail_login_chmod(path, mode):
            if path == self.console.account_file.parent:
                raise PermissionError(1, 'PRIVATE_PATH')
            return original_chmod(path, mode)
        logs = io.StringIO()
        with contextlib.redirect_stdout(logs), patch('credential_store.os.chmod', side_effect=fail_login_chmod):
            self.console.install_session(value, True)
        self.assertTrue(self.console.session_ready())
        snapshot = self.console.snapshot()
        self.assertEqual(snapshot['account']['source'], 'temporary')
        self.assertFalse(snapshot['account']['saved'])
        self.assertIn('EPERM', snapshot['account']['save_problem'])
        key = self.console.account_key.read_bytes()
        self.assertNotIn('PRIVATE_PATH', logs.getvalue())
        self.assertNotIn(value['token'], json.dumps(snapshot))
        self.console.account_action('save', {})
        self.assertEqual(self.console.account_key.read_bytes(), key)
        self.assertEqual(credential_store.load(self.console.account_file, self.console.account_key), value)
        self.assertIsNone(self.console.snapshot()['account']['save_problem'])
        self.console.close()
        self.console = Console(self.root / 'config', self.env)
        self.assertEqual(self.console.account_session, value)
        self.assertEqual(self.console.account_source, 'saved')

    def test_key_permission_failure_reports_stage_without_secret_or_path(self):
        value = {'user_id': 'demo', 'token': 'PRIVATE_CREDENTIAL_FOR_TEST'}
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch('credential_store.os.chmod', side_effect=PermissionError(1, 'PRIVATE_PATH')):
            with self.assertRaises(LoginError) as caught:
                credential_store.save(self.console.account_file, self.console.account_key, value)
        self.assertIn('key_permissions', output.getvalue())
        self.assertIn('所有者', str(caught.exception))
        self.assertNotIn('PRIVATE_PATH', output.getvalue())
        self.assertNotIn(value['token'], output.getvalue())

    def test_inaccessible_login_directory_preserves_new_login_and_status(self):
        self.console.mode = 'server'
        original_exists = Path.exists
        def inaccessible(path):
            if path == self.console.account_file:
                raise PermissionError(13, 'PRIVATE_PATH')
            return original_exists(path)
        with patch.object(Path, 'exists', inaccessible):
            self.console.install_session({'user_id': 'demo', 'token': 'PRIVATE_CREDENTIAL'}, True)
            status = self.console.snapshot()
        self.assertTrue(status['session_ready'])
        self.assertEqual(status['account']['source'], 'temporary')
        self.assertFalse(status['account']['saved'])
        self.assertIn('登录目录无法访问', status['account']['save_problem'])
        self.assertNotIn('PRIVATE_PATH', json.dumps(status))

    def test_permissions_report_owner_mismatch_without_changing_directories(self):
        with patch('web_settings.os.geteuid', return_value=os.geteuid() + 1):
            status = self.console.snapshot()
        self.assertEqual(status['permissions']['directories'][0]['state'], 'owner_mismatch')
        self.assertEqual(self.console.folder.stat().st_uid, os.geteuid())

    def test_storage_states_write_read_delete_and_running_guard(self):
        output = self.root / 'videos'
        self.assertEqual(directory_status(output)['state'], 'missing')
        self.assertEqual(directory_status(self.root / 'config/admin-token')['state'], 'not_directory')
        output.mkdir()
        with patch('web_settings.os.access', return_value=False):
            self.assertEqual(directory_status(output)['state'], 'inaccessible')
        with patch('web_settings.os.access', side_effect=[True, False]):
            self.assertEqual(directory_status(output)['state'], 'not_writable')
        original = output / 'original.mp4'
        original.write_bytes(b'UNCHANGED')
        code, body = self.request('/api/storage/check', {})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)['storage']['write_check']['passed'])
        self.assertEqual(original.read_bytes(), b'UNCHANGED')
        self.assertEqual(list(output.iterdir()), [original])
        self.console.process = object()
        try:
            self.assertEqual(self.request('/api/storage/check', {})[0], 409)
        finally:
            self.console.process = None
        original.unlink()
        output.rmdir()
        self.assertEqual(self.request('/api/storage/check', {})[0], 200)
        self.assertFalse(self.console.snapshot()['storage']['write_check']['passed'])

    def test_intervals_validate_and_schedule_exact_elapsed_delay(self):
        self.console.mode = 'server'
        for value, seconds in (('3h', 10800), ('6h', 21600), ('12h', 43200)):
            settings = dict(VALID, interval=value, automatic=True)
            self.assertEqual(validate(settings), settings)
            self.console.settings = settings
            with patch('web_settings.time.time', return_value=1000):
                self.console.schedule_next()
            self.assertEqual(self.console.next_run, 1000 + seconds)
