"""Application-owned scan loop. No dependency on Codex or a browser timer."""
from copy import deepcopy
from datetime import datetime,timedelta
import json
from pathlib import Path
import threading

from .contracts import DataError
from .japan import JST
from .session_scan import market_open,run_session
from .storage import atomic_json


def slot_for(now):
    now=now.astimezone(JST)
    return now.replace(minute=now.minute//30*30,second=0,microsecond=0)


def next_session_slot(now):
    candidate=slot_for(now)+timedelta(minutes=30)
    for _ in range(48*20):
        if market_open(candidate):return candidate.isoformat()
        candidate+=timedelta(minutes=30)
    raise DataError("NO_UPCOMING_SESSION_IN_CALENDAR")


class ScanScheduler:
    def __init__(self,data_root,*,output_root=None,universe_path=None,runner=None,clock=None):
        self.root=Path(data_root);self.output_root=output_root;self.universe_path=universe_path
        self.runner=runner or run_session;self.clock=clock or (lambda:datetime.now(JST))
        self.settings_path=self.root/'scan_scheduler.json'
        self.enabled=True
        if self.settings_path.exists():
            value=json.loads(self.settings_path.read_text(encoding='utf-8'))
            if value.get('schema_version')!=1 or type(value.get('enabled')) is not bool:raise DataError('INVALID_INTERNAL_SCHEDULER_SETTINGS')
            self.enabled=value['enabled']
        self.running=False;self.last_result=None;self.last_error=None;self.attempted_slot=None
        self.lock=threading.RLock();self.job_lock=threading.Lock();self.wake=threading.Event();self.stop=threading.Event();self.thread=None

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():return
            self.root.mkdir(parents=True,exist_ok=True)
            atomic_json(self.settings_path,{'schema_version':1,'enabled':self.enabled,'interval_minutes':30,'timezone':'Asia/Tokyo'})
            self.thread=threading.Thread(target=self._loop,name='japan-session-scheduler',daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set();self.wake.set()
        if self.thread and self.thread is not threading.current_thread():self.thread.join(timeout=1)

    def set_enabled(self,value):
        if type(value) is not bool:raise DataError('SCHEDULER_ENABLED_MUST_BE_BOOLEAN')
        with self.lock:
            atomic_json(self.settings_path,{'schema_version':1,'enabled':value,'interval_minutes':30,'timezone':'Asia/Tokyo'})
            self.enabled=value
        self.start();self.wake.set()

    def _state(self):
        path=self.root/'session_scan_state.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

    def run_now(self,*,rerun=False):
        if not self.job_lock.acquire(blocking=False):return {'status':'SKIPPED_BUSY','model_calls':0}
        with self.lock:self.running=True;self.last_error=None
        try:
            result=self.runner(now=self.clock(),data_root=self.root,output_root=self.output_root,
                universe_path=self.universe_path,rerun=rerun)
            with self.lock:self.last_result=deepcopy(result)
            return result
        except Exception as exc:
            error=str(exc) if isinstance(exc,DataError) else type(exc).__name__
            with self.lock:self.last_error=error;self.last_result={'status':'FAILED','error':error}
            return {'status':'FAILED','error':error}
        finally:
            with self.lock:self.running=False
            self.job_lock.release()

    def tick(self):
        now=self.clock();slot=slot_for(now).isoformat()
        with self.lock:
            if not self.enabled or self.running or not market_open(now):return
            if self.attempted_slot==slot or self._state().get('slot')==slot:return
            self.attempted_slot=slot
        return self.run_now()

    def _loop(self):
        while not self.stop.is_set():
            try:self.tick()
            except Exception as exc:
                with self.lock:self.last_error=str(exc) if isinstance(exc,DataError) else type(exc).__name__
            self.wake.wait(10);self.wake.clear()

    def status(self):
        now=self.clock()
        with self.lock:
            state=self._state();opened=market_open(now)
            upcoming=next_session_slot(now) if self.enabled else None
            if self.enabled and opened and self.attempted_slot!=slot_for(now).isoformat() and state.get('slot')!=slot_for(now).isoformat():
                upcoming=now.isoformat()
            last=deepcopy(self.last_result or state or None)
            return {'enabled':self.enabled,'engine':'PROGRAM_INTERNAL','interval_minutes':30,'timezone':'Asia/Tokyo',
                'worker_alive':bool(self.thread and self.thread.is_alive()),
                'market_open':opened,'running':self.running or state.get('status')=='RUNNING',
                'next_run_jst':upcoming,'last_result':last,'error':self.last_error,
                'requires_codex':False,'program_must_remain_running':True}
