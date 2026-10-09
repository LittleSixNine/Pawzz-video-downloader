"""Small encrypted NAS credential file; its random key lives in a separate mount."""
import json
import os
from pathlib import Path
import tempfile
import errno
from contextlib import contextmanager

from cryptography.fernet import Fernet, InvalidToken
from account_login import credentials, LoginError


@contextmanager
def file_access(step, message):
    try:
        yield
    except OSError as error:
        code = errno.errorcode.get(error.errno, 'IO_ERROR')
        # Never log exception text: it can include a private path or credential.
        print(f'file access failed: step={step} errno={code}', flush=True)
        raise LoginError(f'{message}（{code}）') from None


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
    with file_access('login_lookup', '无法检查已有登录文件，请检查登录目录访问权限；不会覆盖已有文件'):
        existing = path.exists()
    if existing:
        raise LoginError('已存在保存的登录状态，请先退出账号')
    with file_access('key_lookup', '无法访问密钥目录，请检查运行用户、目录所有者与权限'):
        key_exists = key_path.exists()
    if not key_exists:
        with file_access('key_directory', '密钥目录无法创建或访问，请检查密钥挂载与目录权限'):
            key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with file_access('key_permissions', '无法收紧密钥目录权限，请让目录所有者与程序运行用户一致'):
            os.chmod(key_path.parent, 0o700)
        with file_access('key_create', '无法创建加密密钥，请检查密钥目录写权限与磁盘空间'):
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(Fernet.generate_key())
    with file_access('key_read', '无法读取原加密密钥，请检查密钥文件权限；不要删除或替换原密钥'):
        cipher = read_key(key_path).encrypt(json.dumps(value, separators=(',', ':')).encode())
    with file_access('login_directory', '登录保存目录无法创建，请检查配置目录写权限'):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_access('login_permissions', '无法收紧登录目录权限，请让目录所有者与程序运行用户一致'):
        os.chmod(path.parent, 0o700)
    with file_access('login_write', '加密登录文件保存失败，请检查配置目录写权限与磁盘空间'):
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
