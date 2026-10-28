"""Opt-in notifications; no account/session data or raw worker messages are exported."""
import json
from pathlib import Path
import secrets
import threading
import time
from notify_client import Outbox, write, read_policy, validate_policy, POLICY_DEFAULTS, problem_code, producer_log


class Notifications:
    def __init__(self, folder, status, closed):
        self.config = Path(folder) / 'notify.json'
        self.enabled = self.config.is_file()
        self.status, self.closed = status, closed
        self.guard = threading.RLock()
        self.problems = {}
        self.thread = None
        if self.enabled:
            try:
                self.outbox = Outbox(Path(folder) / 'notify-outbox', self.config)
                self.markers = self.outbox.folder / 'record-markers'
                self.seen = json.loads(self.markers.read_text()) if self.markers.exists() else {}
                if not isinstance(self.seen, dict):
                    raise ValueError()
            except (OSError, ValueError):
                self.enabled = False
                print('notification_disabled: cannot open producer state; export remains available', flush=True)

    def safe(self, function, stage='record'):
        try:
            value=function()
            if self.problems.pop(stage,None):producer_log('producer_recovered',stage=stage)
            return value
        except Exception as error:
            code=problem_code(error)
            if self.problems.get(stage)!=code:
                producer_log('producer_failed',code,stage)
                self.problems[stage]=code

    def settings(self):
        try:
            return dict(configured=self.enabled, policy=read_policy(self.config), relay=self.outbox.status() if self.enabled else None, problem=None)
        except (OSError, ValueError, TypeError):
            return dict(configured=self.enabled, policy=POLICY_DEFAULTS.copy(), relay=None,
                        problem='通知策略或接入配置无法读取，请检查配置目录')

    def save_policy(self, value):
        policy = validate_policy(value)
        write(self.config.with_name('notify-policy.json'), policy)
        producer_log('policy_updated',stage='settings')
        return policy

    def record(self, record):
        if not self.enabled:
            return
        def emit():
            with self.guard:
                phase = 'started' if record['state'] == 'running' else 'finished'
                key = record['id'] + ':' + phase
                if key in self.seen:
                    return
                state = record['state']
                kind = {'running':'started','success':'success','stopped':'cancelled'}.get(state,'failed')
                code = ('auth_expired' if record.get('exit_code') == 4 else
                        'storage_low' if record.get('exit_code') == 3 else
                        'process_interrupted' if state == 'interrupted' else
                        'execution_failed' if kind == 'failed' else '')
                timestamp = record['started_at'] if phase == 'started' else record.get('finished_at') or record['started_at'] + .000001
                metrics = {'files': record.get('new_files') or 0} if phase == 'finished' else {}
                self.outbox.emit(kind, record['id'], event_id='pawzz:' + key,
                                 occurred=timestamp, code=code, metrics=metrics)
                self.seen[key] = True
                if code == 'auth_expired':
                    self.seen['problem:auth_expired'] = True
                self.seen = dict(list(self.seen.items())[-500:])
                write(self.markers, self.seen)
        self.safe(emit)

    def failed_start(self, code='execution_failed'):
        if self.enabled:
            self.safe(lambda:self.outbox.emit('failed', 'preflight:' + secrets.token_hex(12), code=code))

    def start(self):
        if self.enabled:
            self.thread = threading.Thread(target=self.loop,daemon=True,name='notification-relay')
            self.thread.start()

    def pulse(self):
        status = self.status()
        problem = status.get('problem_code', '')
        with self.guard:
            key = 'problem:auth_expired'
            if problem == 'auth_expired' and not self.seen.get(key):
                self.outbox.emit('failed', 'account:' + secrets.token_hex(12), code=problem)
                self.seen[key] = True
                write(self.markers, self.seen)
            elif not problem and self.seen.pop(key, None):
                write(self.markers, self.seen)
        self.outbox.emit('heartbeat', monitoring=status['monitoring'], next_start=status['next_start'])

    def loop(self):
        last_pulse = 0
        while not self.closed.is_set():
            if time.monotonic() - last_pulse >= 60:
                self.safe(self.pulse,stage='heartbeat')
                last_pulse = time.monotonic()
            self.safe(lambda:self.outbox.flush(limit=10),stage='relay')
            self.closed.wait(5)
