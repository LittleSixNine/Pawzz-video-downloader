"""Small encrypted NAS credential file; its random key lives in a separate mount."""
import json
import os
from pathlib import Path
import tempfile

from cryptography.fernet import Fernet, InvalidToken
from account_login import credentials, LoginError


def key_location(value, config_folder):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise LoginError('登录加密密钥需要独立的绝对路径')
    path = path.resolve()
    for forbidden in (config_folder.resolve(), Path(__file__).resolve().parent.parent):
        if path == forbidden or forbidden in path.parents:
            raise LoginError('解密密钥必须放在配置目录与项目目录之外')
    return path


def read_key(path):
    if path.stat().st_size != 44 or path.stat().st_mode & 0o077:
        raise LoginError('加密密钥格式或权限无效，请检查独立密钥目录')
    return Fernet(path.read_bytes())


def save(path, key_path, value):
    value = credentials(value)
    # Never regenerate a missing key when ciphertext already exists.
    if path.exists():
        raise LoginError('已存在保存的登录状态，请先退出账号')
    if not key_path.exists():
        key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(key_path.parent, 0o700)
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(Fernet.generate_key())
    cipher = read_key(key_path).encrypt(json.dumps(value, separators=(',', ':')).encode())
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, name = tempfile.mkstemp(prefix='.session-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(cipher)
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def load(path, key_path):
    if path.stat().st_size > 16384:
        raise LoginError('加密登录文件大小无效')
    try:
        plain = read_key(key_path).decrypt(path.read_bytes())
        return credentials(json.loads(plain))
    except (InvalidToken, ValueError, UnicodeError):
        raise LoginError('保存的登录状态无法解密；请检查原密钥或退出后重新登录') from None
