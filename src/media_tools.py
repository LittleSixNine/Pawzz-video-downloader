"""Locate the packaged FFmpeg, with a checked system executable as fallback."""
from functools import lru_cache
from pathlib import Path
import os
import platform
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent.parent
MISSING = 'FFmpeg 不可用，无法开启无损修复；请使用完整发行包或安装 FFmpeg。原片导出仍可使用。'


@lru_cache(maxsize=8)
def _inspect(path, modified, size):
    try:
        version = subprocess.run([path, '-version'], check=True, capture_output=True,
                                 text=True, timeout=5).stdout
        formats = subprocess.run([path, '-hide_banner', '-formats'], check=True,
                                 capture_output=True, text=True, timeout=5).stdout
        match = re.search(r'^ffmpeg version ([A-Za-z0-9._+~-]{1,64})(?:\s|$)', version)
        required = {'mp4': 'E', 'streamhash': 'E', 'mov': 'D'}
        for line in formats.splitlines():
            entry = re.match(r'^\s*([D .E]{2})\s+(\S+)\s', line)
            if entry:
                for name in entry[2].split(','):
                    if name in required and required[name] in entry[1]:
                        required.pop(name)
        return match[1] if match and not required else None
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def _resolve():
    candidates = []
    if platform.system() == 'Darwin':
        arch = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x86_64', 'amd64': 'x86_64'}.get(platform.machine().lower())
        if arch:
            candidates.append((ROOT / 'platforms/desktop/ffmpeg' / ('macos-' + arch) / 'ffmpeg', 'bundled'))
    system = shutil.which('ffmpeg')
    if system:
        candidates.append((Path(system), 'system'))
    for path, source in candidates:
        try:
            if not path.is_file() or not os.access(path, os.X_OK):
                continue
            info = path.stat()
            version = _inspect(str(path), info.st_mtime_ns, info.st_size)
            if version:
                return str(path), dict(available=True, source=source, version=version,
                                       message=('随包内置' if source == 'bundled' else '系统提供') + ' FFmpeg ' + version + '，可无损修复')
        except OSError:
            continue
    return None, dict(available=False, source=None, version=None, message=MISSING)


def ffmpeg_path():
    return _resolve()[0]


def ffmpeg_status():
    return _resolve()[1]
