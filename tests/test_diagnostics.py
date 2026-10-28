import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from diagnostics import DailyLogs, PREFIX, clean_event, day_name, error_fields, event, phase
from direct_exporter import Receiver
import test_web_settings

TASK = '0123456789abcdef'
OTHER = 'abcdef0123456789'


def entry(**values):
    return dict(dict(time=time.time(), task_id=TASK, stage='catalog', action='waiting'), **values)


class DailyLogTests(unittest.TestCase):
    def test_allowlist_discards_credentials_paths_addresses_and_exception_messages(self):
        error = PermissionError(13, 'PRIVATE_PATH token=SECRET')
        value = clean_event(entry(token='SECRET', ip='192.168.1.1', mac='02:00:00:00:00:01',
                                  message=str(error), **error_fields(error)))
        self.assertEqual(value['errno'], 13)
        self.assertEqual(value['exception'], 'PermissionError')
        self.assertNotIn('SECRET', json.dumps(value))
        self.assertNotIn('192.168', json.dumps(value))
        for changes in ({'stage': 'SECRET'}, {'task_id': '../private'}, {'time': float('nan')},
                        {'action': 'SECRET'}, {'time': True}):
            with self.assertRaises(ValueError):
                clean_event(entry(**changes))

    def test_phase_logs_safe_failure_and_preserves_the_error(self):
        output = io.StringIO()
        with patch.dict(os.environ, PAWZZ_TASK_ID=TASK), contextlib.redirect_stdout(output):
            with self.assertRaises(PermissionError):
                with phase('storage'):
                    raise PermissionError(13, 'PRIVATE_PATH')
        rows = [json.loads(line[len(PREFIX):]) for line in output.getvalue().splitlines()]
        self.assertEqual([row['action'] for row in rows], ['start', 'failed'])
        self.assertEqual(rows[-1]['errno'], 13)
        self.assertGreaterEqual(rows[-1]['duration_ms'], 0)
        self.assertNotIn('PRIVATE_PATH', output.getvalue())

    def test_beijing_midnight_permissions_and_retention(self):
        midnight = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(day_name(midnight - 1), '2026-10-28')
        self.assertEqual(day_name(midnight), '2026-10-29')
        with tempfile.TemporaryDirectory() as folder:
            logs = DailyLogs(folder)
            for offset in range(20):
                self.assertTrue(logs.write(entry(time=midnight - (19 - offset) * 86400)))
            self.assertEqual(len(logs.dates()), 14)
            self.assertEqual(logs.dates()[-1], '2026-10-16')
            keep = logs.folder / 'private-note.txt'
            keep.write_text('do not remove')
            logs.write(entry(time=midnight + 86400))
            self.assertTrue(keep.exists())
            self.assertEqual(logs.folder.stat().st_mode & 0o777, 0o700)
            self.assertEqual((logs.folder / '2026-10-30.jsonl').stat().st_mode & 0o777, 0o600)

    def test_size_rotation_keeps_recent_entries_and_reader_filters_tasks(self):
        with tempfile.TemporaryDirectory() as folder, patch('diagnostics.MAX_BYTES', 900):
            logs = DailyLogs(folder)
            for i in range(80):
                logs.write(entry(count=i, task_id=TASK if i % 2 else OTHER))
            files = list(logs.folder.glob('*.jsonl*'))
            self.assertEqual(len(files), 3)
            self.assertTrue(all(path.stat().st_size <= 900 for path in files))
            value = logs.read(day_name(), TASK, limit=2)
            self.assertTrue(value['truncated'])
            self.assertEqual([row['count'] for row in value['entries']], [77, 79])
            self.assertTrue(all(row['task_id'] == TASK for row in logs.read(day_name(), TASK, None)['entries']))

    def test_write_failure_is_visible_and_reader_rejects_paths_and_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            logs = DailyLogs(folder)
            with patch('diagnostics.os.open', side_effect=PermissionError(13, 'PRIVATE_PATH')):
                self.assertFalse(logs.write(entry()))
            self.assertIn('详细日志保存失败', logs.problem)
            self.assertNotIn('PRIVATE_PATH', logs.problem)
            self.assertTrue(logs.write(entry()))
            self.assertIsNone(logs.problem)
            for day in ('../../secret', '2026-02-30', '2026-10-28.jsonl'):
                with self.assertRaises(ValueError):
                    logs.read(day)
            (logs.folder / '2026-01-01.jsonl').symlink_to(logs.folder / (day_name() + '.jsonl'))
            self.assertEqual(logs.read('2026-01-01')['entries'], [])

    def test_image_context_includes_every_runtime_module(self):
        project = Path(__file__).resolve().parent.parent
        allowed = (project / '.dockerignore').read_text().splitlines()
        for module in (project / 'src').glob('*.py'):
            self.assertIn('!src/' + module.name, allowed)

    def test_rejected_callbacks_are_logged_once_without_accepting_wrong_source(self):
        output = io.StringIO()
        http = build_opener(ProxyHandler({}))
        with patch.dict(os.environ, PAWZZ_TASK_ID=TASK), contextlib.redirect_stdout(output):
            with Receiver('127.0.0.2', '127.0.0.1', 0) as receiver:
                url = f'http://127.0.0.1:{receiver.server.server_port}/{receiver.key}/file'
                for _ in range(2):
                    with self.assertRaises(HTTPError) as caught:
                        http.open(Request(url, data=b'PRIVATE_BODY', method='PUT'))
                    self.assertEqual(caught.exception.code, 403)
                    caught.exception.close()
                with self.assertRaises(TimeoutError):
                    receiver.wait(.01)
        rows = [json.loads(line[len(PREFIX):]) for line in output.getvalue().splitlines()]
        rejected = [row for row in rows if row['action'] == 'callback_rejected']
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]['reason'], 'source_mismatch')
        self.assertNotIn(receiver.key, output.getvalue())
        self.assertNotIn('PRIVATE_BODY', output.getvalue())


