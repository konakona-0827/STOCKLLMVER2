"""Internal timer and UI controls use fake jobs; never calls real models."""
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request,urlopen

from support_scout.contracts import timestamp
from support_scout.scheduler import ScanScheduler,next_session_slot
from support_scout.dashboard import DeskServer,serve


class InternalSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.now=timestamp('2026-11-02T13:30:00+09:00');self.calls=[]
        self.scheduler=ScanScheduler(self.root,clock=lambda:self.now,runner=self.run_job)
    def tearDown(self):self.scheduler.close();self.tmp.cleanup()
    def run_job(self,**kwargs):self.calls.append(kwargs);return {'status':'COMPLETE','model_calls':3}

    def test_tick_runs_without_codex_and_only_once_each_scheduled_hour(self):
        self.scheduler.tick();self.scheduler.tick();self.assertEqual(len(self.calls),1)
        self.now+=timedelta(minutes=60);self.scheduler.tick();self.assertEqual(len(self.calls),2)
        self.assertFalse(self.scheduler.status()['requires_codex'])
        self.assertEqual(self.scheduler.status()['engine'],'PROGRAM_INTERNAL')

    def test_lunch_weekend_and_holiday_do_not_call_runner(self):
        for day in ['2026-11-02T12:00:00+09:00','2026-11-03T13:00:00+09:00','2026-11-01T13:00:00+09:00']:
            self.now=timestamp(day);self.scheduler.tick()
        self.assertEqual(self.calls,[])

    def test_restart_respects_reserved_slot_and_next_slot_runs(self):
        (self.root/'session_scan_state.json').write_text(json.dumps({'slot':self.now.isoformat(),'status':'DEGRADED'}))
        self.scheduler.tick();self.assertEqual(self.calls,[])
        self.now+=timedelta(minutes=60);self.scheduler.tick();self.assertEqual(len(self.calls),1)

    def test_stop_is_persisted_and_stops_future_jobs(self):
        self.scheduler.set_enabled(False);self.scheduler.tick();self.assertEqual(self.calls,[])
        other=ScanScheduler(self.root,clock=lambda:self.now,runner=self.run_job)
        self.assertFalse(other.enabled);other.close()

    def test_relative_env_is_resolved_from_configuration_directory(self):
        from support_scout.settings import load_settings
        config=self.root/'local_settings.json'
        config.write_text(json.dumps({'openai_env_file':'.env','model':'fixture'}),encoding='utf-8')
        self.assertEqual(load_settings(config)['openai_env_file'],str((self.root/'.env').resolve()))

    def test_next_run_skips_culture_day_and_lunch(self):
        self.assertEqual(next_session_slot(timestamp('2026-11-02T11:00:00+09:00')),'2026-11-02T12:30:00+09:00')
        self.assertEqual(next_session_slot(timestamp('2026-11-02T15:00:00+09:00')),'2026-11-02T15:15:00+09:00')
        self.assertEqual(next_session_slot(timestamp('2026-11-02T15:15:00+09:00')),'2026-11-04T09:00:00+09:00')

    def test_full_day_has_exactly_seven_automatic_runs_and_no_backfill(self):
        from support_scout.scan_schedule import DAILY_SCAN_TIMES
        for hour in range(8,17):
            for minute in range(60):
                self.now=timestamp(f'2026-11-02T{hour:02d}:{minute:02d}:00+09:00')
                self.scheduler.tick()
        self.assertEqual([c['now'].strftime('%H:%M') for c in self.calls],list(DAILY_SCAN_TIMES))
        self.calls.clear();self.now=timestamp('2026-11-04T14:42:00+09:00')
        self.scheduler.tick();self.assertEqual(self.calls,[])
        self.assertEqual(self.scheduler.status()['next_run_jst'],'2026-11-04T15:15:00+09:00')

    def test_closing_scan_is_distinct_from_1500_manual_slot(self):
        from support_scout.session_scan import run_session
        def observer(**kw):return {},self.root/'fake',0
        first=run_session(now=timestamp('2026-11-02T15:00:00+09:00'),data_root=self.root,observer=observer)
        second=run_session(now=timestamp('2026-11-02T15:15:09+09:00'),data_root=self.root,observer=observer)
        self.assertNotEqual(first['slot'],second['slot']);self.assertEqual(second['status'],'COMPLETE')
        third=run_session(now=timestamp('2026-11-02T15:19:00+09:00'),data_root=self.root,observer=observer)
        self.assertEqual(third['status'],'SKIPPED_DUPLICATE_SLOT')

    def test_manual_rerun_and_timer_never_overlap(self):
        entered=threading.Event();release=threading.Event()
        def job(**kwargs):entered.set();release.wait(2);return {'status':'COMPLETE'}
        self.scheduler.runner=job
        thread=threading.Thread(target=lambda:self.scheduler.run_now(rerun=True));thread.start();self.assertTrue(entered.wait(1))
        self.assertEqual(self.scheduler.run_now()['status'],'SKIPPED_BUSY')
        self.scheduler.tick();self.assertTrue(self.scheduler.status()['running'])
        release.set();thread.join(2)


class SchedulerDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.calls=[]
        self.server=DeskServer(0,data_root=self.root,output_root=self.root/'reports',session_runner=lambda **kw:self.calls.append(kw) or {'status':'COMPLETE'})
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join(2);self.tmp.cleanup()
    def post(self,value,token=True):
        headers={'Origin':self.base,'Content-Type':'application/json'}
        if token:headers['X-Desk-Token']=self.server.token
        with urlopen(Request(self.base+'/api/scheduler',data=json.dumps(value).encode(),headers=headers)) as r:return json.load(r)

    def test_dashboard_has_program_controls_and_verified_state(self):
        with urlopen(self.base) as r:h=r.read().decode()
        self.assertIn('日股自動掃描控制台',h);self.assertIn('scheduler-start',h);self.assertIn('scheduler-stop',h)
        state=self.post({'enabled':False});self.assertFalse(state['enabled']);self.assertFalse(state['requires_codex'])

    def test_control_requires_csrf_token(self):
        from urllib.error import HTTPError
        with self.assertRaises(HTTPError) as e:self.post({'enabled':False},token=False)
        self.assertEqual(e.exception.code,403)

    def test_duplicate_launcher_reuses_existing_dashboard(self):
        with patch('webbrowser.open') as browser:
            serve(self.server.server_port,open_browser=True)
        browser.assert_called_once()


if __name__=='__main__':unittest.main()
