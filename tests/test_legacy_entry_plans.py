from datetime import date
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from trade_plan_context import live_plans


class LegacyEntryPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.execution = self.root/'execution.sqlite3'
        self.analysis = self.root/'analysis.sqlite3'
        with closing(sqlite3.connect(self.execution)) as db, db:
            db.execute('CREATE TABLE execution_attempts (run_id TEXT,symbol TEXT,side TEXT,started_at TEXT,detail_json TEXT)')
            db.execute('CREATE TABLE manual_ai_adoptions (symbol TEXT,qty INT,adopted_at TEXT)')
        with closing(sqlite3.connect(self.analysis)) as db, db:
            db.execute('CREATE TABLE selection_analyses (run_id TEXT PRIMARY KEY,decision_json TEXT)')

    def save(self, run, *, symbol='2891', side='BUY', filled=25, day='2026-10-16', plan=None, target=80.5):
        with closing(sqlite3.connect(self.execution)) as db, db:
            db.execute('INSERT INTO execution_attempts VALUES (?,?,?,?,?)', (run,symbol,side,day+'T04:00:00+00:00',
                       json.dumps(dict(filled_quantity=filled, buy_plan=plan))))
        with closing(sqlite3.connect(self.analysis)) as db, db:
            db.execute('INSERT INTO selection_analyses VALUES (?,?)', (run,json.dumps(dict(decisions=[dict(
                symbol=symbol, decision=side, strategy_id='breakout', suggested_price=74.8,
                target_price=target, invalidation_price=73.4, holding_days=5, reason='原計畫')]))))

    def read(self, qty=25):
        return live_plans(self.execution, {'2891':qty}, self.analysis, as_of=date(2026,11,3))['2891']

    def test_exact_saved_buy_is_restored_only_for_confirmed_remaining_quantity(self):
        self.save('original')
        result = self.read()
        self.assertTrue(result['available'])
        lot = result['lots'][0]
        self.assertEqual(lot['quantity'],25)
        self.assertEqual(lot['plan']['source_run_id'],'original')
        self.assertEqual(lot['plan']['invalidation_price'],73.4)
        self.assertEqual(lot['review_status']['completed_sessions_after_entry'],9)
        self.assertTrue(lot['review_status']['review_due'])
        with closing(sqlite3.connect(self.execution)) as db, db:
            self.assertIsNone(json.loads(db.execute('SELECT detail_json FROM execution_attempts').fetchone()[0])['buy_plan'])

    def test_unfilled_order_and_fully_sold_old_lot_never_supply_current_plan(self):
        self.save('old',filled=30,day='2026-10-12',target=90)
        self.save('sell',side='SELL',filled=30,day='2026-10-14')
        self.save('cancelled',filled=0,day='2026-10-15',target=100)
        self.save('current',day='2026-10-16')
        result=self.read()
        self.assertEqual(len(result['lots']),1)
        self.assertEqual(result['lots'][0]['plan']['target_price'],80.5)
        self.assertEqual(result['lots'][0]['plan']['source_run_id'],'current')

    def test_explicit_execution_plan_wins_and_manual_holding_remains_unknown(self):
        self.save('entry',plan=dict(target_price=79,holding_days=10))
        self.assertEqual(self.read()['lots'][0]['plan']['target_price'],79)
        with closing(sqlite3.connect(self.execution)) as db, db:
            db.execute('INSERT INTO manual_ai_adoptions VALUES (?,?,?)',('1101',100,'2026-10-13T04:00:00+00:00'))
        result=live_plans(self.execution,{'1101':100},self.analysis)['1101']
        self.assertFalse(result['available'])
        self.assertIsNone(result['lots'][0]['plan'])

    def test_missing_or_malformed_archive_does_not_invent_a_plan(self):
        self.save('entry')
        with closing(sqlite3.connect(self.analysis)) as db, db:
            db.execute("UPDATE selection_analyses SET decision_json='broken'")
        self.assertFalse(self.read()['available'])
        self.assertFalse(live_plans(self.execution,{'2891':25},self.root/'missing.sqlite3')['2891']['available'])
        self.assertFalse((self.root/'missing.sqlite3').exists())

    def test_restored_plan_reaches_model_summary_and_independent_sell_review(self):
        from token_budget import candidate_view
        from position_analyzer import review_trades
        from execution.auto_advice_executor import LiveExecutionConfig
        self.save('original')
        candidate=dict(symbol='2891',asset_type='STOCK',last_price=73.7,bid=73.6,ask=73.7,
            quote_status='LIVE',quote_timestamp='2026-11-03T11:05:00+08:00',is_trial=False,
            paper_position=dict(qty=25),max_sell_qty=25,buy_plans=self.read())
        visible=candidate_view(candidate,dict(available_cash_cents=546026,capital_limit_twd=10000),LiveExecutionConfig())
        self.assertEqual(visible['buy_plans']['lots'][0]['plan']['source_run_id'],'original')
        trade=dict(symbol='2891',decision='SELL',suggested_qty=25,suggested_price=73.6,
                   reason='重新評估原計畫',warnings=[])
        def approve(request):
            actual=json.loads(request['input'])['original_input']['analysis_set'][0]['buy_plans']
            self.assertEqual(actual['lots'][0]['plan']['invalidation_price'],73.4)
            self.assertTrue(actual['lots'][0]['review_status']['review_due'])
            return json.dumps(dict(reviews=[dict(symbol='2891',approved=True,reason='測試核准')])),{}
        review=review_trades(dict(decisions=[trade]),dict(model='test',input=json.dumps(dict(
            analysis_set=[candidate],paper_simulation_rules=dict(minimum_commission_twd=20)))),approve)
        self.assertEqual(review['status'],'VALID',review)
        self.assertEqual(trade['decision'],'SELL')


if __name__ == '__main__':
    unittest.main()
