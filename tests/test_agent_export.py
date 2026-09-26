import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_export


class AgentExportTests(unittest.TestCase):
    def test_check_is_read_only_run_needs_permission_and_results_hide_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / 'settings.json'
            session = root / 'session.json'
            settings.write_text(json.dumps(dict(device_ip='192.168.1.50', callback_ip='192.168.1.10', serial='DEMO')))
            session.write_text(json.dumps(dict(user_id='demo', token='SYNTHETIC_AGENT_SECRET')))
            args = ['--settings', str(settings), '--session', str(session), '--output', str(root / 'new/videos')]
            def invoke(action, *extra):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = agent_export.main([action, *args, *extra])
                self.assertNotIn('SYNTHETIC_AGENT_SECRET', out.getvalue())
                return code, json.loads(out.getvalue())
            with patch('agent_export.export_cycle', return_value=3) as export:
                self.assertEqual(invoke('check'), (0, dict(status='ready', scope='local_checks_only', device_verified=False)))
                self.assertFalse((root / 'new').exists())
                self.assertFalse((root / 'instance.lock').exists())
                self.assertEqual(invoke('run')[0], 2)
                export.assert_not_called()
                code, result = invoke('run', '--allow-maintenance')
                self.assertEqual((code, result), (0, dict(status='success', new_files=3)))
                self.assertTrue(export.call_args.args[0]['once'])
                self.assertEqual(export.call_count, 1)
            with patch('agent_export.export_cycle', side_effect=ValueError('SYNTHETIC_AGENT_SECRET')):
                self.assertEqual(invoke('run', '--allow-maintenance'), (1, dict(status='failed', reason='invalid_data', new_files=None)))
            with patch('agent_export.export_cycle', side_effect=agent_export.LowStorageError()):
                self.assertEqual(invoke('run', '--allow-maintenance')[0], 3)
            with patch('agent_export.export_cycle', side_effect=KeyboardInterrupt):
                self.assertEqual(invoke('run', '--allow-maintenance')[0], 130)
            settings.write_text('{broken')
            self.assertEqual(invoke('check')[0], 2)

    def test_web_login_uses_user_page_and_returns_real_child_result_without_credentials(self):
        import subprocess
        import sys
        import web_settings
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / 'settings.json'
            settings.write_text(json.dumps(dict(device_ip='192.168.1.50', callback_ip='192.168.1.10', serial='DEMO')))
            args = ['--settings', str(settings), '--web-login', '--output', str(root / 'videos')]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(agent_export.main(['check', *args]), 0)
            self.assertTrue(json.loads(output.getvalue())['login_required'])
            self.assertFalse((root / 'agent-console').exists())
            actual_spawn = subprocess.Popen
            consoles = []
            def make_console(*args):
                console = web_settings.Console(*args)
                consoles.append(console)
                return console
            def user_opens_page(url):
                self.assertIn('#connect=', url)
                console = consoles[0]
                self.assertFalse(console.snapshot()['running'])
                console.install_session({'user_id': 'demo', 'token': 'SYNTHETIC_WEB_AGENT'}, False)
                console.start()  # Simulate the user's distinct Start click.
            def child(args, **kwargs):
                code = "import os; from synology_exporter import read_session; assert read_session(int(os.environ['PAWZZ_SESSION_FD'])) == ('demo','SYNTHETIC_WEB_AGENT'); print('cycle complete: 2 original files')"
                return actual_spawn([sys.executable, '-c', code], **kwargs)
            output = io.StringIO()
            with patch('agent_export.Console', side_effect=make_console), patch('agent_export.webbrowser.open', side_effect=user_opens_page), patch('web_settings.subprocess.Popen', side_effect=child), contextlib.redirect_stdout(output):
                self.assertEqual(agent_export.main(['run', *args, '--allow-maintenance']), 0)
            lines = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(lines[0]['status'], 'awaiting_user')
            self.assertEqual(lines[-1], {'status': 'success', 'new_files': 2})
            self.assertNotIn('SYNTHETIC_WEB_AGENT', output.getvalue())
            self.assertIsNone(consoles[0].account_session)
            for path in root.rglob('*'):
                if path.is_file():
                    self.assertNotIn('SYNTHETIC_WEB_AGENT', path.read_text())


if __name__ == '__main__':
    unittest.main()
