import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from pawzz_notifications import Notifications


class PawzzTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'notify.json').write_text(json.dumps(dict(url='http://127.0.0.1:1',project='feeder',token='disposable')))
        self.closed=threading.Event()
        self.notifications=Notifications(self.root,lambda:dict(monitoring=True,next_start=time.time()+600),self.closed)

    def test_optional_and_bad_state_never_break_export(self):
        (self.root/'notify.json').unlink()
        n=Notifications(self.root,lambda:{},self.closed)
        self.assertFalse(n.enabled)
        (self.root/'notify.json').write_text(json.dumps(dict(url='http://127.0.0.1:1',project='feeder',token='disposable')))
        (self.root/'notify-outbox'/'record-markers').write_text('broken')
        self.assertFalse(Notifications(self.root,lambda:{},self.closed).enabled)

    def test_actual_attempts_and_duplicate_history_save(self):
        record=dict(id='one',state='running',started_at=time.time(),finished_at=None,new_files=0)
        self.notifications.record(record);self.notifications.record(record)
        record.update(state='success',finished_at=time.time(),new_files=3,exit_code=0)
        self.notifications.record(record);self.notifications.record(record)
        events=self.notifications.outbox.pending()
        self.assertEqual([e['kind'] for e in events],['started','success'])
        self.assertEqual(events[1]['metrics'],{'files':3})

    def test_after_crash_replayed_record_is_byte_equivalent(self):
        record=dict(id='one',state='failed',started_at=time.time()-10,finished_at=time.time(),new_files=0,exit_code=4)
        self.notifications.record(record)
        (self.root/'notify-outbox'/'record-markers').unlink()
        n=Notifications(self.root,lambda:{},self.closed);n.record(record)
        events=self.notifications.outbox.pending()
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['code'],'auth_expired')

    def test_heartbeat_is_sanitized(self):
        self.notifications.pulse()
        value=self.notifications.outbox.pending()[0]
        self.assertEqual(set(value),{'id','run','kind','occurred','monitoring','next_start','protocol','alert','sender_policy','relay'})

    def test_existing_account_fault_alerts_once_even_when_automatic_is_off(self):
        self.notifications.status=lambda:dict(monitoring=False,next_start=None,problem_code='auth_expired')
        self.notifications.pulse();self.notifications.pulse()
        events=self.notifications.outbox.pending()
        self.assertEqual(sum(e['kind']=='failed' for e in events),1)
        self.assertEqual(sum(e['kind']=='heartbeat' for e in events),1)

    def test_policy_saved_on_sender_changes_threshold_without_center_configuration(self):
        self.notifications.save_policy(dict(failure_threshold=2,reminder_minutes=0,recovery=True,immediate_codes=[]))
        for index in range(2):
            self.notifications.record(dict(id='run'+str(index),state='failed',started_at=time.time()-1,finished_at=time.time(),new_files=0,exit_code=1))
        events=self.notifications.outbox.pending()
        self.assertEqual([e['alert'] for e in events],['none','open'])
        self.assertEqual(self.notifications.settings()['policy']['failure_threshold'],2)
        self.assertNotIn('token',json.dumps(self.notifications.settings()))

    def test_local_relay_status_has_error_cause_without_endpoint_or_token(self):
        from urllib.error import HTTPError
        class Unauthorized:
            def open(self,*args,**kwargs):raise HTTPError('PRIVATE endpoint',401,'PRIVATE',{},None)
        self.notifications.outbox.emit('failed','diagnostic')
        self.notifications.outbox.flush(Unauthorized())
        value=self.notifications.settings()
        self.assertEqual(value['relay']['last_error'],'authentication_failed')
        self.assertEqual(value['relay']['pending'],1)
        self.assertNotIn('disposable',json.dumps(value));self.assertNotIn('http',json.dumps(value))

    def test_producer_warning_recovers_and_warns_again_on_a_new_episode(self):
        import contextlib,io
        def failed():raise PermissionError('PRIVATE details')
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            self.notifications.safe(failed,stage='heartbeat');self.notifications.safe(failed,stage='heartbeat')
            self.notifications.safe(lambda:None,stage='heartbeat');self.notifications.safe(failed,stage='heartbeat')
        text=output.getvalue()
        self.assertEqual(text.count('producer_failed'),2);self.assertEqual(text.count('producer_recovered'),1)
        self.assertNotIn('PRIVATE',text)
