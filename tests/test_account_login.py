import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

import account_login
import direct_exporter
from synology_exporter import AuthenticationError, DeviceClient
import test_web_settings
from test_web_settings import VALID


class ProtocolTests(unittest.TestCase):
    def test_signing_and_real_data_shape_reject_missing_credentials(self):
        self.assertEqual(account_login.sign(account_login.SMS_PATH, '1000'), '4418C9597AD7F2D12F9928D271ED1889')
        self.assertEqual(account_login.sign(account_login.LOGIN_PATH, '1000'), 'D70D0406B7ACC27ACF4DB5E7F7A6AF5B')
        payload = {'user': {'id': 123}, 'token': 'SYNTHETIC_ONLY'}
        for data in (payload, json.dumps(payload)):
            with patch('account_login.post', return_value={'data': data}) as post:
                self.assertEqual(account_login.login('13900000000', '123456'), {'user_id': '123', 'token': 'SYNTHETIC_ONLY'})
                self.assertEqual(post.call_args.args[1]['captcha'], '123456')
        for data in (None, [], {'user': {}, 'token': 'SYNTHETIC_ONLY'}, 'bad-json'):
            with patch('account_login.post', return_value={'data': data}), self.assertRaises(account_login.LoginError):
                account_login.login('13900000000', '123456')
        with patch('account_login.post') as post:
            for phone, code in (('invalid', '123456'), ('13900000000', '<script>')):
                with self.assertRaises(account_login.LoginError):
                    account_login.login(phone, code)
            post.assert_not_called()

    def test_transport_rejects_redirects_server_errors_and_sensitive_messages(self):
        with self.assertRaises(account_login.LoginError):
            account_login.NoRedirect().redirect_request(None, None, 302, None, None, 'https://other.invalid')
        for body in ({'success': False, 'msg': 'PRIVATE_VALUE'}, {'code': 401, 'msg': 'PRIVATE_VALUE'}, []):
            response = io.BytesIO(json.dumps(body).encode())
            with patch('account_login.build_opener') as opener:
                opener.return_value.open.return_value = response
                with self.assertRaises(account_login.LoginError) as caught:
                    account_login.send_code('13900000000')
                self.assertNotIn('PRIVATE_VALUE', str(caught.exception))
                request = opener.return_value.open.call_args.args[0]
                self.assertEqual(request.full_url, account_login.API_ROOT + account_login.SMS_PATH)
                self.assertNotIn('13900000000', request.full_url)
        with patch('account_login.post', return_value={'unknown': True}), self.assertRaises(account_login.LoginError):
            account_login.send_code('13900000000')

    def test_mqtt_auth_rejection_is_distinct_from_network_failure(self):
        from types import SimpleNamespace
        client = DeviceClient('demo', 'TEST_SERIAL', 'SYNTHETIC_ONLY')
        client._connected(None, None, None, SimpleNamespace(is_failure=True, value=135), None)
        self.assertTrue(client.auth_rejected)
        client._connected(None, None, None, SimpleNamespace(is_failure=True, value=136), None)
        self.assertFalse(client.auth_rejected)
        client._disconnected(None, None, None, SimpleNamespace(is_failure=True, value=135), None)
        self.assertTrue(client.auth_rejected)
        with patch('direct_exporter.config', return_value={'once': True}), patch('direct_exporter.export_cycle', side_effect=AuthenticationError()):
            self.assertEqual(direct_exporter.main(), 4)


