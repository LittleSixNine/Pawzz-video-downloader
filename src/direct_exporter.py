"""Export original MP4s through the owner-authorized firmware maintenance handler."""
from __future__ import annotations
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta

from synology_exporter import DeviceClient, INTERVALS, read_session, AuthenticationError
from timeline_protocol import _head
from device_discovery import DiscoveryError, discover_device, normalize_mac
from diagnostics import event, phase, error_fields
from media_tools import ffmpeg_path

SOURCE = re.compile(r'/data_fs/record/(\d{8})/(\d{6}\.\d{3}\.mp4)')
MD5 = re.compile(r'[0-9a-f]{32}')
MIN_FREE_BYTES = 5 * 1024**3


class RepairError(RuntimeError):
    pass


class LowStorageError(OSError):
    pass


def require_storage(folder, incoming=0):
    if shutil.disk_usage(folder).free < MIN_FREE_BYTES + incoming:
        raise LowStorageError('storage below reserve')


def source_path(value):
    match = SOURCE.fullmatch(value)
    if not match:
        raise ValueError('unexpected recording path')
    datetime.strptime(match[1] + match[2][:6], '%Y%m%d%H%M%S')
    return match


def digest_file(path):
    digest = hashlib.md5()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verified_existing(target, source):
    try:
        record = json.loads(target.with_suffix('.mp4.json').read_text())
        return (record['source'] == source and record['md5_before'] == record['md5_after']
                == digest_file(target) and record['bytes'] == target.stat().st_size)
    except (OSError, ValueError, KeyError):
        return False


