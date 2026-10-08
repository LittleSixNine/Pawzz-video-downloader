"""Small authenticated LAN console; export work stays in a separate process."""
from __future__ import annotations
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import ssl
import tempfile
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from direct_exporter import config, require_storage, LowStorageError, MIN_FREE_BYTES
from synology_exporter import INTERVALS
import account_login
import credential_store
from account_login import LoginError

DEFAULTS = dict(device_ip='', callback_ip='', callback_port=10003, serial='',
                start_date='', source='', interval='1d', allow_maintenance=False,
                automatic=False)
FIELDS = dict(device_ip='PAWZZ_DEVICE_IP', callback_ip='PAWZZ_CALLBACK_IP',
              callback_port='PAWZZ_CALLBACK_PORT', serial='PAWZZ_SN',
              start_date='PAWZZ_START_DATE', source='PAWZZ_SOURCE', interval='PAWZZ_INTERVAL')
LAN = tuple(ipaddress.ip_network(x) for x in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
RETRY_DELAY = 600
MAX_RETRIES = 3


def validate(settings):
    if not isinstance(settings, dict) or set(settings) != set(DEFAULTS):
        raise ValueError('设置字段不完整或含不支持的字段')
    for name in ('allow_maintenance', 'automatic'):
        if type(settings[name]) is not bool:
            raise ValueError('开关值无效')
    if type(settings['callback_port']) is not int:
        raise ValueError('回调端口必须为整数')
    for name in FIELDS.keys() - {'callback_port'}:
        if not isinstance(settings[name], str) or len(settings[name]) > 256:
            raise ValueError('设置格式无效')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', settings['serial']):
        raise ValueError('请填写有效设备序列号（字母、数字、下划线或短横线）')
    try:
        for name in ('device_ip', 'callback_ip'):
            address = ipaddress.IPv4Address(settings[name])
            if not any(address in network for network in LAN):
                raise ValueError()
        config(environment(settings, {} , allow_for_validation=True))
    except (ValueError, KeyError, TypeError):
        raise ValueError('请检查私网 IPv4、端口、日期、源文件路径和周期') from None
    if settings['automatic'] and not settings['allow_maintenance']:
        raise ValueError('自动运行需要明确允许维护读取')
    return settings.copy()


def environment(settings, base, allow_for_validation=False):
    env = dict(base)
    env.update({target: str(settings[name]) for name, target in FIELDS.items()})
    env['PAWZZ_ALLOW_MAINTENANCE'] = 'yes' if settings['allow_maintenance'] or allow_for_validation else 'no'
    env['PAWZZ_ONCE'] = '1'
    env.setdefault('PAWZZ_SESSION_FILE', '/run/secrets/session.json')
    env.setdefault('PAWZZ_OUTPUT_DIR', '/videos')
    return env


def save_json(path, value):
    # Create with restrictive permissions before writing, then replace atomically.
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


class Console:
    def __init__(self, folder, base_env=None):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.env = dict(os.environ if base_env is None else base_env)
        self.mode = self.env.get('PAWZZ_MODE', 'server')
        if self.mode not in ('desktop', 'server'):
            raise ValueError('PAWZZ_MODE must be desktop or server')
        self.connect_code = None
        self.connect_deadline = 0
        if self.mode == 'desktop' and (folder / 'local.json').exists():
            saved = json.loads((folder / 'local.json').read_text())
            self.env['PAWZZ_OUTPUT_DIR'] = str(local_output(saved['output_dir']))
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.settings = DEFAULTS.copy()
        self.problem = None
        settings_path = folder / 'settings.json'
        if settings_path.exists():
            try:
                self.settings = validate(json.loads(settings_path.read_text()))
            except (ValueError, OSError):
                self.problem = '保存的设置无法读取，请重新保存；未启动导出'
        if self.mode == 'desktop':
            # Computer exports are always deliberate, even with an old server config.
            self.settings['automatic'] = False
        token_path = folder / 'admin-token'
        if not token_path.exists():
            with token_path.open('x') as stream:
                os.chmod(token_path, 0o600)
                stream.write(secrets.token_urlsafe(32))
        self.token = token_path.read_text().strip()
        if len(self.token) < 32 or not self.token.isascii():
            raise ValueError('admin-token must contain at least 32 ASCII characters')
        self.process = None
        self.account_session = None
        self.account_source = None
        self.account_problem = None
        self.logout_pending = False
        self.sms_phone = None
        self.sms_deadline = 0
        self.sms_next = 0
        self.sms_sends = []
        self.login_attempts = 0
        self.external_disabled = (folder / 'account-disabled').exists()
        self.account_file = folder / 'login' / 'session.enc'
        self.account_key = credential_store.key_location(self.env.get('PAWZZ_SESSION_KEY_FILE', '/keys/session.key'), folder)
        if self.mode == 'server' and self.account_file.exists():
            try:
                self.account_session = credential_store.load(self.account_file, self.account_key)
                self.account_source = 'saved'
            except (ValueError, OSError):
                self.account_problem = '保存的登录状态无法解密，请检查独立密钥；或退出账号后重新登录'
        self.last_exit = None
        self.last_result = '尚未运行'
        self.last_finished = None
        self.history = []
        self.history_problem = None
        self.history_valid = True
        history_path = folder / 'history.json'
        if history_path.exists():
            try:
                records = json.loads(history_path.read_text())
                if not isinstance(records, list) or len(records) > 100:
                    raise ValueError()
                for record in records:
                    if (not isinstance(record, dict)
                            or record.get('state') not in ('running', 'success', 'failed', 'stopped', 'interrupted', 'paused')
                            or not isinstance(record.get('started_at'), (int, float))
                            or not isinstance(record.get('result'), str)
                            or not isinstance(record.get('id'), str)):
                        raise ValueError()
                self.history = records
                changed = False
                for record in self.history:
                    if record['state'] == 'running':
                        record.update(state='interrupted', result='服务曾中断，上轮结果未确认',
                                      finished_at=None, exit_code=None, new_files=None)
                        changed = True
                if changed:
                    save_json(history_path, self.history)
                if self.history:
                    latest = self.history[0]
                    self.last_exit = latest.get('exit_code')
                    self.last_finished = latest.get('finished_at')
                    self.last_result = latest['result']
            except (ValueError, OSError):
                self.history = []
                self.history_valid = False
                self.history_problem = '任务历史无法读取，原文件已保留；修复后重启服务再运行'
        if self.settings['automatic'] and not self.session_ready():
            self.settings['automatic'] = False
            self.problem = '登录状态未保留，请登录后重新开启自动归档'
        self.next_run = time.time() if self.settings['automatic'] else None
        self.next_kind = 'scheduled' if self.next_run else None
        self.retry_count = 0
        self.monitor = None
        self.scheduler = threading.Thread(target=self.schedule, daemon=True)
        self.scheduler.start()

    def snapshot(self):
        with self.lock:
            self.expire_account()
            session = Path(self.env.get('PAWZZ_SESSION_FILE', '/run/secrets/session.json'))
            output = Path(self.env.get('PAWZZ_OUTPUT_DIR', '/videos'))
            storage = dict(available=False, writable=False, free=None, total=None)
            try:
                if output.is_dir():
                    usage = shutil.disk_usage(output)
                    storage.update(available=True, writable=os.access(output, os.W_OK),
                                   free=usage.free, total=usage.total)
            except OSError:
                pass
            try:
                validate(self.settings)
                configured = True
            except ValueError:
                configured = False
            return dict(mode=self.mode, settings=self.settings.copy(), running=self.process is not None,
                        last_exit=self.last_exit, last_result=self.last_result,
                        last_finished=self.last_finished, next_run=self.next_run,
                        session_ready=self.session_ready(),
                        account=dict(source=self.account_source or ('external' if self.session_ready() else None),
                                     saved=self.account_file.exists(), logout_pending=self.logout_pending,
                                     external_present=session.is_file(),
                                     temporary_seconds=None,
                                     sms_wait=max(0, int(self.sms_next - time.monotonic()) + 1),
                                     code_pending=self.sms_phone is not None),
                        problem=self.account_problem or self.problem or self.history_problem, output=str(output),
                        configured=configured, storage=storage, history_valid=self.history_valid,
                        next_kind=self.next_kind, retry_count=self.retry_count,
                        policy=dict(retry_seconds=RETRY_DELAY, max_retries=MAX_RETRIES,
                                    min_free_bytes=MIN_FREE_BYTES),
                        history=[record.copy() for record in self.history])

    def new_connect_code(self):
        with self.lock:
            self.connect_code = secrets.token_urlsafe(32)
            self.connect_deadline = time.monotonic() + 60
            return self.connect_code

    def connect(self, body):
        with self.lock:
            code = body.get('code') if isinstance(body, dict) else None
            if (self.mode != 'desktop' or not isinstance(code, str) or not self.connect_code
                    or time.monotonic() > self.connect_deadline
                    or not secrets.compare_digest(code.encode(), self.connect_code.encode())):
                raise ValueError('连接链接已失效，请使用管理密钥')
            self.connect_code = None
            return self.token

    def local_setup(self, body):
        with self.lock:
            if self.mode != 'desktop':
                raise ValueError('此操作仅适用于电脑版')
            if self.process is not None:
                raise RuntimeError('请先停止任务')
            if not isinstance(body, dict):
                raise ValueError('设置格式无效')
            if set(body) == {'output_dir'}:
                output = local_output(body['output_dir'])
                output.mkdir(parents=True, exist_ok=True)
                if not os.access(output, os.W_OK):
                    raise OSError('Output is not writable')
                save_json(self.folder / 'local.json', {'output_dir': str(output)})
                self.env['PAWZZ_OUTPUT_DIR'] = str(output)
            elif set(body) == {'session'}:
                self.install_session(account_login.credentials(body['session']), False)
            else:
                raise ValueError('不支持的本地设置')

    def session_ready(self):
        if self.logout_pending or self.account_problem:
            return False
        return self.account_session is not None or (not self.external_disabled and
            Path(self.env.get('PAWZZ_SESSION_FILE', '/run/secrets/session.json')).is_file())

    def expire_account(self):
        now = time.monotonic()
        if self.sms_phone and now >= self.sms_deadline:
            self.sms_phone = None

    def install_session(self, value, remember):
        if self.process is not None or self.logout_pending:
            raise RuntimeError('请先停止任务')
        if self.account_file.exists():
            raise LoginError('请先退出已保存的账号，再更换登录方式')
        # This marker contains no credential; it prevents fallback to an old mounted account.
        save_json(self.folder / 'account-disabled', True)
        self.external_disabled = True
        if remember:
            if self.mode != 'server':
                raise LoginError('电脑版不保存登录凭据')
            credential_store.save(self.account_file, self.account_key, value)
        self.account_session = value
        self.account_source = 'saved' if remember else 'temporary'
        self.account_problem = None
        self.sms_phone = None

    def account_action(self, action, body):
        # ponytail: one account per console; short vendor calls share the existing lock.
        with self.lock:
            self.expire_account()
            if action == 'logout':
                if body != {}:
                    raise LoginError('退出请求格式无效')
                save_json(self.folder / 'account-disabled', True)
                self.external_disabled = True
                self.stop()
                self.account_file.unlink(missing_ok=True)
                self.sms_phone = None
                self.account_problem = None
                self.logout_pending = self.process is not None
                if not self.logout_pending:
                    self.account_session = self.account_source = None
                return
            if self.process is not None or self.logout_pending:
                raise RuntimeError('请先停止任务')
            if self.session_ready() or self.account_file.exists():
                raise LoginError('请先退出当前账号再重新登录；已有外部会话文件不会被删除')
            if not isinstance(body, dict):
                raise LoginError('登录请求格式无效')
            now = time.monotonic()
            if action == 'send':
                if set(body) != {'phone'}:
                    raise LoginError('短信请求格式无效')
                phone = account_login.phone_number(body['phone'])
                self.sms_sends = [sent for sent in self.sms_sends if now - sent < 3600]
                if now < self.sms_next or len(self.sms_sends) >= 5:
                    raise LoginError('发送过于频繁，请稍后再试；每次间隔 60 秒，每小时最多 5 次')
                self.sms_next = now + 60
                self.sms_sends.append(now)
                self.sms_phone = None
                account_login.send_code(phone)
                self.sms_phone, self.sms_deadline, self.login_attempts = phone, now + 300, 0
            elif action == 'login':
                if set(body) != {'phone', 'code', 'remember'} or type(body['remember']) is not bool:
                    raise LoginError('登录请求格式无效')
                if body['remember'] and self.mode != 'server':
                    raise LoginError('电脑版不保存登录凭据')
                if (not self.sms_phone or body['phone'] != self.sms_phone or self.login_attempts >= 5):
                    raise LoginError('请先发送验证码；本次验证已失效或尝试次数已用完')
                self.login_attempts += 1
                value = account_login.login(body['phone'], body['code'])
                self.install_session(value, body['remember'])
            elif action == 'cancel':
                if body != {}:
                    raise LoginError('取消请求格式无效')
                self.sms_phone = None
            else:
                raise LoginError('登录操作不存在')

    def persist_history(self):
        try:
            save_json(self.folder / 'history.json', self.history)
            self.history_problem = None
        except OSError:
            self.history_problem = '任务历史保存失败，请检查配置目录权限与磁盘空间'

    def save(self, settings):
        settings = validate(settings)
        if self.mode == 'desktop' and settings['automatic']:
            raise ValueError('电脑版仅按需导出，不开启后台计划')
        if settings['callback_port'] == int(self.env.get('PAWZZ_WEB_PORT', '8080')):
            raise ValueError('回调端口不能与网页端口相同')
        with self.lock:
            if settings['automatic'] and not self.session_ready():
                raise ValueError('请先登录，再开启自动归档')
            if self.process is not None:
                raise RuntimeError('任务运行中，请先停止再保存设置')
            save_json(self.folder / 'settings.json', settings)
            self.settings = settings
            self.problem = None
            self.next_run = time.time() if settings['automatic'] else None
            self.next_kind = 'scheduled' if self.next_run else None
            self.retry_count = 0

    def schedule_next(self, failed=False, low_storage=False):
        self.next_run = self.next_kind = None
        if self.mode == 'desktop' or not self.settings['automatic'] or self.closed.is_set() or self.account_problem:
            self.retry_count = 0
            return
        if low_storage:
            self.next_kind, delay = 'storage', RETRY_DELAY
        elif failed and self.retry_count < MAX_RETRIES:
            self.retry_count += 1
            self.next_kind, delay = 'retry', RETRY_DELAY
        else:
            self.retry_count = 0
            self.next_kind, delay = 'scheduled', INTERVALS[self.settings['interval']]
        self.next_run = time.time() + delay

    def start(self, trigger='manual'):
        with self.lock:
            if self.closed.is_set() or self.process is not None:
                raise RuntimeError('已有任务运行或服务正在关闭')
            settings = validate(self.settings)
            if not settings['allow_maintenance']:
                raise ValueError('请先允许维护读取并保存设置')
            self.expire_account()
            env = environment(settings, self.env)
            if not self.session_ready():
                raise ValueError('请先登录账号')
            if not self.history_valid:
                raise ValueError('任务历史无法读取')
            output = Path(env['PAWZZ_OUTPUT_DIR'])
            output.mkdir(parents=True, exist_ok=True)
            try:
                require_storage(output)
            except LowStorageError:
                self.problem = '存储空间不足，归档已暂停；需保留至少 5 GB 可用空间'
                self.schedule_next(low_storage=True)
                raise
            if trigger != 'retry':
                self.retry_count = 0
            record = dict(id=secrets.token_hex(8), started_at=time.time(), finished_at=None,
                          trigger=trigger, retry_attempt=self.retry_count, state='running', new_files=0, exit_code=None,
                          result='正在执行', scope='指定原片' if settings['source'] else
                          ('自 ' + settings['start_date'] if settings['start_date'] else '全部日期'))
            # Persist before launching: a later restart can identify unfinished work.
            updated = [record] + self.history[:99]
            save_json(self.folder / 'history.json', updated)
            self.history = updated
            self.history_problem = None
            read_fd = write_fd = None
            try:
                if self.account_session:
                    read_fd, write_fd = os.pipe()
                    env['PAWZZ_SESSION_FD'] = str(read_fd)
                else:
                    env.pop('PAWZZ_SESSION_FD', None)
                self.process = subprocess.Popen(
                    [sys.executable, '-u', str(Path(__file__).with_name('direct_exporter.py'))],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    pass_fds=(read_fd,) if read_fd is not None else ())
                if write_fd is not None:
                    os.close(read_fd)
                    read_fd = None
                    with os.fdopen(write_fd, 'w') as pipe:
                        write_fd = None
                        json.dump(self.account_session, pipe)
            except OSError:
                if self.process is not None:
                    self.process.terminate()
                    self.process.wait(timeout=10)
                    self.process.stdout.close()
                    self.process = None
                record.update(state='failed', finished_at=time.time(), result='导出进程未能启动')
                self.last_result = record['result']
                self.last_exit = None
                self.last_finished = record['finished_at']
                self.persist_history()
                raise
            finally:
                for fd in (read_fd, write_fd):
                    if fd is not None:
                        os.close(fd)
            self.next_run = None
            self.next_kind = None
            self.problem = None
            self.last_result = '正在执行'
            self.monitor = threading.Thread(target=self.watch, args=(self.process, record), daemon=True)
            self.monitor.start()

    def watch(self, process, record):
        result = None
        # Never expose raw worker output, file names or credentials to logs/API.
        hints = {'missing_file': '文件缺失：检查会话挂载或输出目录',
                 'permission_denied': '权限不足：检查会话、输出目录与 NAS ACL',
                 'timeout': '连接或回调超时：检查地址、端口、防火墙与 MQTT',
                 'io_or_network': '磁盘或网络错误：检查空间、端口冲突、DNS 和 TLS',
                 'invalid_data': '配置或校验错误：检查设置、会话格式与文件完整性',
                 'operation_failed': '操作失败：检查 MQTT 访问和回调校验'}
        for line in process.stdout:
            if line.startswith('verified original:'):
                with self.lock:
                    record['new_files'] += 1
            if line.startswith(('cycle failed:', 'startup failed:')):
                result = next((label for code, label in hints.items() if code + ':' in line), '任务失败')
            match = re.fullmatch(r'cycle complete: (\d+) original files\n?', line)
            if match:
                result = f'本轮完成，新增 {match[1]} 个原片'
                with self.lock:
                    record['new_files'] = int(match[1])
        code = process.wait()
        process.stdout.close()
        with self.lock:
            self.process = None
            self.last_exit = code
            self.last_finished = time.time()
            self.last_result = ('已停止' if code in (130, -15) else result or
                                ('本轮完成' if code == 0 else '任务异常退出，请查看诊断与启动窗口'))
            if code == 4:
                self.account_problem = '厂商拒绝了登录凭据，任务已暂停；请退出账号后重新登录'
                self.last_result = self.account_problem
                self.settings['automatic'] = False
                try:
                    save_json(self.folder / 'settings.json', self.settings)
                except OSError:
                    self.account_problem += '；设置保存失败，重启前请检查配置目录'
            if code == 3:
                self.last_result = '空间不足以保留 5 GB 余量，归档已暂停；已有原片保留'
                self.problem = self.last_result
            record.update(finished_at=self.last_finished, exit_code=code, result=self.last_result,
                          state='success' if code == 0 else 'paused' if code == 3 else 'stopped' if code in (130, -15) else 'failed')
            if self.logout_pending or code == 4:
                self.account_session = self.account_source = None
                self.logout_pending = False
            self.persist_history()
            self.schedule_next(failed=record['state'] == 'failed', low_storage=code == 3)

    def stop(self):
        with self.lock:
            # Stopping also disables the schedule, so it cannot start a second job.
            if self.settings['automatic']:
                updated = dict(self.settings, automatic=False)
                save_json(self.folder / 'settings.json', updated)
                self.settings = updated
            self.next_run = None
            self.next_kind = None
            self.retry_count = 0
            if self.process is not None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass

    def schedule(self):
        while not self.closed.wait(1):
            with self.lock:
                self.expire_account()
                if self.next_run is None or time.time() < self.next_run:
                    continue
                try:
                    self.start(trigger='retry' if self.next_kind == 'retry' else 'automatic')
                except LowStorageError:
                    pass  # start() already scheduled the next local space check.
                except (ValueError, RuntimeError, OSError):
                    self.problem = '自动任务未能启动，请检查设置和会话挂载'
                    self.schedule_next(failed=True)

    def close(self):
        self.closed.set()
        with self.lock:
            process = self.process
            if process is not None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        if process is not None:
            try:
                process.wait(timeout=40)
            except subprocess.TimeoutExpired:
                # Container shutdown has a fixed deadline; cleanup cannot be guaranteed here.
                process.kill()
                process.wait()
        self.scheduler.join(timeout=2)
        if self.monitor:
            self.monitor.join(timeout=2)
        self.account_session = self.account_source = self.sms_phone = None


def handler_for(console):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def reply(self, code, body, kind='application/json; charset=utf-8'):
            data = json.dumps(body, ensure_ascii=False).encode() if isinstance(body, dict) else body
            self.send_response(code)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def same_origin(self):
            host = self.headers.get('Host', '')
            if console.mode == 'desktop' and host not in (
                    f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'):
                self.reply(403, {'error': '电脑版仅允许本机访问'})
                return False
            origin = self.headers.get('Origin')
            if origin:
                parsed = urlsplit(origin)
                if parsed.scheme not in ('http', 'https') or parsed.netloc != host:
                    self.reply(403, {'error': '不允许跨站请求'})
                    return False
            return True

        def authorized(self):
            if not self.same_origin():
                return False
            given = self.headers.get('Authorization', '')
            if not secrets.compare_digest(given.encode(), ('Bearer ' + console.token).encode()):
                self.reply(401, {'error': '请输入正确的管理密钥'})
                return False
            return True

        def secure_login(self):
            # Do not trust forwarded headers: use TLS here or the loopback-only desktop server.
            return isinstance(self.connection, ssl.SSLSocket) or (console.mode == 'desktop'
                and self.client_address[0] == '127.0.0.1' and self.server.server_address[0] == '127.0.0.1')

        def status(self):
            value = console.snapshot()
            value['account']['login_allowed'] = self.secure_login()
            return value

        def do_GET(self):
            if not self.same_origin():
                return
            if self.path == '/api/mode':
                return self.reply(200, {'mode': console.mode})
            if self.path in ('/', '/settings.js', '/settings.css'):
                name, kind = {'/': ('settings.html', 'text/html; charset=utf-8'),
                              '/settings.js': ('settings.js', 'text/javascript; charset=utf-8'),
                              '/settings.css': ('settings.css', 'text/css; charset=utf-8')}[self.path]
                return self.reply(200, (Path(__file__).resolve().parent.parent / 'web' / name).read_bytes(), kind)
            if not self.authorized():
                return
            if self.path == '/api/status':
                return self.reply(200, self.status())
            self.reply(404, {'error': '接口不存在'})

        def do_POST(self):
            if self.path == '/api/connect' and console.mode == 'desktop':
                if not self.same_origin():
                    return
            elif not self.authorized():
                return
            try:
                if self.headers.get('Content-Type') != 'application/json':
                    raise ValueError('请求必须为 JSON')
                size = int(self.headers.get('Content-Length', '-1'))
                if not 0 < size <= 8192:
                    raise ValueError('请求长度无效')
                body = json.loads(self.rfile.read(size))
                if self.path == '/api/connect' and console.mode == 'desktop':
                    return self.reply(200, {'token': console.connect(body)})
                if self.path.startswith('/api/account/'):
                    action = self.path.removeprefix('/api/account/')
                    if action in ('send', 'login') and not self.secure_login():
                        return self.reply(403, {'error': 'NAS 登录需要 HTTPS；请先配置证书，普通 HTTP 不接收手机号和验证码'})
                    console.account_action(action, body)
                elif self.path == '/api/local':
                    if isinstance(body, dict) and 'session' in body and not self.secure_login():
                        return self.reply(403, {'error': '导入会话需要本机访问或 HTTPS'})
                    console.local_setup(body)
                elif self.path == '/api/settings':
                    console.save(body)
                elif self.path == '/api/run':
                    console.start()
                elif self.path == '/api/stop':
                    console.stop()
                else:
                    return self.reply(404, {'error': '接口不存在'})
                self.reply(200, self.status())
            except LoginError as error:
                self.reply(400, {'error': str(error)})
            except LowStorageError:
                self.reply(409, {'error': '存储空间不足，归档已暂停；释放空间后再运行，至少保留 5 GB'})
            except (ValueError, UnicodeError):
                self.reply(400, {'error': '设置无效或未准备就绪，请检查字段、维护许可、会话文件与保存路径'})
            except RuntimeError:
                self.reply(409, {'error': '任务运行中，请等待结束或先停止'})
            except OSError:
                self.reply(500, {'error': '文件访问失败，请检查配置目录、会话挂载和磁盘权限'})
    return Handler


def local_output(value):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError('请填写完整的保存目录')
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError('请使用绝对路径')
    path = path.resolve()
    project = Path(__file__).resolve().parent.parent
    if path == project or project in path.parents:
        raise ValueError('请将录像保存在项目代码目录之外')
    return path


def make_server(console, host, port):
    if console.mode == 'desktop' and host != '127.0.0.1':
        raise ValueError('Desktop console must listen on 127.0.0.1')
    server = ThreadingHTTPServer((host, port), handler_for(console))
    try:
        cert, key = console.env.get('PAWZZ_TLS_CERT'), console.env.get('PAWZZ_TLS_KEY')
        if cert or key:
            if not cert or not key or console.mode != 'server':
                raise ValueError('Both TLS certificate and private key are required for server mode')
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
    except Exception:
        server.server_close()
        raise
    console.env['PAWZZ_WEB_PORT'] = str(server.server_port)
    return server


def main():
    os.umask(0o077)
    console = Console(Path(os.environ.get('PAWZZ_CONFIG_DIR', '/config')))
    server = make_server(console, os.environ.get('PAWZZ_WEB_BIND', '0.0.0.0'),
                         int(os.environ.get('PAWZZ_WEB_PORT', '8080')))
    def terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    print('Settings console ready. Export stays idle until configured. Admin key: config/admin-token', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        console.close()
        server.server_close()


if __name__ == '__main__':
    main()
