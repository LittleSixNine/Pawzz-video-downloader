"""Run an on-demand export console on this computer, without Docker."""
from __future__ import annotations
import argparse
import errno
import fcntl
import os
from pathlib import Path
import signal
import sys
import webbrowser

from web_settings import Console, local_output, make_server


def data_folder():
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'Pawzz Video'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'pawzz-video'


def main(argv=None):
    parser = argparse.ArgumentParser(description='Pawzz Video · 电脑按需导出（仅本机访问）')
    parser.add_argument('--data-dir', type=Path, default=data_folder(), help='私有配置与会话目录')
    parser.add_argument('--output-dir', type=Path, help='更改原片保存位置（已有录像不移动）')
    parser.add_argument('--port', type=int, default=8080, help='优先使用的网页端口，占用时自动换用空闲端口')
    parser.add_argument('--no-browser', action='store_true', help='仅启动服务，不打开浏览器')
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('端口必须在 0 到 65535 之间')
    os.umask(0o077)
    folder = local_output(str(args.data_dir.expanduser().absolute()))
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    instance = (folder / 'instance.lock').open('a')
    try:
        fcntl.flock(instance, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        instance.close()
        print('电脑版已在运行，请使用已有页面或先关闭另一个启动窗口。', file=sys.stderr)
        return 2
    output = Path.home() / 'Movies' / 'Pawzz Video'
    env = dict(os.environ, PAWZZ_MODE='desktop', PAWZZ_CONFIG_DIR=str(folder),
               PAWZZ_SESSION_FILE=str(folder / 'session.json'), PAWZZ_OUTPUT_DIR=str(output))
    console = Console(folder, env)
    try:
        if args.output_dir:
            console.local_setup({'output_dir': str(args.output_dir.expanduser().absolute())})
        Path(console.env['PAWZZ_OUTPUT_DIR']).mkdir(parents=True, exist_ok=True)
        try:
            server = make_server(console, '127.0.0.1', args.port)
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
            server = make_server(console, '127.0.0.1', 0)
        address = f'http://127.0.0.1:{server.server_port}/'
        print(f'Pawzz Video 电脑版已启动：{address}', flush=True)
        print(f'管理密码文件（首次安装默认 admin，可在设置修改）：{folder / "admin-token"}', flush=True)
        print('导出时请保持本窗口开启、电脑唤醒。按 Control-C 安全停止。', flush=True)
        if not args.no_browser:
            # The one-use code never goes into HTTP URLs or logs; the page clears it immediately.
            webbrowser.open(address + '#connect=' + console.new_connect_code())
        def terminate(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, terminate)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        console.close()
        instance.close()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError):
        print('未能启动：请检查端口范围、数据目录权限，以及 local.json 是否有效。', file=sys.stderr)
        raise SystemExit(2)