class LoginConsoleTests(unittest.TestCase):
    mode = 'desktop'
    setUp = test_web_settings.WebSettingsTests.setUp
    tearDown = test_web_settings.WebSettingsTests.tearDown
    request = test_web_settings.WebSettingsTests.request

    def login(self, remember=False):
        self.console.sms_next = 0
        with patch('account_login.send_code'), patch('account_login.login', return_value={'user_id': 'demo', 'token': 'SYNTHETIC_MEMORY_ONLY'}):
            self.assertEqual(self.request('/api/account/send', {'phone': '13900000000'})[0], 200)
            return self.request('/api/account/login', {'phone': '13900000000', 'code': '123456', 'remember': remember})

    def test_temporary_login_never_persists_and_expires_without_fallback(self):
        self.assertEqual(self.login()[0], 200)
        snapshot = self.console.snapshot()
        self.assertTrue(snapshot['session_ready'])
        self.assertFalse(snapshot['running'])
        self.assertFalse(snapshot['settings']['allow_maintenance'])
        self.assertNotIn('SYNTHETIC_MEMORY_ONLY', json.dumps(snapshot))
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn('SYNTHETIC_MEMORY_ONLY', path.read_text())
                self.assertNotIn('13900000000', path.read_text())
                self.assertNotIn('123456', path.read_text())
        self.session.write_text('{"user_id":"old", "token":"OLD_SYNTHETIC"}')
        self.console.account_deadline = 0
        self.assertFalse(self.console.snapshot()['session_ready'])
        self.console.close()
        from web_settings import Console
        self.console = Console(self.root / 'config', self.env)
        self.assertFalse(self.console.snapshot()['session_ready'])
        self.assertTrue(self.session.exists())

    def test_sms_rate_limit_attempt_limit_and_origin(self):
        with patch('account_login.send_code') as send:
            self.assertEqual(self.request('/api/account/send', {'phone': '13900000000'}, auth=False)[0], 401)
            self.assertEqual(self.request('/api/account/send', {'phone': '13900000000'}, origin='https://evil.invalid')[0], 403)
            send.assert_not_called()
            self.assertEqual(self.request('/api/account/send', {'phone': '13900000000'})[0], 200)
            self.assertEqual(self.request('/api/account/send', {'phone': '13900000000'})[0], 400)
            self.assertEqual(send.call_count, 1)
        with patch('account_login.login', side_effect=account_login.LoginError('验证码错误')) as login:
            for _ in range(6):
                self.assertEqual(self.request('/api/account/login', {'phone': '13900000000', 'code': '123456', 'remember': False})[0], 400)
            self.assertEqual(login.call_count, 5)
        self.console.sms_deadline = 0
        self.console.snapshot()
        self.assertIsNone(self.console.sms_phone)

    def test_nas_http_and_spoofed_forwarded_header_cannot_send_secrets(self):
        self.console.mode = 'server'
        with patch('account_login.send_code') as send:
            request = Request(self.base + '/api/account/send', data=b'{"phone":"13900000000"}',
                              headers={'Content-Type': 'application/json', 'X-Forwarded-Proto': 'https',
                                       'Authorization': 'Bearer ' + self.console.token})
            with self.assertRaises(HTTPError) as caught:
                self.http.open(request)
            self.assertEqual(caught.exception.code, 403)
            caught.exception.close()
            send.assert_not_called()
            self.assertFalse(json.loads(self.request('/api/status')[1])['account']['login_allowed'])

    def test_real_https_accepts_login_with_verified_test_certificate(self):
        import datetime
        import ipaddress
        import ssl
        import threading
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from urllib.request import build_opener, ProxyHandler, HTTPSHandler
        from web_settings import make_server
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, 'localhost')])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
                .not_valid_after(now + datetime.timedelta(hours=1))
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
                .sign(key, hashes.SHA256()))
        cert_path, key_path = self.root / 'test-cert.pem', self.root / 'test-cert.key'
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.console.mode = 'server'
        self.console.env.update(PAWZZ_TLS_CERT=str(cert_path), PAWZZ_TLS_KEY=str(key_path))
        server = make_server(self.console, '127.0.0.1', 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        old_base, old_http = self.base, self.http
        try:
            self.base = f'https://127.0.0.1:{server.server_port}'
            # Trust this per-test certificate explicitly; verification remains enabled.
            self.http = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context(cafile=str(cert_path))))
            self.assertTrue(json.loads(self.request('/api/status')[1])['account']['login_allowed'])
            self.assertEqual(self.login(remember=True)[0], 200)
            self.assertEqual(self.console.snapshot()['account']['source'], 'saved')
            self.assertNotIn(b'SYNTHETIC_MEMORY_ONLY', self.console.account_file.read_bytes())
            self.assertEqual(self.request('/api/account/logout', {})[0], 200)
            self.assertFalse(self.console.account_file.exists())
        finally:
            self.base, self.http = old_base, old_http
            server.shutdown(); server.server_close(); thread.join()

    def test_saved_file_is_explicit_restrictive_restartable_and_removed_on_logout(self):
        self.console.mode = 'server'
        self.env['PAWZZ_MODE'] = 'server'
        value = {'user_id': 'demo', 'token': 'SYNTHETIC_SAVED_ONLY'}
        self.console.install_session(value, True)
        file = self.console.account_file
        self.assertNotIn(b'SYNTHETIC_SAVED_ONLY', file.read_bytes())
        import credential_store
        self.assertEqual(credential_store.load(file, self.console.account_key), value)
        self.assertEqual(self.console.account_key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(file.parent.stat().st_mode & 0o777, 0o700)
        self.console.close()
        from web_settings import Console
        self.console = Console(self.root / 'config', self.env)
        self.assertEqual(self.console.snapshot()['account']['source'], 'saved')
        self.console.account_action('logout', {})
        self.assertFalse(file.exists())
        self.assertFalse(self.console.snapshot()['session_ready'])

    def test_failed_save_never_reports_persistence_success(self):
        self.console.mode = 'server'
        with patch('credential_store.save', side_effect=PermissionError('SYNTHETIC_PRIVATE_PATH')), self.assertRaises(OSError):
            self.console.install_session({'user_id': 'demo', 'token': 'SYNTHETIC_ONLY'}, True)
        self.assertFalse(self.console.session_ready())
        self.assertFalse(self.console.account_file.exists())

    def test_server_temporary_schedule_and_rejected_auth_do_not_silently_resume(self):
        from web_settings import Console
        self.console.mode = 'server'
        self.env['PAWZZ_MODE'] = 'server'
        with self.assertRaises(ValueError):
            self.console.save(dict(VALID, automatic=True))
        self.console.install_session({'user_id': 'demo', 'token': 'SYNTHETIC_ONLY'}, False)
        self.console.settings = dict(VALID, automatic=True)
        self.console.account_deadline = 0
        self.assertTrue(self.console.snapshot()['session_ready'])
        self.console.stop()
        self.assertGreater(self.console.account_deadline, time.monotonic())
        self.console.settings['automatic'] = True
        from types import SimpleNamespace
        self.console.watch(SimpleNamespace(stdout=io.StringIO(''), wait=lambda: 4), dict(new_files=0))
        self.assertFalse(self.console.settings['automatic'])
        self.assertFalse(self.console.snapshot()['session_ready'])
        self.assertIsNone(self.console.next_run)
        self.console.close()
        self.console = Console(self.root / 'config', self.env)
        self.assertFalse(self.console.snapshot()['session_ready'])
        self.assertFalse(self.console.settings['automatic'])

    def test_wrong_missing_key_and_tampered_ciphertext_fail_closed(self):
        import credential_store
        from cryptography.fernet import Fernet
        self.console.mode = 'server'
        self.env['PAWZZ_MODE'] = 'server'
        self.console.install_session({'user_id': 'demo', 'token': 'SYNTHETIC_ONLY'}, True)
        file, key = self.console.account_file, self.console.account_key
        original_cipher, original_key = file.read_bytes(), key.read_bytes()
        for broken_key in (Fernet.generate_key(), b'bad-key'):
            key.write_bytes(broken_key)
            with self.assertRaises((ValueError, OSError)):
                credential_store.load(file, key)
        key.write_bytes(original_key)
        file.write_bytes(original_cipher[:-8] + b'xxxxxxxx')
        with self.assertRaises(ValueError):
            credential_store.load(file, key)
        file.write_bytes(original_cipher)
        key.unlink()
        self.console.close()
        from web_settings import Console
        self.console = Console(self.root / 'config', self.env)
        self.assertFalse(self.console.snapshot()['session_ready'])
        self.assertTrue(file.exists())
        self.assertFalse(key.exists())
        with self.assertRaises(account_login.LoginError):
            credential_store.key_location(str(self.root / 'config/key'), self.root / 'config')

    def test_real_child_receives_pipe_not_credential_arguments_then_desktop_clears(self):
        self.assertEqual(self.login()[0], 200)
        self.console.save(VALID)
        real_spawn = subprocess.Popen
        def child(args, **kwargs):
            self.assertNotIn('SYNTHETIC_MEMORY_ONLY', json.dumps(kwargs['env']))
            self.assertNotIn('SYNTHETIC_MEMORY_ONLY', str(args))
            code = "import os; from synology_exporter import read_session; assert read_session(int(os.environ['PAWZZ_SESSION_FD'])) == ('demo', 'SYNTHETIC_MEMORY_ONLY'); print('cycle complete: 2 original files')"
            return real_spawn([sys.executable, '-c', code], **kwargs)
        with patch('web_settings.subprocess.Popen', side_effect=child):
            self.console.start()
        self.console.monitor.join(8)
        self.assertEqual(self.console.last_exit, 0)
        self.assertEqual(self.console.history[0]['new_files'], 2)
        self.assertIsNone(self.console.account_session)
        self.assertFalse(self.console.snapshot()['session_ready'])

    def test_logout_stops_child_before_reporting_credentials_cleared(self):
        self.assertEqual(self.login()[0], 200)
        self.console.save(VALID)
        real_spawn = subprocess.Popen
        def child(args, **kwargs):
            code = "import os,time; from synology_exporter import read_session; read_session(int(os.environ['PAWZZ_SESSION_FD'])); time.sleep(20)"
            return real_spawn([sys.executable, '-c', code], **kwargs)
        with patch('web_settings.subprocess.Popen', side_effect=child):
            self.console.start()
        self.console.account_action('logout', {})
        self.assertFalse(self.console.session_ready())
        self.console.monitor.join(8)
        self.assertIsNone(self.console.account_session)
        self.assertIsNone(self.console.process)
        self.assertFalse(self.console.logout_pending)
        self.assertFalse(self.console.settings['automatic'])


if __name__ == '__main__':
    unittest.main()
