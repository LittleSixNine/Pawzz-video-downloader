import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import media_tools


class MediaToolsTests(unittest.TestCase):
    def setUp(self):
        media_tools._inspect.cache_clear()

    def test_prefers_matching_bundle_then_checked_system_and_never_exposes_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bundle = root / 'platforms/desktop/ffmpeg/macos-arm64/ffmpeg'
            bundle.parent.mkdir(parents=True)
            bundle.write_text('test'); bundle.chmod(0o755)
            system = root / 'system-ffmpeg'; system.write_text('test'); system.chmod(0o755)
            with patch('media_tools.ROOT', root), patch('media_tools.platform.system', return_value='Darwin'), \
                 patch('media_tools.platform.machine', return_value='arm64'), \
                 patch('media_tools.shutil.which', return_value=str(system)), \
                 patch('media_tools._inspect', return_value='9.0.2'):
                self.assertEqual(media_tools.ffmpeg_path(), str(bundle))
                status = media_tools.ffmpeg_status()
                self.assertEqual(status['source'], 'bundled')
                self.assertNotIn(folder, str(status))
                bundle.chmod(0o644)
                self.assertEqual(media_tools.ffmpeg_path(), str(system))
                self.assertEqual(media_tools.ffmpeg_status()['source'], 'system')

    def test_checks_streamcopy_formats_and_caches_probes(self):
        version = subprocess.CompletedProcess([], 0, 'ffmpeg version 9.0.2 Copyright')
        formats = subprocess.CompletedProcess([], 0, ' D   mov,mp4,m4a QuickTime\n  E  mp4 MP4\n  E  streamhash Hash\n')
        with patch('media_tools.subprocess.run', side_effect=[version, formats]) as run:
            self.assertEqual(media_tools._inspect('/test/ffmpeg', 1, 10), '9.0.2')
            self.assertEqual(media_tools._inspect('/test/ffmpeg', 1, 10), '9.0.2')
            self.assertEqual(run.call_count, 2)
        with patch('media_tools.subprocess.run', side_effect=[version, subprocess.CompletedProcess([], 0, '  E  mp4 MP4\n')]):
            self.assertIsNone(media_tools._inspect('/test/ffmpeg', 2, 10))
        with patch('media_tools.subprocess.run', side_effect=OSError('PRIVATE_PATH')):
            self.assertIsNone(media_tools._inspect('/test/ffmpeg', 3, 10))

    def test_linux_uses_system_and_unavailable_status_is_safe(self):
        with patch('media_tools.platform.system', return_value='Linux'), patch('media_tools.shutil.which', return_value=None):
            self.assertIsNone(media_tools.ffmpeg_path())
            self.assertFalse(media_tools.ffmpeg_status()['available'])
            self.assertIn('原片导出仍可使用', media_tools.ffmpeg_status()['message'])
