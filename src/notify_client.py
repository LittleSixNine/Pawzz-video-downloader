"""Stdlib producer: business policy and delivery outbox commit in one transaction."""
import argparse
import errno
from datetime import datetime, timezone
import socket
import ssl
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

POLICY_DEFAULTS = dict(failure_threshold=3, immediate_codes=['auth_expired','storage_low'],
                       reminder_minutes=0, recovery=False)
TERMINAL = ('success','failed','deferred','cancelled')


RELAY_ERRORS = {'','authentication_failed','forbidden','rate_limited','server_error','http_rejected',
                'connection_refused','dns_failed','tls_failed','timeout','connection_failed',
                'invalid_acknowledgement','invalid_data','permission_denied','disk_full','database_busy',
                'database_error','io_error','unexpected_error'}


def problem_code(error):
    if isinstance(error,urllib.error.HTTPError):
        return {401:'authentication_failed',403:'forbidden',429:'rate_limited'}.get(error.code,'server_error' if error.code>=500 else 'http_rejected')
    if isinstance(error,urllib.error.URLError):return problem_code(error.reason) if isinstance(error.reason,BaseException) else 'connection_failed'
    if isinstance(error,ssl.SSLError):return 'tls_failed'
    if isinstance(error,socket.gaierror):return 'dns_failed'
    if isinstance(error,ConnectionRefusedError):return 'connection_refused'
    if isinstance(error,TimeoutError):return 'timeout'
    if isinstance(error,PermissionError):return 'permission_denied'
    if isinstance(error,OSError) and error.errno==errno.ENOSPC:return 'disk_full'
    if isinstance(error,sqlite3.Error):return 'database_busy' if 'locked' in str(error).lower() else 'database_error'
    if isinstance(error,(ValueError,TypeError)):return 'invalid_data'
    if isinstance(error,OSError):return 'io_error'
    return 'unexpected_error'


def validate_relay(value):
    fields={'pending','oldest_pending','last_attempt','last_success','last_error','last_failure','last_failure_at','last_request_id','last_failure_request_id'}
    if isinstance(value,dict):value={'last_request_id':None,'last_failure_request_id':None}|value
    if not isinstance(value,dict) or set(value)!=fields:raise ValueError('发送端积压状态无效')
    if type(value['pending']) is not int or not 0<=value['pending']<=10**9:raise ValueError('发送端积压数量无效')
    for name in ('oldest_pending','last_attempt','last_success','last_failure_at'):
        if value[name] is not None and (type(value[name]) not in (int,float) or not math.isfinite(value[name]) or not 0<=value[name]<=10**12):raise ValueError('发送端状态时间无效')
    if any(not isinstance(value[k],str) or value[k] not in RELAY_ERRORS for k in ('last_error','last_failure')):raise ValueError('发送端错误类型无效')
    for key in ('last_request_id','last_failure_request_id'):
        if value[key] is not None and (not isinstance(value[key],str) or not re.fullmatch(r'[a-f0-9]{24}',value[key])):raise ValueError('排障编号无效')
    return value.copy()


def producer_log(event,code='',stage='relay',request_id=None):
    if code not in RELAY_ERRORS:code='unexpected_error'
    reference={'request_id':request_id} if isinstance(request_id,str) and re.fullmatch(r'[a-f0-9]{24}',request_id) else {}
    try:print(json.dumps(dict(time=time.time(),timestamp=datetime.now(timezone.utc).isoformat(),component='notify_producer',client_version='0.3.0',level='warning' if code else 'info',event=event,stage=stage,error=code,**reference)),flush=True)
    except OSError:pass


