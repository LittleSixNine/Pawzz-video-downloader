"""Local preflight and one-shot export with a credential-free JSON result."""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import threading
import time
import webbrowser

from direct_exporter import config, export_cycle, failure_hint, LowStorageError, require_storage
from synology_exporter import read_session
from web_settings import Console, DEFAULTS, environment, local_output, validate, make_server


def prepare(args):
    settings_path = local_output(str(args.settings.expanduser().absolute()))
    session_path = local_output(str(args.session.expanduser().absolute())) if args.session else None
    output = local_output(str(args.output.expanduser().absolute()))
    if settings_path.stat().st_size > 8192 or session_path and session_path.stat().st_size > 8192:
        raise ValueError('Configuration file too large')
    supplied = json.loads(settings_path.read_text())
    if not isinstance(supplied, dict):
        raise ValueError('Settings must be an object')
    settings = validate({**DEFAULTS, **supplied, 'automatic': False})
    if session_path:
        read_session(session_path)  # Format only; no network or credential display.
    ancestor = output
    while not ancestor.exists():
        ancestor = ancestor.parent
    if not ancestor.is_dir() or not os.access(ancestor, os.W_OK):
        raise OSError('Output is not writable')
    require_storage(ancestor)
    settings['allow_maintenance'] = True  # Actual use is gated separately by --allow-maintenance.
    cfg = config(environment(settings, dict(os.environ, PAWZZ_SESSION_FILE=str(session_path),
                                           PAWZZ_OUTPUT_DIR=str(output))))
    return cfg


def browser_export(args):
    """The person logs in and starts the already-authorized single job in their browser."""
    folder = args.settings.expanduser().resolve().parent / 'agent-console'
    env = dict(os.environ, PAWZZ_MODE='desktop', PAWZZ_SESSION_FILE=str(folder / 'unused-session.json'),
               PAWZZ_OUTPUT_DIR=str(args.output.expanduser().resolve()))
    console = Console(folder, env)
    server = None
    try:
        supplied = json.loads(args.settings.expanduser().read_text())
        console.save(validate({**DEFAULTS, **supplied, 'automatic': False, 'allow_maintenance': True}))
        # An Agent task has an explicit destination, even if a previous UI run chose another one.
        console.local_setup({'output_dir': env['PAWZZ_OUTPUT_DIR']})
        server = make_server(console, '127.0.0.1', 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        address = f'http://127.0.0.1:{server.server_port}/'
        previous_ids = {record['id'] for record in console.history}
        print(json.dumps(dict(status='awaiting_user', url=address,
                              action='在本机网页登录，再点击开始导出；不向 Agent 提供手机号或验证码'), ensure_ascii=False), flush=True)
        # Only the browser receives this short-lived code. It is never printed.
        webbrowser.open(address + '#connect=' + console.new_connect_code())
        deadline = time.monotonic() + 900
        while not console.closed.wait(.2):
            state = console.snapshot()
            new_job = next((row for row in state['history'] if row['id'] not in previous_ids), None)
            if new_job and not state['running']:
                return (0, dict(status='success', new_files=new_job['new_files'])) if new_job['state'] == 'success' else (
                    new_job.get('exit_code') or 1, dict(status=new_job['state'], new_files=None))
            if not new_job and time.monotonic() >= deadline:
                return 2, dict(status='not_ready', reason='browser_login_or_start_timeout')
    finally:
        console.close()
        if server:
            server.shutdown()
            server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Agent 单次原片导出；check 不连接设备、不创建目录')
    parser.add_argument('action', choices=('check', 'run'))
    parser.add_argument('--settings', required=True, type=Path, help='仓库外的设备设置 JSON')
    login_source = parser.add_mutually_exclusive_group(required=True)
    login_source.add_argument('--session', type=Path, help='高级：仓库外的明文用户会话 JSON')
    login_source.add_argument('--web-login', action='store_true', help='在本机网页临时登录并手动开始一轮；凭据不保存到文件')
    parser.add_argument('--output', required=True, type=Path, help='仓库外的原片目录')
    parser.add_argument('--allow-maintenance', action='store_true', help='明确许可本轮使用维护读取通道')
    args = parser.parse_args(argv)
    os.umask(0o077)
    def report(code, **body):
        print(json.dumps(body, ensure_ascii=False))
        return code
    try:
        cfg = prepare(args)
    except LowStorageError:
        return report(3, status='paused', reason='low_storage', new_files=None)
    except (ValueError, TypeError, OSError, KeyError) as error:
        return report(2, status='not_ready', reason=failure_hint(error).split(':')[0])
    if args.action == 'check':
        return report(0, status='ready', scope='local_checks_only', device_verified=False, **({'login_required': True} if args.web_login else {}))
    if not args.allow_maintenance:
        return report(2, status='not_ready', reason='maintenance_permission_required')
    def terminate(*_):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        # ponytail: directory lock only; different data folders still need operator coordination.
        with (args.settings.expanduser().resolve().parent / 'instance.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return report(2, status='not_ready', reason='another_instance_running')
            if args.web_login:
                code, result = browser_export(args)
                return report(code, **result)
            with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet):
                count = export_cycle(cfg)
        return report(0, status='success', new_files=count)
    except KeyboardInterrupt:
        return report(130, status='stopped', new_files=None)
    except LowStorageError:
        return report(3, status='paused', reason='low_storage', new_files=None)
    except Exception as error:
        return report(1, status='failed', reason=failure_hint(error).split(':')[0], new_files=None)
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    raise SystemExit(main())
