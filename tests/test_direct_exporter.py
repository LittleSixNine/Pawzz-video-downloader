import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import URLError
from types import SimpleNamespace

from direct_exporter import request, retry_read, export_cycle, export_one, source_path, verified_existing, LowStorageError, MIN_FREE_BYTES

SOURCE = '/data_fs/record/20000101/010203.456.mp4'

class OriginalExportTests(unittest.TestCase):
    def test_reject_shell_injection_and_bad_date(self):
        for source in [SOURCE+';id', '/data_fs/record/20001301/010203.456.mp4', SOURCE+'/../x']:
            with self.assertRaises(ValueError):source_path(source)

    def transfer(self, receiver, data, after):
        base=f'http://127.0.0.1:{receiver.server.server_port}/{receiver.key}'
        before=hashlib.md5(data).hexdigest()
        with urlopen(Request(base+'/file', data=data, method='PUT', headers={'X-Source-MD5':before,'X-Source-MTime':str(int(time.time())-3600)})) as response:
            self.assertEqual(response.status,200)
        with urlopen(Request(base+'/done', data=after.encode(), method='POST')) as response:
            self.assertEqual(response.status,200)
        receiver.wait(2)

    def test_real_http_transfer_and_verified_skip(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg=dict(output=Path(folder),device='127.0.0.1',bind='127.0.0.1',port=0)
            data=b'original file bytes'*10000
            digest=hashlib.md5(data).hexdigest()
            with patch('direct_exporter.request', side_effect=lambda d,c,r,o:self.transfer(r,data,digest)) as transfer:
                self.assertTrue(export_one(None,cfg,SOURCE))
                self.assertFalse(export_one(None,cfg,SOURCE))
                self.assertEqual(transfer.call_count,1)
            target=Path(folder)/'20000101/010203.456.mp4'
            self.assertEqual(target.read_bytes(),data)
            target.write_bytes(b'x'*len(data))
            self.assertFalse(verified_existing(target,SOURCE))
            with self.assertRaises(RuntimeError):export_one(None,cfg,SOURCE)

    def test_recent_file_skip_leaves_no_file(self):
        def skip(device,cfg,receiver,operation):
            url=f'http://127.0.0.1:{receiver.server.server_port}/{receiver.key}/done'
            with urlopen(Request(url,data=b'SKIP',method='POST')) as response:
                self.assertEqual(response.status,200)
            receiver.wait(2)
        with tempfile.TemporaryDirectory() as folder:
            cfg=dict(output=Path(folder),device='127.0.0.1',bind='127.0.0.1',port=0)
            with patch('direct_exporter.request',side_effect=skip):
                self.assertFalse(export_one(None,cfg,SOURCE))
            self.assertEqual(list(Path(folder).rglob('*.mp4')),[])

    def test_changed_source_does_not_publish_partial(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg=dict(output=Path(folder),device='127.0.0.1',bind='127.0.0.1',port=0)
            with patch('direct_exporter.request',side_effect=lambda d,c,r,o:self.transfer(r,b'old bytes','0'*32)):
                with self.assertRaises(ValueError):export_one(None,cfg,SOURCE)
            self.assertEqual(list(Path(folder).rglob('*.mp4')),[])
            self.assertEqual(list(Path(folder).rglob('*.part')),[])

    def test_space_reserve_rejects_large_file_and_midstream_drop(self):
        # Both a known-size file and space consumed by another app must stop safely.
        data = b'x' * (2 * 1024**2)
        for free_values in ([MIN_FREE_BYTES + len(data) - 1],
                            [MIN_FREE_BYTES + len(data), MIN_FREE_BYTES + len(data), 1]):
            with self.subTest(free_values=free_values), tempfile.TemporaryDirectory() as folder:
                cfg = dict(output=Path(folder), device='127.0.0.1', bind='127.0.0.1', port=0)
                keep = Path(folder) / 'existing.mp4'
                keep.write_bytes(b'keep this')
                def transfer(_device, _cfg, receiver, _operation):
                    url = f'http://127.0.0.1:{receiver.server.server_port}/{receiver.key}/file'
                    with patch('direct_exporter.shutil.disk_usage',
                               side_effect=[SimpleNamespace(free=n) for n in free_values]):
                        # Early rejection may close the socket before the body is sent.
                        with self.assertRaises(URLError):
                            urlopen(Request(url, data=data, method='PUT',
                                            headers={'X-Source-MD5': hashlib.md5(data).hexdigest()}))
                        with self.assertRaises(LowStorageError):
                            receiver.wait(2)
                    raise LowStorageError()
                with patch('direct_exporter.request', side_effect=transfer):
                    with self.assertRaises(LowStorageError):
                        export_one(None, cfg, SOURCE)
                self.assertEqual(keep.read_bytes(), b'keep this')
                self.assertFalse((Path(folder) / '20000101/010203.456.mp4').exists())
                self.assertEqual(list(Path(folder).rglob('*.part')), [])

    def test_batch_catalog_selects_finished_files_and_skips_future_file(self):
        class FakeDevice:
            def __init__(self, *args): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass

        class FakeReceiver:
            def __init__(self, *args): self.data = b''
            def __enter__(self): return self
            def __exit__(self, *args): pass

        finished = [
            '/data_fs/record/20000101/010203.456.mp4',
            '/data_fs/record/20000101/060000.000.mp4',
        ]
        future = (datetime.now() + timedelta(hours=1)).strftime(
            '/data_fs/record/%Y%m%d/%H%M%S.000.mp4'
        )
        catalog = b'CATALOG' + bytes([10]) + bytes([10]).join(
            source.encode() for source in finished + [future]
        ) + bytes([10])
        exported = []

        def respond(_device, _config, receiver, _operation):
            receiver.data = catalog

        with patch('direct_exporter.read_session', return_value=('test-user', 'test-token')), \
             patch('direct_exporter.DeviceClient', FakeDevice), \
             patch('direct_exporter.publish'), \
             patch('direct_exporter.Receiver', FakeReceiver), \
             patch('direct_exporter.request', side_effect=respond), \
             patch('direct_exporter.export_one',
                   side_effect=lambda _device, _cfg, source: exported.append(source) or True):
            count = export_cycle({
                'session': Path('unused'), 'serial': 'unused',
                'device': '127.0.0.1', 'bind': '127.0.0.1', 'port': 0,
                'source': '', 'output': Path('.'), 'start': '',
            })

        self.assertEqual(count, 2)
        self.assertCountEqual(exported, finished)



class RetryTests(unittest.TestCase):
    transfer = OriginalExportTests.transfer
    def test_timeout_retry_keeps_device_and_cleans_partial(self):
        data=b'verified retry original'; digest=hashlib.md5(data).hexdigest()
        with tempfile.TemporaryDirectory() as folder:
            cfg=dict(output=Path(folder),session=Path('unused'),serial='test',source=SOURCE,
                     start='',device='127.0.0.1',bind='127.0.0.1',port=0)
            attempts=[]
            def respond(device,cfg,receiver,operation):
                attempts.append(receiver.key)
                if len(attempts)==1:
                    receiver.destination.write_bytes(b'incomplete')
                    raise TimeoutError()
                self.transfer(receiver,data,digest)
            with patch('direct_exporter.read_session',return_value=('test','test')), \
                 patch('direct_exporter.DeviceClient') as client, \
                 patch('direct_exporter.request',side_effect=respond), \
                 patch('direct_exporter.publish') as close, patch('direct_exporter.time.sleep') as sleep:
                self.assertEqual(export_cycle(cfg),1)
                self.assertEqual(client.call_count,1)
                self.assertEqual(len(set(attempts)),2)
                sleep.assert_called_once_with(5)
                close.assert_called_once_with(client.return_value.__enter__.return_value,'disenable_remote_telnet',{})
            target=Path(folder)/'20000101/010203.456.mp4'
            self.assertTrue(verified_existing(target,SOURCE))
            self.assertEqual(list(Path(folder).rglob('*.part')),[])

    def test_request_timeout_does_not_close_channel(self):
        from unittest.mock import Mock
        receiver=Mock(key='test'); receiver.wait.side_effect=TimeoutError()
        with patch('direct_exporter.publish') as publish:
            with self.assertRaises(TimeoutError):
                request(Mock(user_id='test'),{'advertise':'192.168.1.2','port':10003},receiver,lambda b,c:'true')
        self.assertEqual([call.args[1] for call in publish.call_args_list],['enable_remote_telnet'])

    def test_failed_and_stopped_batch_closes_channel_once(self):
        cases=[([TimeoutError()]*4,TimeoutError,4),([KeyboardInterrupt()],KeyboardInterrupt,1),
               ([ValueError('checksum mismatch')],ValueError,1),([LowStorageError()],LowStorageError,1)]
        for outcomes,error,attempts in cases:
            with self.subTest(error=error),tempfile.TemporaryDirectory() as folder:
                cfg=dict(output=Path(folder),session=Path('unused'),serial='test',source=SOURCE,
                         start='',device='127.0.0.1',bind='127.0.0.1',port=0)
                with patch('direct_exporter.read_session',return_value=('test','test')), \
                     patch('direct_exporter.DeviceClient'),patch('direct_exporter.export_one',side_effect=outcomes) as export, \
                     patch('direct_exporter.publish') as close,patch('direct_exporter.time.sleep'):
                    with self.assertRaises(error):export_cycle(cfg)
                    self.assertEqual(export.call_count,attempts)
                    self.assertEqual(close.call_count,1)
                    self.assertEqual(close.call_args.args[1],'disenable_remote_telnet')

    def test_catalog_timeout_retries_with_fresh_receiver(self):
        from unittest.mock import MagicMock
        first,second=MagicMock(),MagicMock()
        second.__enter__.return_value.data=('CATALOG\n'+SOURCE+'\n').encode()
        with tempfile.TemporaryDirectory() as folder:
            cfg=dict(output=Path(folder),session=Path('unused'),serial='test',source='',
                     start='',device='127.0.0.1',bind='127.0.0.1',port=0)
            with patch('direct_exporter.read_session',return_value=('test','test')), \
                 patch('direct_exporter.DeviceClient'),patch('direct_exporter.Receiver',side_effect=[first,second]) as receivers, \
                 patch('direct_exporter.request',side_effect=[TimeoutError(),None]) as requests, \
                 patch('direct_exporter.export_one',return_value=True) as export, \
                 patch('direct_exporter.publish') as close,patch('direct_exporter.time.sleep'):
                self.assertEqual(export_cycle(cfg),1)
                self.assertEqual(receivers.call_count,2)
                self.assertEqual(requests.call_count,2)
                export.assert_called_once()
                self.assertEqual(close.call_count,1)

    def test_retry_limit_and_non_timeout_fail_closed(self):
        from unittest.mock import Mock
        operation=Mock(side_effect=TimeoutError())
        with patch('direct_exporter.time.sleep') as sleep:
            with self.assertRaises(TimeoutError):retry_read(operation)
            self.assertEqual(operation.call_count,4)
            self.assertEqual(sleep.call_count,3)
        for error in (ValueError('checksum mismatch'),LowStorageError(),KeyboardInterrupt()):
            operation=Mock(side_effect=error)
            with patch('direct_exporter.time.sleep') as sleep:
                with self.assertRaises(type(error)):retry_read(operation)
                self.assertEqual(operation.call_count,1)
                sleep.assert_not_called()

if __name__=='__main__':unittest.main()
