import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from direct_exporter import RepairError, digest_file, export_cycle, export_one, repair_video
from web_settings import DEFAULTS, environment, validate

SOURCE = '/data_fs/record/20000101/010203.456.mp4'

class RepairTests(unittest.TestCase):
    def test_default_off_old_settings_and_both_modes(self):
        settings = dict(DEFAULTS, device_ip='192.168.1.2', callback_ip='192.168.1.3', serial='TEST')
        del settings['repair_video']
        self.assertFalse(validate(settings)['repair_video'])
        for mode in ('desktop', 'server'):
            self.assertEqual(environment(validate(settings), {'PAWZZ_MODE': mode})['PAWZZ_REPAIR_VIDEO'], 'no')
            enabled = validate(dict(settings, repair_video=True))
            self.assertEqual(environment(enabled, {'PAWZZ_MODE': mode})['PAWZZ_REPAIR_VIDEO'], 'yes')
        with self.assertRaises(ValueError):
            validate(dict(settings, repair_video='yes'))

    def test_missing_ffmpeg_fails_before_device_connection(self):
        with patch('direct_exporter.ffmpeg_path', return_value=None), patch('direct_exporter.DeviceClient') as device:
            with self.assertRaises(RepairError): export_cycle({'repair': True})
            device.assert_not_called()

    def test_existing_original_can_be_repaired_without_download(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            target = output / '20000101/010203.456.mp4'
            target.parent.mkdir(); target.write_bytes(b'original')
            digest = digest_file(target)
            target.with_suffix('.mp4.json').write_text(json.dumps(dict(source=SOURCE, bytes=8,
                md5_before=digest, md5_after=digest)))
            with patch('direct_exporter.repair_video') as repair, patch('direct_exporter.request') as request:
                self.assertFalse(export_one(None, dict(output=output), SOURCE))
                repair.assert_not_called()
                self.assertFalse(export_one(None, dict(output=output, repair=True), SOURCE))
                repair.assert_called_once_with(target, output)
                request.assert_not_called()

    def test_failure_and_hash_mismatch_preserve_original(self):
        for fail in (True, False):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as folder:
                output=Path(folder); original=output/'source.mp4'; original.write_bytes(b'original')
                calls=[]
                def run(args, **kwargs):
                    calls.append(args)
                    if fail:
                        raise subprocess.CalledProcessError(1, args)
                    if '-movflags' in args:
                        Path(args[-1]).write_bytes(b'remux')
                        return subprocess.CompletedProcess(args, 0, b'')
                    return subprocess.CompletedProcess(args, 0, str(len(calls)).encode())
                with patch('direct_exporter.ffmpeg_path', return_value='/test/ffmpeg'), patch('direct_exporter.subprocess.run', side_effect=run):
                    with self.assertRaises(RepairError): repair_video(original, output)
                self.assertEqual(original.read_bytes(), b'original')
                self.assertFalse((output/'repaired/source.mp4').exists())
                self.assertEqual(list(output.rglob('*.part')), [])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg not installed')
    def test_real_remux_preserves_packets_and_skips_verified_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder); original=output/'source.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=size=32x48:rate=12',
                '-f','lavfi','-i','sine=frequency=440','-t','0.5','-c:v','libx264','-c:a','aac',str(original)], check=True)
            before=original.read_bytes()
            repair_video(original, output)
            target=output/'repaired/source.mp4'
            def packets(path):
                result=subprocess.check_output(['ffprobe','-v','error','-show_packets','-show_data_hash','sha256',
                    '-show_entries','packet=stream_index,size,data_hash','-of','json',str(path)])
                return json.loads(result)['packets']
            self.assertEqual(packets(original), packets(target))
            self.assertEqual(original.read_bytes(), before)
            with patch('direct_exporter.subprocess.run') as run:
                repair_video(original, output)
                run.assert_not_called()
            target.write_bytes(b'corrupt')
            with self.assertRaises(RepairError): repair_video(original, output)
            self.assertEqual(target.read_bytes(), b'corrupt')