def write(path, data):
    fd, temporary = tempfile.mkstemp(prefix='.notify-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def validate_policy(value):
    if not isinstance(value, dict) or set(value) - set(POLICY_DEFAULTS):
        raise ValueError('发送端通知策略字段无效')
    result = dict(POLICY_DEFAULTS, **value)
    if type(result['failure_threshold']) is not int or not 1 <= result['failure_threshold'] <= 100:
        raise ValueError('失败次数须为 1 至 100')
    if type(result['reminder_minutes']) is not int or not 0 <= result['reminder_minutes'] <= 43200:
        raise ValueError('重复提醒间隔无效')
    if type(result['recovery']) is not bool:
        raise ValueError('恢复通知开关无效')
    codes = result['immediate_codes']
    if not isinstance(codes,list) or len(codes)>20 or any(not isinstance(c,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,47}',c) for c in codes):
        raise ValueError('首次提醒错误类型无效')
    return result


def read_policy(config):
    config = Path(config)
    separate = config.with_name('notify-policy.json')
    if separate.exists():
        return validate_policy(json.loads(separate.read_text()))
    cfg = json.loads(config.read_text()) if config.exists() else {}
    return validate_policy(cfg.get('policy', {}))


class Outbox:
    def __init__(self, folder, config, clock=time.time):
        self.folder, self.config, self.clock = Path(folder), Path(config), clock
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = self.folder/'queue.sqlite3'
        with self.locked(), self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE, run TEXT NOT NULL, kind TEXT NOT NULL, alert TEXT NOT NULL,
                    payload TEXT NOT NULL, pending INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS relay_status (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_pending ON events(pending,sequence);
                CREATE INDEX IF NOT EXISTS events_run ON events(run);
            ''')
            if not db.execute('SELECT 1 FROM state').fetchone():
                project=self.configuration()['project']
                db.execute('INSERT INTO state VALUES(1,?)',(json.dumps(dict(failures=0,active=False,last_alert=0,last_code='',last_time=0,project=project)),))
                # Preserve v0.1 payloads verbatim; do not retrospectively make alert decisions.
                for old in sorted(self.folder.glob('[0-9]*.json')):
                    event=json.loads(old.read_text())
                    db.execute('INSERT OR IGNORE INTO events(id,run,kind,alert,payload) VALUES(?,?,?,?,?)',
                               (event['id'],event.get('run',''),event['kind'],'none',json.dumps(event)))
        # Old JSON files remain as migration backups, never as a second active queue.

    @contextmanager
    def locked(self):
        with (self.folder/'.lock').open('a') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX)
            yield

    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.db,timeout=10)
        os.chmod(self.db,0o600)
        db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback();raise
        finally: db.close()

    def configuration(self):
        cfg=json.loads(self.config.read_text())
        if not isinstance(cfg,dict) or set(cfg)-{'url','project','token','policy'} or not all(isinstance(cfg.get(k),str) and cfg[k] for k in ('url','project','token')):
            raise ValueError('notification configuration invalid')
        url=urllib.parse.urlsplit(cfg['url'])
        if url.scheme not in ('http','https') or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise ValueError('notification URL invalid')
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,47}',cfg['project']):
            raise ValueError('notification project invalid')
        return cfg

    def emit(self,kind,run='',*,event_id=None,occurred=None,**values):
        if kind not in ('started','heartbeat',*TERMINAL) or (kind!='heartbeat' and not run):
            raise ValueError('invalid producer event')
        cfg,policy=self.configuration(),read_policy(self.config)
        with self.connection() as db:
            previous=db.execute('SELECT payload FROM events WHERE id=?',(event_id or '',)).fetchone()
            if previous is None and kind in TERMINAL:
                previous=db.execute("SELECT payload FROM events WHERE run=? AND kind IN ('success','failed','deferred','cancelled') LIMIT 1",(run,)).fetchone()
            if previous is None and kind=='started':
                previous=db.execute("SELECT payload FROM events WHERE run=? AND kind='started' LIMIT 1",(run,)).fetchone()
            if previous:
                old=json.loads(previous[0])
                if old['kind']!=kind or old.get('run','')!=run or (occurred is not None and old['occurred']!=occurred) or any(old.get(k)!=v for k,v in values.items()):
                    raise ValueError('conflicting duplicate producer event')
                return old
            state=json.loads(db.execute('SELECT value FROM state WHERE id=1').fetchone()[0])
            if state['project'] not in (None,cfg['project']):
                raise ValueError('outbox belongs to another project; use a separate outbox')
            state['project']=cfg['project']
            stamp=max(self.clock(),state['last_time']+.000001) if occurred is None else occurred
            if type(stamp) not in (int,float) or not math.isfinite(stamp):raise ValueError('invalid producer time')
            event=dict(id=event_id or secrets.token_hex(16),run=run,kind=kind,occurred=stamp,**values)
            code=event.get('code','')
            alert='none'
            if kind=='failed':
                state['failures']+=1
                qualified=state['failures']>=policy['failure_threshold'] or code in policy['immediate_codes']
                if qualified and (not state['active'] or code in policy['immediate_codes'] and code!=state['last_code']):
                    alert='open';state['active']=True
                if qualified:state['last_code']=code or 'failure'
            if kind=='success':
                if state['active'] and policy['recovery']:alert='resolved'
                state.update(failures=0,active=False,last_code='')
            elif state['active'] and kind in ('failed','heartbeat') and alert=='none' and policy['reminder_minutes'] and stamp-state['last_alert']>=policy['reminder_minutes']*60:
                alert='repeat'
            if alert!='none':
                state['last_alert']=stamp
                event['code']=code or state['last_code'] or 'failure'
            state['last_time']=max(stamp,state['last_time'])
            event.update(protocol=2,alert=alert,sender_policy=policy)
            event.setdefault('relay',self._status(db))
            if kind=='heartbeat' and alert=='none':
                db.execute("DELETE FROM events WHERE kind='heartbeat' AND alert='none'")
            db.execute('INSERT INTO events(id,run,kind,alert,payload) VALUES(?,?,?,?,?)',
                       (event['id'],run,kind,alert,json.dumps(event,ensure_ascii=False)))
            db.execute('UPDATE state SET value=? WHERE id=1',(json.dumps(state),))
            return event

    def pending(self,limit=100):
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT payload FROM events WHERE pending=1 ORDER BY sequence LIMIT ?',(limit,))]

    def _status(self,db):
        row=db.execute('SELECT value FROM relay_status WHERE id=1').fetchone()
        value=json.loads(row[0]) if row else dict(last_attempt=None,last_success=None,last_error='',last_failure='',last_failure_at=None)
        value['pending']=db.execute('SELECT count(*) FROM events WHERE pending=1').fetchone()[0]
        oldest=db.execute('SELECT payload FROM events WHERE pending=1 ORDER BY sequence LIMIT 1').fetchone()
        value['oldest_pending']=json.loads(oldest[0])['occurred'] if oldest else None
        return validate_relay(value)

    def status(self):
        with self.connection() as db:return self._status(db)

    def relay_result(self,error,sent,attempted,request_id=None):
        with self.connection() as db:
            row=db.execute('SELECT value FROM relay_status WHERE id=1').fetchone()
            old=json.loads(row[0]) if row else dict(last_attempt=None,last_success=None,last_error='',last_failure='',last_failure_at=None)
            value=old.copy()
            if attempted:value.update(last_attempt=self.clock(),last_request_id=request_id)
            if sent:value['last_success']=self.clock()
            if error:value.update(last_error=error,last_failure=error,last_failure_at=self.clock(),last_failure_request_id=request_id)
            elif sent:value['last_error']=''
            db.execute('INSERT INTO relay_status VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value',(json.dumps(value),))
        if value['last_error']!=old['last_error']:
            producer_log('relay_failed' if value['last_error'] else 'relay_recovered',value['last_error'],request_id=request_id)

    def flush(self,opener=None,limit=100):
        with (self.folder/'.flush.lock').open('a') as stream:
            try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return {'sent':0,'pending':-1}
            cfg=self.configuration()
            with self.connection() as db:
                project=json.loads(db.execute('SELECT value FROM state WHERE id=1').fetchone()[0])['project']
                if project not in (None,cfg['project']):raise ValueError('outbox belongs to another project; use a separate outbox')
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self,*args,**kwargs):return None
            sender=opener or urllib.request.build_opener(NoRedirect)
            sent,error,attempted,request_id=0,'',False,None
            for event in self.pending(limit):
                request=urllib.request.Request(cfg['url'].rstrip('/')+'/api/events/'+cfg['project'],json.dumps(event).encode(),
                    {'Content-Type':'application/json','Authorization':'Bearer '+cfg['token']})
                attempted=True
                request_id=None
                try:
                    with sender.open(request,timeout=3) as response:
                        header=(getattr(response,'headers',None) or {}).get('X-Request-ID')
                        request_id=header if isinstance(header,str) and re.fullmatch(r'[a-f0-9]{24}',header) else None
                        result=json.loads(response.read(4096))
                        if response.status!=200 or not isinstance(result,dict) or not isinstance(result.get('duplicate'),bool):
                            raise ValueError('invalid acknowledgement')
                except (OSError,ValueError) as caught:
                    header=(getattr(caught,'headers',None) or {}).get('X-Request-ID')
                    if isinstance(header,str) and re.fullmatch(r'[a-f0-9]{24}',header):request_id=header
                    error='invalid_acknowledgement' if isinstance(caught,ValueError) else problem_code(caught)
                    break
                with self.connection() as db:
                    db.execute('UPDATE events SET pending=0 WHERE id=?',(event['id'],))
                    if event['kind']=='heartbeat' and event.get('alert','none')=='none':db.execute('DELETE FROM events WHERE id=?',(event['id'],))
                sent+=1
            if attempted:self.relay_result(error,sent,attempted,request_id)
            return {'sent':sent,'pending':self.status()['pending']}

def joiner_result(path, run, began, exit_code):
    metrics = {}
    report = None
    try:
        # The CLI prints indented JSON alongside logs; decode whole objects, not lines.
        with Path(path).open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 4 * 1024 * 1024))
            raw = stream.read()
        text = raw.decode('utf-8', errors='replace')
        decoder, cursor = json.JSONDecoder(), 0
        while cursor < len(text):
            start = text.find('{', cursor)
            if start < 0:
                break
            try:
                value, cursor = decoder.raw_decode(text, start)
                if isinstance(value, dict) and {'startedAt', 'finishedAt', 'exitCode', 'jobs'} <= set(value):
                    report = value
            except ValueError:
                cursor = start + 1
        from datetime import datetime
        if not report or datetime.fromisoformat(report['startedAt'].replace('Z', '+00:00')).timestamp() < began - 2 or report['exitCode'] != exit_code:
            raise ValueError()
        jobs = report['jobs']
        if not isinstance(jobs, list):
            raise ValueError()
        for key in ('completed', 'skipped', 'deferred', 'failed'):
            metrics[key] = sum(job['status'] == key for job in jobs)
        metrics['files'] = sum(job['files'] for job in jobs if job['status'] == 'completed')
        metrics['seconds'] = max(0, time.time() - began)
        if exit_code == 0:
            return 'success', '', metrics
        if exit_code == 1 and metrics['deferred'] and not metrics['failed'] and not report.get('errors'):
            return 'deferred', 'input_unstable', metrics
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return ('cancelled', '', {}) if exit_code in (130, 143) else ('failed', 'execution_failed', metrics)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--outbox', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--no-flush', action='store_true')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('flush')
    commands.add_parser('status')
    start = commands.add_parser('start')
    start.add_argument('--run', required=True)
    finish = commands.add_parser('joiner-finish')
    finish.add_argument('--run', required=True)
    finish.add_argument('--began', type=float, required=True)
    finish.add_argument('--exit', type=int, required=True)
    finish.add_argument('--result', required=True)
    options = parser.parse_args()
    outbox = Outbox(options.outbox, options.config)
    if options.command == 'status':
        print(json.dumps(outbox.status()));return
    if options.command == 'start':
        outbox.emit('started', options.run)
    elif options.command == 'joiner-finish':
        kind, code, metrics = joiner_result(options.result, options.run, options.began, options.exit)
        outbox.emit(kind, options.run, code=code, metrics=metrics)
    if options.no_flush:
        return
    try:
        result = outbox.flush(limit=10)
        print(json.dumps(result))
    except (OSError, ValueError):
        print('notification_pending: check configuration, permissions and endpoint')
        raise SystemExit(1)


if __name__ == '__main__':
    main()
