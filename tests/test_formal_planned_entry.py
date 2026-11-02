from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from config import TAIPEI
from execution.planned_entry import contract, verify, validate_price
from execution.models import AdviceIntent, Side, ExecutionStatus
from execution.risk_guard import MarketQuote, RiskRejected
from execution.auto_advice_executor import LiveExecutionConfig, execute_advice_file
from execution.account_snapshot import ExecutionAccountSnapshot
from execution.account_guard import BuyingPower
from execution.execution_store import ExecutionStore
from execution.capital_executor import SendResult
from trade_economics import evaluate_buy

AT = datetime(2026, 11, 2, 10, 0, tzinfo=TAIPEI)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return AT.astimezone(tz) if tz else AT.astimezone().replace(tzinfo=None)


class FormalPlannedTests(unittest.TestCase):
    def setup_plan(self):
        decision = dict(suggested_price=98.5, target_price=130, invalidation_price=97, reason='wait pullback')
        candidate = dict(quote_timestamp=AT.isoformat(), asset_type='STOCK')
        entry = contract(candidate, decision, AT)
        rec = dict(asset_type='STOCK', pricing_policy='PLANNED_LIMIT', entry_plan=entry,
                   buy_plan=dict(target_price=130, invalidation_price=97))
        intent = AdviceIntent('run', 'run:2330', '1.0', '2330', Side.BUY, Decimal(80), 'test', None, (),
            'LIVE', AT.isoformat(), Decimal(0), Decimal(100), Decimal('98.5'), 5, Decimal(0), 0,
            pricing_policy='PLANNED_LIMIT')
        q = MarketQuote('2330','LIVE',Decimal(0),Decimal(100),Decimal('99.9'),Decimal(100))
        return intent, rec, q

    def test_price_economics_use_willing_price_not_current_ask(self):
        q = dict(ask=100, bid=99.9, asset_type='STOCK')
        kw = dict(qty=5,limit_price=98.5,target_price=130,invalidation_price=97,minimum_commission_twd=20)
        current = evaluate_buy(q, **kw)
        planned = evaluate_buy(dict(q,pricing_policy='PLANNED_LIMIT'), **kw)
        self.assertEqual(current['entry_twd'], 100)
        self.assertEqual(planned['entry_twd'], 98.5)
        self.assertGreater(planned['estimated_net_upside_twd'], current['estimated_net_upside_twd'])

    def test_contract_requires_valid_session_bracket_and_unchanged_price(self):
        i, r, q = self.setup_plan()
        verify(i, r, q, AT)
        for instant in (AT.replace(hour=8), AT.replace(hour=14), AT+timedelta(days=1)):
            with self.assertRaises(RiskRejected):
                verify(i,r,q,instant)
        with self.assertRaises(RiskRejected):
            verify(replace(i,suggested_price=Decimal(99)),r,q,AT)
        with self.assertRaises(RiskRejected):
            verify(i,r,replace(q,last_price=Decimal(96)),AT)

    def test_stock_and_etf_ticks(self):
        self.assertEqual(validate_price('98.55','ETF'),Decimal('98.55'))
        with self.assertRaises(RiskRejected):
            validate_price('98.55','STOCK')
        for price in ('NaN','0','Infinity'):
            with self.assertRaises(RiskRejected):
                validate_price(price,'STOCK')

    def test_opt_in_does_not_change_default_live_config(self):
        self.assertFalse(LiveExecutionConfig().risk_config().allow_planned_limits)
        self.assertTrue(LiveExecutionConfig(planned_limits_enabled=True).risk_config().allow_planned_limits)

    def test_original_formal_ledger_ack_waits_for_exact_broker_fill(self):
        i, rec, q = self.setup_plan()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = root/'execution.json'
            cfg.write_text(json.dumps(dict(enabled=True,planned_limits_enabled=True,
                min_available_to_buy_twd=0,min_buy_fee_reserve_twd=20,max_daily_buy_twd=30000,observe_seconds=0)))
            path = root/'existing.sqlite3'
            store = ExecutionStore(path)
            store.bootstrap_ai_qty('1101',99)
            advice = dict(interface_version='1.0',run_id='run',run_status='READY',llm_validation='VALID',
                pretrade_review_status='VALID',real_order_sent=False,
                market=dict(quotes=[dict(symbol='2330',quote_status='LIVE',price=100,bid=99.9,ask=100)]),
                recommendations=[dict(rec,symbol='2330',decision='BUY',confidence=.8,suggested_qty=5,
                    suggested_price=98.5,action_ratio=0,quote_status='LIVE',quote_timestamp=AT.isoformat())],
                execution_budget=dict(available_cash_twd=5000,strategy_cap_twd=10000))
            ap = root/'advice.json'; ap.write_text(json.dumps(advice))
            power = BuyingPower(True,True,Decimal(20000),Decimal(20000),Decimal(20000),'mock')
            snapshot = ExecutionAccountSnapshot(AT.isoformat(),power)
            session = SimpleNamespace(connected=True,ensure_ready=Mock(return_value=SimpleNamespace(
                ready=True,reply_state=1,reply_connected=True,action='REUSE')))
            def send(**kwargs):
                self.assertEqual(kwargs['price'],Decimal('98.5'))
                kwargs['on_submitted'](SendResult(0,'accepted','1234567890123','mock'))
                return SimpleNamespace(status=ExecutionStatus.ACKNOWLEDGED,quantity=5,price=Decimal('98.5'),
                    send_return_code=0,seq13='1234567890123',filled_quantity=0,broker_order_sent=True,
                    send_attempted=True,crosscheck_performed=True,used_fallback=False,changes_position=False,
                    crosscheck=SimpleNamespace(status=ExecutionStatus.ACKNOWLEDGED,accepted_rows=['accepted'],
                        fill_rows=[],cancel_rows=[],used_fallback=False))
            with patch('execution.auto_advice_executor.datetime',Clock), \
                    patch('execution.auto_advice_executor.query_buying_power',return_value=power), \
                    patch('execution.auto_advice_executor.CapitalOddLotExecutor.send_limit_order',side_effect=send) as sender:
                report = execute_advice_file(ap,project_root=root,config_path=cfg,session=session,
                                             account_snapshot=snapshot,execution_db_path=path)
                self.assertEqual(report['results'][0]['status'],'ACKNOWLEDGED',report)
                self.assertEqual(store.get_ai_qty('2330'),0)
                self.assertEqual(store.get_ai_qty('1101'),99)
                self.assertEqual(store.pending_reconciliation_symbols(),{'2330'})
                again = execute_advice_file(ap,project_root=root,config_path=cfg,session=session,
                                           account_snapshot=snapshot,execution_db_path=path)
                self.assertEqual(again['results'][0]['status'],'SKIPPED_DUPLICATE')
                advice['run_id'] = 'later-run'
                ap.write_text(json.dumps(advice))
                later = execute_advice_file(ap,project_root=root,config_path=cfg,session=session,
                                           account_snapshot=snapshot,execution_db_path=path)
                self.assertEqual(later['results'][0]['status'],'SKIPPED_CHECK_FAILED')
                self.assertIn('earlier order',later['results'][0]['reason'])
                self.assertEqual(sender.call_count,1)
            store.reconcile_broker_fill('run:2330','1234567890123',5,
                                       [dict(quantity=5,price_twd='98.4')],5,0)
            self.assertEqual(store.get_ai_qty('2330'),5)
            self.assertEqual(store.get_ai_qty('1101'),99)
            with store._connect() as db:
                d=json.loads(db.execute("SELECT detail_json FROM execution_attempts WHERE decision_id='run:2330'").fetchone()[0])
                self.assertEqual(d['entry_plan']['limit_price'],98.5)
                self.assertEqual(d['pricing_policy'],'PLANNED_LIMIT')
