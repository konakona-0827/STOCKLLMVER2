import ast
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from config import ROOT, now
from scheduler import Scheduler
import position_store as ps
import position_analyzer as pa
from analysis_service import build_union, AnalysisService


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'history.sqlite3'
        self.p = ps.save_manual(self.path,'0050',10,20,100,114.2)
        self.c = dict(symbol='0050', last_price=116.2, quote_status='LAST_KNOWN',
                      position=ps.context(self.p,116.2))
        self.answer = dict(market_view='非即時行情',decisions=[dict(symbol='0050',decision='SELL',
            confidence=.7,action_ratio=.5,suggested_qty=10,reference_price=116.2,
            suggested_price=116.2,reason='降低 AI 管理部位',data_quality='LAST_KNOWN',warnings=['不是即時行情'])])

    def tearDown(self):
        self.tmp.cleanup()

    def test_fixed_schedule_and_cooldown(self):
        s = Scheduler()
        s.start_auto(0)
        self.assertEqual(s.next_auto,1800)
        self.assertTrue(s.manual(720,100))
        self.assertEqual(s.next_auto,1800)
        self.assertFalse(s.manual(721,101))
        s.finish()
        self.assertFalse(s.manual(779,159))
        self.assertTrue(s.manual(780,160))
        s.finish()
        self.assertTrue(s.tick(1800))
        self.assertEqual(s.next_auto,3600)
        self.assertFalse(s.tick(3600))
        self.assertEqual(s.skipped,1)
        self.assertEqual(s.next_auto,5400)
        s.finish()
        self.assertTrue(s.tick(5400))
        s.finish()

    def test_stop_auto_and_test_interval(self):
        s = Scheduler(120)
        s.start_auto(120)
        self.assertEqual(s.next_auto,240)
        s.stop_auto()
        self.assertFalse(s.tick(1000))

    def test_position_pnl_and_audit(self):
        c = self.c['position']
        self.assertEqual(c['total_qty'],30)
        self.assertAlmostEqual(c['ai_managed_ratio'],2/3)
        self.assertAlmostEqual(c['ai_unrealized_pnl'],40)
        self.assertAlmostEqual(c['total_unrealized_pnl'],202)
        self.assertIsNone(ps.context(ps.empty('2330'),None)['ai_unrealized_pnl_pct'])
        ps.save_manual(self.path,'0050',10,15,100,None)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM position_events').fetchone()[0],2)
        self.assertIsNone(ps.context(ps.load(self.path)[0],116.2)['total_unrealized_pnl'])

    def test_bad_positions_rejected(self):
        for args in [('abc',1,1,None,None),('0050',-1,20,None,None),('0050',True,20,None,None),('0050',10,20,float('nan'),None)]:
            with self.assertRaises(ValueError):
                ps.save_manual(self.path,*args)

    def test_sell_excludes_user_qty(self):
        self.assertEqual(pa.validate(self.answer,[self.c])['decisions'][0]['suggested_qty'],10)
        self.answer['decisions'][0]['suggested_qty']=15
        with self.assertRaises(ValueError):
            pa.validate(self.answer,[self.c])
        self.assertEqual(pa.sell_qty(100,.29),29)

    def test_all_symbols_required(self):
        with self.assertRaises(ValueError):
            pa.validate(self.answer,[self.c,{**self.c,'symbol':'2454'}])
        self.answer['decisions']*=2
        with self.assertRaises(ValueError):
            pa.validate(self.answer,[self.c])

    def test_unavailable_must_wait(self):
        c = {**self.c,'quote_status':'UNAVAILABLE','last_price':None}
        d = self.answer['decisions'][0]
        d.update(reference_price=None,data_quality='UNAVAILABLE')
        with self.assertRaises(ValueError):
            pa.validate(self.answer,[c])
        d.update(decision='WAIT',action_ratio=0,suggested_qty=0,suggested_price=None)
        pa.validate(self.answer,[c])

    def test_user_only_cannot_sell(self):
        c = copy.deepcopy(self.c)
        c['position']['ai_managed_qty']=0
        with self.assertRaises(ValueError):
            pa.validate(self.answer,[c])

    def test_bad_types_rejected(self):
        for change in [dict(confidence=True),dict(action_ratio=float('nan')),dict(suggested_qty=10.0),dict(reference_price=999),dict(warnings=[])]:
            a = copy.deepcopy(self.answer)
            a['decisions'][0].update(change)
            with self.assertRaises(ValueError):
                pa.validate(a,[self.c])

    def test_union_sources_and_unavailable_position(self):
        stamp = now().isoformat()
        quotes = [dict(symbol=s,last_price=100,quote_status='LAST_KNOWN',quote_timestamp=stamp,
                       open=99,high=101,low=98,bid=99,ask=100,volume=10000) for s in ['0050','2330','2454']]
        quotes[-1].update(last_price=None,quote_status='UNAVAILABLE')
        top = [dict(symbol=s,rank=i,quant_score=1,components={},metrics={},warnings=[]) for i,s in enumerate(['0050','2330'],1)]
        p2 = ps.save_manual(self.path,'2454',0,3,None,100)
        cs = build_union(top,top,quotes,[self.p,p2],self.path,stamp)
        self.assertEqual([(c['symbol'],c['analysis_source']) for c in cs], [('0050','TOP10+POSITION'),('2330','TOP10'),('2454','POSITION')])
        self.assertEqual(len(json.loads(pa.build_request(cs)['input'])['analysis_set']),3)
        self.assertEqual(cs[-1]['quote_status'],'UNAVAILABLE')

    def test_error_fallback_never_mutates_position(self):
        before = ps.load(self.path)
        decision,response=pa.analyze([self.c],{},provider=lambda request:('invalid',{}))
        self.assertEqual(response['validation'],'ERROR')
        self.assertEqual(decision['decisions'][0]['decision'],'WAIT')
        self.assertEqual(ps.load(self.path),before)

    def test_run_error_saved(self):
        svc = AnalysisService(Path(self.tmp.name))
        with patch('analysis_service.load_env'),patch.object(svc,'_run',side_effect=RuntimeError('test failure')):
            with self.assertRaises(RuntimeError):
                svc.run('MANUAL')
        runs = list((Path(self.tmp.name)/'runs').glob('*/run.json'))
        r = json.loads(runs[0].read_text())
        self.assertEqual(r['status'],'ERROR')
        self.assertEqual(r['trigger_type'],'MANUAL')
        self.assertIn('finished_at',r)

    def test_no_execution_calls(self):
        for file in ['dashboard.py','analysis_service.py','position_analyzer.py','scheduler.py','position_store.py','capital.py','capital_multi_quote.py']:
            tree = ast.parse((ROOT/file).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node,ast.Call):
                    name=getattr(node.func,'attr',getattr(node.func,'id',''))
                    self.assertFalse(any(s in name for s in ['SendStock','CancelOrder','PaperBroker']), (file,name))

    def test_gui_worker_error_releases_lock_and_auto_recovers(self):
        import tkinter as tk
        from dashboard import Dashboard
        root = tk.Tk()
        root.withdraw()
        app = Dashboard(root,Path(self.tmp.name),120)
        calls = []
        def fail(trigger, phase):
            calls.append(trigger)
            phase('WAITING_OPENAI')
            time.sleep(.25)
            raise RuntimeError('injected local test error')
        app.service.run = fail
        app.manual()
        deadline = time.monotonic()+3
        try:
            while app.scheduler.scan_lock.locked() and time.monotonic()<deadline:
                root.update()
                time.sleep(.02)
            self.assertEqual(app.status,'ERROR')
            self.assertFalse(app.scheduler.scan_lock.locked())
            self.assertGreaterEqual(app.heartbeat,2)
            app.scheduler.next_auto=time.time()-.1
            deadline=time.monotonic()+3
            while (len(calls)<2 or app.scheduler.scan_lock.locked()) and time.monotonic()<deadline:
                root.update()
                time.sleep(.02)
            self.assertEqual(calls,['MANUAL','AUTO'])
            self.assertFalse(app.scheduler.scan_lock.locked())
            self.assertGreater(app.scheduler.next_auto,time.time())
        finally:
            app.close()
            until=time.monotonic()+2
            while root.winfo_exists() and time.monotonic()<until:
                root.update()
                time.sleep(.02)
                try:
                    alive=root.winfo_exists()
                except tk.TclError:
                    break
                if not alive:
                    break


if __name__=='__main__':
    unittest.main()