class DiagnosticAPITests(unittest.TestCase):
    setUp = test_web_settings.WebSettingsTests.setUp
    tearDown = test_web_settings.WebSettingsTests.tearDown
    request = test_web_settings.WebSettingsTests.request

    def test_authenticated_bounded_view_and_full_download(self):
        for i in range(250):
            self.console.logs.write(entry(count=i, task_id=TASK if i % 2 else OTHER))
        query = '/api/logs?date=' + day_name()
        self.assertEqual(self.request(query, auth=False)[0], 401)
        self.assertEqual(self.request(query, origin='http://evil.example')[0], 403)
        value = json.loads(self.request(query)[1])
        self.assertEqual(len(value['entries']), 200)
        self.assertTrue(value['truncated'])
        filtered = json.loads(self.request(query + '&task=' + TASK)[1])
        self.assertEqual(filtered['count'], 125)
        request = Request(self.base + query + '&download=1&task=' + TASK,
                          headers={'Authorization': 'Bearer ' + self.console.token})
        with self.http.open(request) as response:
            self.assertIn('attachment;', response.headers['Content-Disposition'])
            rows = [json.loads(line) for line in response.read().splitlines()]
            self.assertEqual(len(rows), 125)
        for suffix in ('?date=../../secret', '?date=2026-02-30', '?date=' + day_name() + '&task=bad',
                       '?date=' + day_name() + '&download=2', '?date=a&date=b', '?path=secret'):
            self.assertEqual(self.request('/api/logs' + suffix)[0], 400)
        with patch.object(self.console.logs, 'read', side_effect=AssertionError('status must not read logs')):
            self.assertEqual(self.request('/api/status')[0], 200)

    def test_worker_events_have_parent_task_id_and_actionable_failure(self):
        record = dict(id=TASK, new_files=0, started_at=time.time())
        lines = [PREFIX + json.dumps(entry(task_id=OTHER, stage='catalog', action='callback_rejected',
                                         reason='source_mismatch', token='PRIVATE_TOKEN')),
                 'RAW_PRIVATE_OUTPUT\n',
                 PREFIX + json.dumps(entry(stage='catalog', action='failed', exception='TimeoutError')),
                 'cycle failed: timeout: check callback\n']
        process = SimpleNamespace(stdout=io.StringIO('\n'.join(lines)), wait=lambda: 1)
        self.console.watch(process, record)
        self.assertIn('阶段：目录回传', record['result'])
        self.assertIn('来源 IP 不符', record['result'])
        data = self.console.logs.read(day_name(), TASK, None)
        self.assertEqual(len(data['entries']), 3)
        self.assertNotIn('PRIVATE', json.dumps(data))
        self.assertTrue(all(row['task_id'] == TASK for row in data['entries']))
        self.assertEqual(data['entries'][-1]['exit_code'], 1)

    def test_failed_log_write_does_not_change_task_success(self):
        record = dict(id=TASK, new_files=0, started_at=time.time())
        with patch('diagnostics.os.open', side_effect=PermissionError(13, 'PRIVATE')):
            self.console.watch(SimpleNamespace(stdout=io.StringIO('cycle complete: 3 original files\n'), wait=lambda: 0), record)
        self.assertEqual(record['state'], 'success')
        self.assertEqual(record['new_files'], 3)
        self.assertIn('详细日志保存失败', self.console.snapshot()['diagnostics_problem'])


if __name__ == '__main__':
    unittest.main()
