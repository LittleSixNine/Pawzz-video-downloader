"""Create separate, allowlisted desktop/Docker source distributions (no user data)."""
import argparse
import hashlib
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent
COMMON = ['LICENSE', 'README.md', 'SECURITY.md', 'requirements.txt']


def release_files(root, kind):
    files = {name: root / name for name in COMMON}
    for directory, pattern in [('src', '*.py'), ('docs', '*.md')]:
        for path in (root / directory).glob(pattern):
            files[path.relative_to(root).as_posix()] = path
    for name in ['settings.html', 'settings.js', 'settings.css']:
        files['web/' + name] = root / 'web' / name
    if kind == 'desktop':
        files['start.command'] = root / 'start.command'
        files['tools/build_macos_ffmpeg.sh'] = root / 'tools/build_macos_ffmpeg.sh'
        desktop = root / 'platforms/desktop'
        required = ['start.command', 'ffmpeg/NOTICE.md', 'ffmpeg/COPYING.LGPLv2.1',
                    'ffmpeg/source/ffmpeg-9.0.2.tar.xz', 'ffmpeg/macos-arm64/ffmpeg',
                    'ffmpeg/macos-x86_64/ffmpeg']
        for name in required:
            path = desktop / name
            if not path.is_file():
                raise FileNotFoundError('Build the bundled FFmpeg first: ' + name)
            files['platforms/desktop/' + name] = path
        for name in ['ffmpeg-9.0.2.tar.xz.asc', 'release-key.asc']:
            path = desktop / 'ffmpeg/source' / name
            if path.is_file():
                files['platforms/desktop/ffmpeg/source/' + name] = path
    else:
        files['.dockerignore'] = root / '.dockerignore'
        docker = root / 'platforms/docker'
        for name in ['Dockerfile', 'compose.yaml', '.env.example']:
            files[name] = docker / name
        for path in (docker / 'deploy').glob('*.yaml'):
            files['deploy/' + path.name] = path
    return files


def build_release(destination, version, root=ROOT):
    results = []
    for kind in ('desktop', 'docker'):
        files = release_files(root, kind)
        folder = destination / kind
        folder.mkdir(parents=True, exist_ok=True)
        archive = folder / ('pawzz-video-' + kind + '-' + version + '.zip')
        prefix = 'pawzz-video-' + kind + '/'
        hashes = []
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
            for name, path in sorted(files.items()):
                data = path.read_bytes()
                hashes.append(hashlib.sha256(data).hexdigest() + '  ' + name)
                info = zipfile.ZipInfo(prefix + name, (2026, 10, 28, 0, 0, 0))
                info.create_system = 3
                executable = name.endswith('.command') or name.endswith('.sh') or name.endswith('/ffmpeg')
                info.external_attr = (0o100755 if executable else 0o100644) << 16
                output.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
            info = zipfile.ZipInfo(prefix + 'CONTENTS.sha256', (2026, 10, 28, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            output.writestr(info, '\n'.join(hashes) + '\n')
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        archive.with_suffix('.zip.sha256').write_text(digest + '  ' + archive.name + '\n')
        results.append(archive)
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--version', default='2026.10.28')
    args = parser.parse_args()
    if not args.version or any(c not in '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-_' for c in args.version):
        parser.error('invalid version')
    for archive in build_release(args.destination.resolve(), args.version):
        print(archive)
