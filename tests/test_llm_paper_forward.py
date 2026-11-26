"""First-day runtime/funding fault injection. All quotes/models are test doubles."""
from concurrent.futures import Future
from copy import deepcopy
from datetime import datetime,timedelta
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from research.llm_paper.common import ROOT,ARMS,protocol,save,read,digest,Market
from research.llm_paper.engine import Book
from research.llm_paper.journal import Journal,checkpoint
from research.llm_paper.llm import build_request
from research.llm_paper.live import live_frame
from research.llm_paper.runtime import advance_books,commit_decision,finish_session,drain_jobs,run,pointer
from research.llm_paper.reporting import generate
from test_llm_paper import quote,order,SESSIONS


def latest():
    return dict(protocol(),funding_mode='LATEST_BRANCH_CUMULATIVE',strategy_cap_twd='10000',
        min_available_to_buy_twd='5000',max_daily_buy_twd='30000',max_order_twd='100000',
        minimum_commission_twd='1',buy_cash_buffer_rate='.01',min_buy_fee_reserve_twd='20',
        data_role='SYNTHETIC_INTEGRATION_NOT_FORWARD')


def sources(at='2026-11-24T10:00:00+08:00'):
    symbols=read(ROOT/'config/universe_tw.json')['symbols']
    bars=[dict(date=(datetime(2026,11,23)-timedelta(days=i)).date().isoformat(),open=99,high=103,low=98,close=100,
               volume=100000,complete=True,source='TWSE_DAILY') for i in range(21,-1,-1)]
    daily=dict(bars=bars,status=dict(latest_date='2026-11-23',expected_latest_date='2026-11-23',prior_complete_sessions=22))
    return dict(calendar_valid_year=2026,sessions=SESSIONS,at='2026-11-23T18:00:00+08:00',as_of='2026-11-24',
        instruments={s:dict(exchange='TWSE',asset_type='STOCK',received_at='2026-11-23T18:00:00+08:00') for s in symbols},
        daily={s:deepcopy(daily) for s in symbols})


class LatestFundingTests(unittest.TestCase):
    def test_cash_below_ai_cap_is_not_blanket_block(self):
        p=latest();p['initial_cash_twd']='8373';b=Book(p)
        self.assertEqual(b.available('2026-11-24'),3373)
        self.assertEqual(b.config['strategy_cap_twd'],'10000')

    def test_cap_cumulative_daily_resets_independently(self):
        b=Book(latest());b.submit([order(qty=40)],'2026-11-24T10:00:00+08:00','x')
        b.advance([quote()], '2026-11-24T10:00:01+08:00',SESSIONS,strict_depth=True)
        self.assertEqual(b.cash,95974)
        self.assertEqual(b.positions['2330']['funding_committed'],'4060.200')
        self.assertEqual(b.available('2026-11-24'),Decimal('5939.8'))
        self.assertEqual(b.available('2026-11-25'),Decimal('5939.8'))

    def test_actual_commission_separate_from_twenty_dollar_reserve(self):
        b=Book(latest());b.submit([order(qty=10)],'2026-11-24T10:00:00+08:00','x')
        self.assertEqual(b.reserved(),1025)
        b.advance([quote(ask_size_shares=4)],'2026-11-24T10:00:01+08:00',SESSIONS,strict_depth=True)
        b.advance([quote('2026-11-24T10:00:02+08:00',ask_size_shares=6)],'2026-11-24T10:00:02+08:00',SESSIONS,strict_depth=True)
        self.assertEqual(sum(Decimal(f['commission_twd']) for f in b.fills),2)
        self.assertEqual(b.cash,98993)
        self.assertEqual(Decimal(b.positions['2330']['funding_committed']),1025)

    def test_latest_request_risk_cap_and_reserve_rules_shared(self):
        q=quote();f=dict(id='f',at=q['quote_timestamp'],quotes=[q],candidates=[dict(q,quant_rank=1)])
        requests=[build_request(f,Book(latest()),'strategy',a)[1] for a in ARMS]
        self.assertEqual(requests[0]['input'],requests[1]['input'])
        p=json.loads(requests[0]['input'])
        self.assertEqual(p['paper_account']['capital_limit_twd'],10000)
        self.assertEqual(p['paper_simulation_rules']['min_buy_fee_reserve_twd'],'20')
        self.assertEqual(p['paper_simulation_rules']['minimum_commission_twd'],'1')

    def test_official_cash_floor_does_not_change_cap(self):
        p=latest();p['initial_cash_twd']='5200';b=Book(p)
        b.submit([order(qty=2)],'2026-11-24T10:00:00+08:00','x')
        self.assertEqual(b.orders[-1]['reason'],'CASH_OR_DAILY_CAP')
        self.assertEqual(b.config['strategy_cap_twd'],'10000')


class FirstDaySafetyTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'research-runs').mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'research-runs');self.out=Path(self.tmp.name)
        self.journal=Journal(self.out/'paper.sqlite3');self.config=latest()
        self.books={a:Book(self.config) for a in ARMS}
        self.journal.append('registration',dict(version='fixture',protocol=self.config,live_order_interface=None))

    def tearDown(self):self.tmp.cleanup()

    def test_atomic_batch_rolls_back_if_later_event_fails(self):
        before=self.journal.verify()
        with self.assertRaises(sqlite3.IntegrityError):
            self.journal.append_many([dict(kind='a',payload={},event_id='same'),dict(kind='b',payload={},event_id='same')])
        self.assertEqual(self.journal.verify(),before)

    def test_same_clock_tick_and_checkpoint_get_distinct_event_ids(self):
        before=self.journal.verify()
        with patch('research.llm_paper.journal.stamp',return_value='2026-11-24T02:00:00+00:00'):
            first=self.journal.append('checkpoint',checkpoint(self.books))
            second=self.journal.append('checkpoint',checkpoint(self.books))
        self.assertNotEqual(first,second)
        self.assertEqual(self.journal.verify(),before+2)

    def test_fresh_checkout_export_uses_bound_strategy_and_local_transport(self):
        from research.llm_paper.release import export
        state=self.out/'existing-account';(state/'data/analysis').mkdir(parents=True)
        selected=sorted((ROOT/'strategies').glob('*.md'))[0]
        save(state/'data/analysis/live_strategy.json',dict(path=selected.relative_to(ROOT).as_posix()))
        (state/'.env').write_text('PRIVATE_MARKER=do-not-export',encoding='utf-8')
        target=self.out/'isolated-release'
        with patch('runtime_paths.resolve_state_root',return_value=state):
            record=export(target)
        self.assertEqual(record['strategy'],selected.relative_to(ROOT).as_posix())
        self.assertEqual((target/'codex_cli.py').read_bytes(),(ROOT/'codex_cli.py').read_bytes())
        self.assertTrue((target/'bounded_cli_capture.py').is_file())
        self.assertFalse((target/'.env').exists());self.assertFalse((target/'data').exists())
        env={k:v for k,v in os.environ.items() if k!='TRADING_STATE_ROOT'}
        result=subprocess.run([sys.executable,'-X','utf8','-c',
            'import codex_cli,bounded_cli_capture,position_analyzer; import research.llm_paper.runtime; print("ISOLATED_IMPORT_OK")'],
            cwd=target,env=env,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('ISOLATED_IMPORT_OK',result.stdout)

    def test_decision_and_orders_roll_back_on_checkpoint_write_failure(self):
        with self.journal.connect() as db:
            db.execute("CREATE TRIGGER fail_checkpoint BEFORE INSERT ON events WHEN NEW.kind='checkpoint' BEGIN SELECT RAISE(ABORT,'INJECTED_DISK_FAILURE'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            commit_decision(self.books,ARMS[0],[order()],{},'2026-11-24T10:00:00+08:00','slot',self.journal)
        self.assertFalse(self.books[ARMS[0]].orders)
        self.assertIsNone(self.journal.latest('decision_result'))

    def test_partial_fill_evidence_and_recovery_equal_both_arms(self):
        for a in ARMS:commit_decision(self.books,a,[order()],dict(elapsed_seconds=1), '2026-11-24T10:00:00+08:00','s',self.journal)
        q=quote(ask_size_shares=4);f=dict(id='f',at=q['quote_timestamp'],quotes=[q])
        self.assertEqual(advance_books(self.books,f,SESSIONS,self.journal),2)
        self.assertEqual(advance_books(self.books,f,SESSIONS,self.journal),0)
        saved=self.journal.latest('checkpoint')
        for a in ARMS:
            recovered=Book(self.config,saved[a]);self.assertEqual(recovered.quantity('2330'),4)
            self.assertEqual(recovered.cash,self.books[a].cash)
        evidence=self.journal.latest('fill')
        self.assertEqual(evidence['observation']['depth_received_at'],evidence['fill']['quote_at'])

    def test_fill_transaction_failure_restores_both_books(self):
        for b in self.books.values():b.submit([order()],'2026-11-24T10:00:00+08:00','s')
        q=quote();f=dict(id='f',at=q['quote_timestamp'],quotes=[q])
        with patch.object(self.journal,'append_many',side_effect=OSError('disk')):
            with self.assertRaises(OSError):advance_books(self.books,f,SESSIONS,self.journal)
        self.assertTrue(all(not b.fills and b.quantity('2330')==0 for b in self.books.values()))

    def test_future_receipt_and_crossed_book_cannot_be_live(self):
        for changes in (dict(received_at='2026-11-25T10:00:00+08:00'),dict(bid=101,ask=100)):
            q=quote('2026-11-24T10:00:00+08:00',exchange='TWSE',**changes)
            f=live_frame(dict(quotes=[q]),sources(),{},q['quote_timestamp'])
            self.assertEqual(f['quotes'][0]['quote_status'],'LAST_KNOWN')

    def test_strict_fill_rejects_future_receipt_and_crossed_book(self):
        for changes in (dict(received_at='2026-11-25T10:00:00+08:00'),dict(bid=101,ask=100)):
            b=Book(self.config);b.submit([order()],'2026-11-24T10:00:00+08:00','s')
            q=quote(**changes);b.advance([q],q['quote_timestamp'],SESSIONS,strict_depth=True)
            self.assertFalse(b.fills)

    def test_close_expires_partial_rod_and_preserves_hash_chain(self):
        for b in self.books.values():b.submit([order()],'2026-11-24T10:00:00+08:00','s')
        q=quote(ask_size_shares=4);advance_books(self.books,dict(id='f',at=q['quote_timestamp'],quotes=[q]),SESSIONS,self.journal)
        self.assertTrue(finish_session(self.books,'2026-11-24','2026-11-24T13:30:01+08:00',self.journal))
        self.assertFalse(finish_session(self.books,'2026-11-24','2026-11-24T13:30:02+08:00',self.journal))
        for b in self.books.values():
            self.assertFalse(b.pending);self.assertEqual(b.orders[-1]['status'],'PARTIALLY_FILLED_EXPIRED_ROD')
            self.assertEqual(b.quantity('2330'),4)
        self.assertGreater(self.journal.verify(),1)

    def test_late_model_after_close_records_error_without_order(self):
        f=Future();f.set_result(dict(decision=dict(decisions=[order()]),elapsed_seconds=1))
        futures={ARMS[0]:f};q=quote();req=dict(input=json.dumps(dict(replay_scope=dict(as_of='2026-11-24T13:29:59+08:00'))))
        control=self.out/'control.json';pointer(control,dict(active=True))
        drain_jobs(futures,{ARMS[0]:([q],req)},'2026-11-24-slot',self.books,self.journal,control,'fixture',self.config,SESSIONS,'2026-11-24T13:30:01+08:00')
        self.assertFalse(futures);self.assertFalse(self.books[ARMS[0]].orders)
        self.assertEqual(self.journal.latest('decision_error')['error'],'SESSION_CLOSED_BEFORE_PAPER_SUBMISSION')

    def test_preopen_report_never_claims_forward_success(self):
        report=generate(self.out,'2026-11-26',self.out/'report',at='2026-11-25T15:00:00+08:00')
        self.assertEqual(report['phase'],'NOT_STARTED');self.assertIsNone(report['relative_to_formal_baseline'])
        self.assertEqual(report['live_test_evidence_gate'],'SYNTHETIC_ONLY')
        self.assertFalse(report['real_order_authorized_by_report'])

    def test_real_preopen_gate_is_pending_and_no_orders_authorized(self):
        folder=self.out/'real';j=Journal(folder/'paper.sqlite3');p=latest();p.pop('data_role')
        j.append('registration',dict(version='real-fixture',protocol=p,live_order_interface=None))
        r=generate(folder,'2026-11-26',folder/'report')
        self.assertEqual(r['live_test_evidence_gate'],'NOT_READY_NO_FIRST_DAY_EVIDENCE')
        self.assertIsNone(r['relative_to_formal_baseline'])

    def test_report_detects_cash_corruption(self):
        self.books[ARMS[0]].cash-=1
        self.journal.append('checkpoint',checkpoint(self.books))
        r=generate(self.out,'2026-11-24',self.out/'report')
        self.assertIn('CASH_IDENTITY:'+ARMS[0],r['audit_issues'])

    def test_old_day_report_excludes_next_day_account_changes(self):
        finish_session(self.books,'2026-11-24','2026-11-24T13:30:01+08:00',self.journal)
        self.journal.append('session_started',dict(day='2026-11-25',accounts=checkpoint(self.books)))
        self.books[ARMS[0]].cash-=100
        self.journal.append('checkpoint',checkpoint(self.books))
        r=generate(self.out,'2026-11-24',self.out/'report')
        self.assertEqual(r['comparison_arms'][ARMS[0]]['cash_twd'],'100000')
        self.assertFalse(r['audit_issues'])

    def test_full_intraday_runtime_with_fake_feed_real_validator_and_no_orders(self):
        import research.llm_paper.runtime as runtime
        start=datetime.fromisoformat('2026-11-24T10:00:00+08:00');clock={'tick':0,'mono':0}
        runout=self.out/'worker';sourcefile=self.out/'sources.json';save(sourcefile,sources())
        strategy=self.out/'strategy.md';strategy.write_text('測試策略：資料不足 WAIT。',encoding='utf-8')
        profile=self.out/'protocol.json';save(profile,self.config)
        class Clock(datetime):
            @staticmethod
            def now(tz):return (start+timedelta(seconds=clock['tick'])).astimezone(tz)
        class Feed:
            api_codes=[]
            def __init__(self,*args):pass
            def connect(self):pass
            def subscribe_depth(self,*args):pass
            def stopped(self):return False
            def close(self):pass
            def pump(self,*args):
                clock['tick']+=1
                if clock['tick']>=8:
                    # Let both real validator futures be drained; a fixed fast
                    # tick count races the worker threads on a fresh checkout.
                    with Journal(runout/'paper.sqlite3').connect() as db:
                        done=db.execute("SELECT count(*) FROM events WHERE kind='decision_result'").fetchone()[0]
                    if done==2 or clock['tick']>=80:
                        c=read(runout/'control.json');c['stop']=True;pointer(runout/'control.json',c)
            def snapshot(self):
                return dict(at=Clock.now(Market().tz).isoformat(),quotes=[quote(Clock.now(Market().tz).isoformat(),exchange='TWSE')])
        class Model:
            def __init__(self,*args):self.calls=0
            def __call__(self,request):
                self.calls+=1;p=json.loads(request['input'])
                decisions=[dict(symbol=c['symbol'],decision='WAIT',confidence=0,action_ratio=0,suggested_qty=0,reference_price=100,
                    suggested_price=None,strategy_id='NONE',target_price=None,invalidation_price=None,holding_days=None,
                    reason='fixture',data_quality='LIVE',warnings=[]) for c in p['analysis_set']]
                return json.dumps(dict(market_view='fixture',decisions=decisions)),dict(model=self.config['model'] if hasattr(self,'config') else 'gpt-5.6-luna',usage={},response_id='fixture')
        def mono():clock['mono']+=2;return clock['mono']
        with patch.object(runtime,'datetime',Clock),patch.object(runtime,'CapitalFeed',Feed),patch.object(runtime,'Provider',Model),\
             patch.object(runtime.time,'monotonic',side_effect=mono),patch.object(runtime,'freeze_sources',return_value={}),\
             patch.object(runtime,'verify_sources'),patch('config.load_env'):
            result=run(runout,strategy,'unused',sourcefile,'unused',protocol_file=profile)
        journal=Journal(runout/'paper.sqlite3')
        self.assertEqual(result['status'],'STOPPED_BY_CONTROL')
        with journal.connect() as db:
            count=db.execute("SELECT count(*) FROM events WHERE kind='decision_result'").fetchone()[0]
            errors=[json.loads(r[0]) for r in db.execute("SELECT payload FROM events WHERE kind='decision_error'")]
            self.assertEqual(count,2,errors)
        self.assertIsNotNone(journal.latest('market_quality'))
        self.assertFalse(journal.latest('checkpoint')[ARMS[0]]['fills'])


if __name__=='__main__':unittest.main()
