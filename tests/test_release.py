import hashlib
from pathlib import Path
import tempfile
import unittest
import zipfile

from tools.build_release import build_release, release_files, ROOT


class ReleaseTests(unittest.TestCase):
    def test_platform_files_separated_shared_code_identical_and_no_private_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'source'; root.mkdir()
            for name in ['LICENSE', 'README.md', 'SECURITY.md', 'requirements.txt', '.dockerignore', 'start.command',
                         'src/core.py', 'web/settings.html', 'web/settings.js', 'web/settings.css', 'docs/desktop.md',
                         'tools/build_macos_ffmpeg.sh', 'platforms/docker/Dockerfile', 'platforms/docker/compose.yaml',
                         'platforms/docker/.env.example', 'platforms/docker/deploy/compose.once.yaml',
                         'platforms/desktop/start.command', 'platforms/desktop/ffmpeg/NOTICE.md',
                         'platforms/desktop/ffmpeg/COPYING.LGPLv2.1', 'platforms/desktop/ffmpeg/source/ffmpeg-9.0.2.tar.xz',
                         'platforms/desktop/ffmpeg/macos-arm64/ffmpeg', 'platforms/desktop/ffmpeg/macos-x86_64/ffmpeg',
                         '.env', 'settings.json', 'src/notify.json', 'web/settings.backup', 'platforms/desktop/local.json']:
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(name)
            archives = build_release(Path(folder) / 'out', 'test', root)
            contents = []
            for archive in archives:
                with zipfile.ZipFile(archive) as package:
                    prefix = package.namelist()[0].split('/')[0] + '/'
                    files = {name[len(prefix):]: package.read(name) for name in package.namelist()}
                    for entry in package.infolist():
                        expected = 0o755 if entry.filename.endswith(('.command', '.sh', '/ffmpeg')) else 0o644
                        self.assertEqual((entry.external_attr >> 16) & 0o777, expected)
                    for line in files['CONTENTS.sha256'].decode().splitlines():
                        digest, name = line.split('  ', 1)
                        self.assertEqual(hashlib.sha256(files[name]).hexdigest(), digest)
                    self.assertTrue(set(files).isdisjoint({'.env', 'settings.json', 'src/notify.json', 'web/settings.backup', 'platforms/desktop/local.json'}))
                    contents.append(files)
            desktop, docker = contents
            self.assertIn('platforms/desktop/ffmpeg/macos-arm64/ffmpeg', desktop)
            self.assertNotIn('Dockerfile', desktop)
            self.assertIn('Dockerfile', docker)
            self.assertFalse(any(name.startswith('platforms/desktop') for name in docker))
            self.assertEqual(desktop['src/core.py'], docker['src/core.py'])
            (root / 'platforms/desktop/ffmpeg/macos-arm64/ffmpeg').unlink()
            with self.assertRaises(FileNotFoundError):
                release_files(root, 'desktop')

    def test_source_compatibility_links_point_to_platform_files(self):
        for name in ['Dockerfile', 'compose.yaml', '.env.example']:
            self.assertEqual((ROOT / name).resolve(), (ROOT / 'platforms/docker' / name).resolve())
        self.assertEqual((ROOT / 'deploy').resolve(), (ROOT / 'platforms/docker/deploy').resolve())
