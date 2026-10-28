"""Bounded, credential-free task events shared by workers and the console."""
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import threading
import time

PREFIX = '@pawzz-event '
ZONE = timezone(timedelta(hours=8))
STAGES = {'task': '任务', 'session': '读取登录状态', 'storage': '存储检查',
          'mac_discovery': 'MAC 地址查找', 'mqtt': 'MQTT 连接', 'catalog': '目录回传',
          'transfer': '原片传输', 'checksum': '原片校验', 'repair': '无损修复', 'cleanup': '关闭维护通道'}
ACTIONS = {'start', 'success', 'failed', 'retry', 'waiting', 'command_sent',
           'callback_received', 'callback_rejected', 'subscribed', 'disconnected', 'stopped', 'paused', 'interrupted'}
ERRORS = {'ValueError', 'KeyError', 'TypeError', 'OSError', 'FileNotFoundError', 'PermissionError',
          'TimeoutError', 'RuntimeError', 'AuthenticationError', 'DiscoveryError', 'RepairError',
          'LowStorageError', 'CalledProcessError', 'TimeoutExpired', 'ConnectionError',
          'ConnectionResetError', 'ConnectionRefusedError', 'ConnectionAbortedError', 'BrokenPipeError',
          'HTTPError', 'URLError', 'SSLError', 'SSLCertVerificationError', 'gaierror',
          'JSONDecodeError', 'UnicodeDecodeError', 'Exception'}
NUMBERS = {'duration_ms', 'attempt', 'max_attempts', 'count', 'bytes', 'errno', 'http_status',
           'exit_code', 'retry_attempt'}
MAX_BYTES = 2 * 1024 * 1024
PARTS = 3
KEEP_DAYS = 14
ID = re.compile(r'[0-9a-f]{16}')
EMIT_LOCK = threading.Lock()


def clean_event(value, task_id=None):
    if not isinstance(value, dict):
        raise ValueError('invalid diagnostic event')
    task = task_id if task_id is not None else value.get('task_id', '')
    stamp = value.get('time')
    if (not isinstance(task, str) or not ID.fullmatch(task)
            or value.get('stage') not in STAGES or value.get('action') not in ACTIONS
            or type(stamp) not in (int, float) or not math.isfinite(stamp)
            or not 946684800 <= stamp <= 4102444800):
        raise ValueError('invalid diagnostic event')
    result = dict(time=stamp, task_id=task, stage=value['stage'], action=value['action'])
    for key in NUMBERS:
        if type(value.get(key)) is int and -255 <= value[key] <= 10**15:
            result[key] = value[key]
    if value.get('exception') in ERRORS:
        result['exception'] = value['exception']
    if value.get('reason') in ('source_mismatch', 'route_mismatch'):
        result['reason'] = value['reason']
    if isinstance(value.get('root_task_id'), str) and ID.fullmatch(value['root_task_id']):
        result['root_task_id'] = value['root_task_id']
    return result


def error_fields(error):
    name = type(error).__name__
    result = {'exception': name if name in ERRORS else 'Exception'}
    for key, attribute in (('errno', 'errno'), ('http_status', 'code')):
        if type(getattr(error, attribute, None)) is int:
            result[key] = getattr(error, attribute)
    return result


def event(stage, action, **details):
    task = os.environ.get('PAWZZ_TASK_ID', '')
    if not ID.fullmatch(task):
        return
    value = clean_event(dict(details, time=time.time(), task_id=task, stage=stage, action=action))
    with EMIT_LOCK:
        print(PREFIX + json.dumps(value, separators=(',', ':')), flush=True)


@contextmanager
def phase(stage):
    started = time.monotonic()
    event(stage, 'start')
    try:
        yield
    except Exception as error:
        event(stage, 'failed', duration_ms=round((time.monotonic() - started) * 1000), **error_fields(error))
        raise
    else:
        event(stage, 'success', duration_ms=round((time.monotonic() - started) * 1000))


def day_name(stamp=None):
    return datetime.fromtimestamp(time.time() if stamp is None else stamp, ZONE).date().isoformat()


def check_day(day):
    if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        raise ValueError('invalid diagnostic date')
    datetime.strptime(day, '%Y-%m-%d')
    return day


class DailyLogs:
    def __init__(self, folder):
        self.folder = Path(folder) / 'logs'
        self.lock = threading.RLock()
        self.problem = None
        self.last_day = None

    def write(self, value):
        value = clean_event(value)
        data = (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
        day = day_name(value['time'])
        try:
            with self.lock:
                self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.folder.chmod(0o700)
                if self.last_day != day:
                    cutoff = (datetime.strptime(day, '%Y-%m-%d').date() - timedelta(days=KEEP_DAYS - 1)).isoformat()
                    for path in self.folder.iterdir():
                        if re.fullmatch(r'\d{4}-\d{2}-\d{2}\.jsonl(?:\.[12])?', path.name) and path.name[:10] < cutoff:
                            path.unlink()
                    self.last_day = day
                path = self.folder / (day + '.jsonl')
                if path.exists() and path.stat().st_size + len(data) > MAX_BYTES:
                    for index in range(PARTS - 1, 0, -1):
                        previous = Path(str(path) + ('.' + str(index - 1) if index > 1 else ''))
                        if previous.exists():
                            previous.replace(Path(str(path) + '.' + str(index)))
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'ab') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(data)
                self.problem = None
            return True
        except OSError:
            self.problem = '详细日志保存失败，请检查配置目录权限与磁盘空间；导出结果请以任务历史为准'
            return False

    def dates(self):
        with self.lock:
            if not self.folder.exists():
                return []
            return sorted({path.name[:10] for path in self.folder.iterdir()
                           if re.fullmatch(r'\d{4}-\d{2}-\d{2}\.jsonl(?:\.[12])?', path.name)}, reverse=True)

    def read(self, day, task='', limit=200):
        check_day(day)
        if task and not ID.fullmatch(task):
            raise ValueError('invalid task id')
        entries = deque(maxlen=limit)
        count = 0
        with self.lock:
            base = self.folder / (day + '.jsonl')
            for part in range(PARTS - 1, -1, -1):
                path = Path(str(base) + ('.' + str(part) if part else ''))
                if not path.exists() or path.is_symlink():
                    continue
                with path.open('rb') as stream:
                    # Bound reads even if a file was enlarged outside this process.
                    size = path.stat().st_size
                    if size > MAX_BYTES:
                        stream.seek(size - MAX_BYTES)
                        stream.readline()
                    for line in stream:
                        try:
                            value = clean_event(json.loads(line))
                        except (ValueError, TypeError):
                            continue
                        if not task or value['task_id'] == task:
                            entries.append(value)
                            count += 1
        return dict(entries=list(entries), truncated=count > len(entries), count=count)