class Receiver:
    def __init__(self, device_ip, bind_ip, port, destination=None):
        self.key = secrets.token_hex(24)
        self.device_ip = device_ip
        self.destination = destination
        self.done = threading.Event()
        self.received = False
        self.skipped = False
        self.error = None
        self.before = self.after = None
        self.data = None
        self.rejections = set()
        self.stage = 'catalog' if destination is None else 'transfer'
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_PUT(self):
                self.handle_body()
            def do_POST(self):
                self.handle_body()
            def handle_body(self):
                route = self.path.removeprefix('/' + outer.key)
                if (self.client_address[0] != device_ip or
                    self.path not in ('/' + outer.key + '/file', '/' + outer.key + '/done')):
                    reason = ('route_mismatch' if self.path not in ('/' + outer.key + '/file', '/' + outer.key + '/done')
                              else 'source_mismatch')
                    if reason not in outer.rejections:
                        outer.rejections.add(reason)
                        event(outer.stage, 'callback_rejected', reason=reason, http_status=403)
                    self.send_error(403)
                    return
                try:
                    self.connection.settimeout(30)
                    length = int(self.headers.get('Content-Length', '-1'))
                    limit = 4 * 1024 * 1024 if destination is None else 8 * 1024**3
                    if route == '/done':
                        limit = 256
                    if not 0 < length <= limit:
                        raise ValueError('invalid response length')
                    if route == '/done':
                        data = self.rfile.read(length)
                        if len(data) != length:
                            raise ValueError('short digest response')
                        outer.after = data.decode().strip().split()[0]
                        outer.skipped = outer.after == 'SKIP' and not outer.received
                        if not outer.skipped and (not MD5.fullmatch(outer.after) or not outer.received):
                            raise ValueError('invalid completion digest')
                        outer.done.set()
                    else:
                        if outer.received:
                            raise ValueError('duplicate response')
                        if destination is not None:
                            outer.before = self.headers.get('X-Source-MD5', '')
                            if not MD5.fullmatch(outer.before):
                                raise ValueError('missing source digest')
                            require_storage(destination.parent, length)
                            with destination.open('xb') as out:
                                remaining = length
                                while remaining:
                                    block = self.rfile.read(min(1024 * 1024, remaining))
                                    if not block:
                                        raise ValueError('short file response')
                                    require_storage(destination.parent, len(block))
                                    out.write(block)
                                    out.flush()
                                    remaining -= len(block)
                        else:
                            outer.data = self.rfile.read(length)
                            if len(outer.data) != length:
                                raise ValueError('short catalog response')
                            outer.done.set()
                        outer.received = True
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'OK')
                    event(outer.stage, 'callback_received', bytes=length, http_status=200)
                except Exception as error:
                    event(outer.stage, 'callback_rejected', **dict(error_fields(error), http_status=400))
                    outer.error = type(error).__name__
                    outer.done.set()
                    self.send_error(400)
        with phase(self.stage):
            self.server = HTTPServer((bind_ip, port), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.worker.start()
        return self

    def wait(self, timeout):
        if not self.done.wait(timeout):
            raise TimeoutError('device callback timed out')
        if self.error:
            if self.error == 'LowStorageError':
                raise LowStorageError('storage below reserve')
            raise RuntimeError('receiver rejected response: ' + self.error)

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()


def publish(device, name, body):
    head = _head(name, device.user_id)
    head['origin'] = 'server'
    sent = device.client.publish(device.topic, json.dumps({'head': head, 'body': body}), qos=1)
    sent.wait_for_publish(timeout=10)
    if not sent.is_published():
        raise RuntimeError('maintenance command not acknowledged by broker')


def request(device, cfg, receiver, operation):
    # Firmware invokes a shell; only fixed templates and validated IPs/paths enter here.
    base = f"http://{cfg['advertise']}:{cfg['port']}/{receiver.key}"
    curl = "/oem/tool/curl --noproxy '*' -fsS --connect-timeout 10 --max-time 300"
    script = operation(base, curl)
    stage = 'transfer' if getattr(receiver, 'destination', None) is not None else 'catalog'
    with phase(stage):
        publish(device, 'enable_remote_telnet', {'svr_ip': f"{cfg['advertise']} & {{ {script}; }}"})
        event(stage, 'command_sent')
        event(stage, 'waiting')
        receiver.wait(330)


@phase('repair')
def repair_video(original, output):
    target = output / 'repaired' / original.relative_to(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    original_md5 = digest_file(original)
    metadata = target.with_suffix('.mp4.json')
    if target.exists():
        try:
            record = json.loads(metadata.read_text())
            if (record['original_md5'] == original_md5 and
                    record['md5_local'] == digest_file(target) and
                    record['bytes'] == target.stat().st_size):
                return
        except (OSError, ValueError, KeyError):
            pass
        raise RepairError('existing repaired file lacks matching verification record')
    require_storage(target.parent, original.stat().st_size)
    partial = target.with_suffix('.mp4.repair.part')
    temporary = metadata.with_suffix('.json.part')
    partial.unlink(missing_ok=True)
    executable = ffmpeg_path()
    if not executable:
        raise RepairError('FFmpeg required for lossless repair')
    def run(arguments):
        try:
            return subprocess.run([executable, '-hide_banner', '-v', 'error', '-nostdin', *arguments],
                                  check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=300).stdout
        except (OSError, subprocess.SubprocessError):
            raise RepairError('lossless repair failed; original retained') from None
    try:
        run(['-n', '-i', str(original), '-map', '0', '-c', 'copy',
             '-movflags', '+faststart', '-f', 'mp4', str(partial)])
        def stream_hash(path):
            return run(['-i', str(path), '-map', '0', '-c', 'copy',
                        '-f', 'streamhash', '-hash', 'sha256', '-'])
        before = stream_hash(original)
        if not before or before != stream_hash(partial) or digest_file(original) != original_md5:
            raise RepairError('lossless repair stream verification failed; original retained')
        record = dict(original_md5=original_md5, md5_local=digest_file(partial),
                      bytes=partial.stat().st_size, method='ffmpeg-streamcopy',
                      verified_at=datetime.now().isoformat())
        temporary.write_text(json.dumps(record, indent=2))
        temporary.replace(metadata)
        partial.replace(target)
        print('verified repair:', target.relative_to(output), flush=True)
    finally:
        partial.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


@phase('transfer')
def export_one(device, cfg, source):
    match = source_path(source)
    target = cfg['output'] / match[1] / match[2]
    target.parent.mkdir(parents=True, exist_ok=True)
    if verified_existing(target, source):
        if cfg.get('repair', False):
            repair_video(target, cfg['output'])
        return False
    if target.exists():
        raise RuntimeError('existing file lacks matching verification record')
    require_storage(target.parent)
    partial = target.with_suffix('.mp4.part')
    partial.unlink(missing_ok=True)
    try:
        with Receiver(cfg['device'], cfg['bind'], cfg['port'], partial) as receiver:
            def operation(base, curl):
                return (f'if [ "$(find {source} -mmin +10)" = "{source}" ]; then '
                        f'before=$(md5sum {source}) && before=${{before%% *}} && '
                        f'{curl} -H "X-Source-MD5: $before" -T {source} {base}/file && '
                        f'after=$(md5sum {source}) && '
                        f'{curl} --data-binary "$after" {base}/done; '
                        f'else {curl} --data-binary SKIP {base}/done; fi')
            request(device, cfg, receiver, operation)
            if receiver.skipped:
                return False
            with phase('checksum'):
                local = digest_file(partial)
                if receiver.before != local or receiver.after != local:
                    raise ValueError('source changed or file checksum mismatch')
            record = {'source': source, 'bytes': partial.stat().st_size,
                      'md5_before': receiver.before, 'md5_after': receiver.after,
                      'md5_local': local, 'verified_at': datetime.now().isoformat()}
        metadata = target.with_suffix('.mp4.json')
        temporary = metadata.with_suffix('.json.part')
        temporary.write_text(json.dumps(record, indent=2))
        temporary.replace(metadata)
        partial.replace(target)
        print('verified original:', target.relative_to(cfg['output']), flush=True)
        if cfg.get('repair', False):
            repair_video(target, cfg['output'])
        return True
    finally:
        partial.unlink(missing_ok=True)


def retry_read(operation, stage='catalog'):
    for attempt in range(4):
        try:
            return operation()
        except TimeoutError:
            if attempt == 3:
                raise
            event(stage, 'retry', attempt=attempt + 1, max_attempts=3)
            print(f'read retry: {attempt + 1}/3', flush=True)
            time.sleep(5)


def export_cycle(cfg):
    if cfg.get('repair', False) and not ffmpeg_path():
        raise RepairError('FFmpeg required for lossless repair')
    with phase('session'):
        user, token = read_session(cfg['session'])
    with phase('storage'):
        cfg['output'].mkdir(parents=True, exist_ok=True)
        require_storage(cfg['output'])
    cfg = cfg.copy()
    fallback = bool(cfg.get('device') and cfg.get('device_mac'))
    if not cfg.get('device') and cfg.get('device_mac'):
        with phase('mac_discovery'):
            cfg = dict(cfg, device=discover_device(cfg['device_mac'], cfg['advertise']))

    def read(operation, stage='catalog'):
        def attempt():
            nonlocal fallback, cfg
            try:
                return operation()
            except TimeoutError:
                if not fallback:
                    raise
                fallback = False
                event('mac_discovery', 'retry', attempt=1, max_attempts=1)
                with phase('mac_discovery'):
                    address = discover_device(cfg['device_mac'], cfg['advertise'])
                if address == cfg['device']:
                    raise
                cfg = dict(cfg, device=address)
                return operation()
        return retry_read(attempt, stage)
    count = 0
    with DeviceClient(user, cfg['serial'], token) as device:
        try:
            if cfg['source']:
                candidates = [cfg['source']]
            else:
                def discover():
                    with Receiver(cfg['device'], cfg['bind'], cfg['port']) as receiver:
                        def operation(base, curl):
                            # A sentinel distinguishes an empty directory from no response.
                            return ("{ printf 'CATALOG\\n'; find /data_fs/record -type f -name '*.mp4'; } | "
                                    f'{curl} --data-binary @- {base}/file')
                        request(device, cfg, receiver, operation)
                        lines = receiver.data.decode().splitlines()
                        if not lines or lines[0] != 'CATALOG':
                            raise ValueError('invalid catalog')
                        return sorted(set(lines[1:]))
                candidates = read(discover)
            event('catalog', 'success', count=len(candidates))
            cutoff = datetime.now() - timedelta(minutes=10)
            for source in candidates:
                match = source_path(source)
                timestamp = datetime.strptime(match[1] + match[2][:6], '%Y%m%d%H%M%S')
                if timestamp > cutoff or (cfg['start'] and match[1] < cfg['start']):
                    continue
                count += int(read(lambda: export_one(device, cfg, source), stage='transfer'))
                event('transfer', 'success', count=count)
        finally:
            with phase('cleanup'):
                publish(device, 'disenable_remote_telnet', {})
    return count


def config(env=None, allow_incomplete=False):
    env = os.environ if env is None else env
    if env.get('PAWZZ_ALLOW_MAINTENANCE') != 'yes':
        raise ValueError('explicit PAWZZ_ALLOW_MAINTENANCE=yes required')
    mac = normalize_mac(env['PAWZZ_DEVICE_MAC']) if env.get('PAWZZ_DEVICE_MAC') else ''
    device = str(ipaddress.IPv4Address(env['PAWZZ_DEVICE_IP'])) if env.get('PAWZZ_DEVICE_IP') else None
    advertise = str(ipaddress.IPv4Address(env['PAWZZ_CALLBACK_IP'])) if env.get('PAWZZ_CALLBACK_IP') else None
    if ((not allow_incomplete and (not (device or mac) or not advertise))
            or (device and not ipaddress.ip_address(device).is_private)
            or (advertise and not ipaddress.ip_address(advertise).is_private)):
        raise ValueError('local network addresses required')
    port = int(env.get('PAWZZ_CALLBACK_PORT', '10003'))
    if not 1024 <= port <= 65535:
        raise ValueError('invalid callback port')
    start = env.get('PAWZZ_START_DATE', '')
    if start:
        start = datetime.strptime(start, '%Y-%m-%d').strftime('%Y%m%d')
    source = env.get('PAWZZ_SOURCE', '')
    if source:
        source_path(source)
    return dict(device=device, device_mac=mac, advertise=advertise, port=port,
                bind=str(ipaddress.IPv4Address(env.get('PAWZZ_BIND_IP', '0.0.0.0'))),
                serial=env['PAWZZ_SN'], session=int(env['PAWZZ_SESSION_FD']) if env.get('PAWZZ_SESSION_FD') else Path(env['PAWZZ_SESSION_FILE']),
                output=Path(env.get('PAWZZ_OUTPUT_DIR', '/videos')),
                interval=INTERVALS[env.get('PAWZZ_INTERVAL', '1d')],
                once=env.get('PAWZZ_ONCE', '0') == '1', source=source, start=start,
                repair=env.get('PAWZZ_REPAIR_VIDEO', 'no') == 'yes')


def failure_hint(error):
    # Fixed messages keep session values and private paths out of container logs.
    if isinstance(error, DiscoveryError):
        return str(error) + ': check device MAC, same LAN and iproute2'
    if isinstance(error, RepairError):
        return 'repair_failed: check FFmpeg and repaired files; originals retained'
    if isinstance(error, AuthenticationError):
        return 'authentication: sign in again'
    if isinstance(error, FileNotFoundError):
        return 'missing_file: check session mount and output directory'
    if isinstance(error, PermissionError):
        return 'permission_denied: check session/output permissions and NAS ACL'
    if isinstance(error, TimeoutError):
        return 'timeout: check MQTT connectivity, callback IP/port and firewall'
    if isinstance(error, OSError):
        return 'io_or_network: check disk space, port conflicts, DNS and TLS'
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return 'invalid_data: check settings, session format and file verification'
    return 'operation_failed: check MQTT access and callback verification'


def main():
    os.umask(0o077)
    # Turn container termination into an exception so the active request's finally runs.
    def terminate(*args):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        cfg = config()
    except (ValueError, KeyError, TypeError) as error:
        event('task', 'failed', **error_fields(error))
        print('startup failed: configuration; ' + failure_hint(error), flush=True)
        return 2
    while True:
        try:
            count = export_cycle(cfg)
            print(f'cycle complete: {count} original files', flush=True)
        except KeyboardInterrupt:
            print('stopped by request', flush=True)
            return 130
        except AuthenticationError:
            print('cycle failed: authentication: sign in again', flush=True)
            return 4
        except LowStorageError:
            print('cycle paused: low_storage', flush=True)
            return 3
        except Exception as error:
            event('task', 'failed', **error_fields(error))
            print('cycle failed: ' + failure_hint(error), flush=True)
            if cfg['once']:
                return 1
        if cfg['once']:
            print('one-shot finished normally; container will stop (exit 0)', flush=True)
            return 0
        try:
            time.sleep(cfg['interval'])
        except KeyboardInterrupt:
            return 130


if __name__ == '__main__':
    raise SystemExit(main())
