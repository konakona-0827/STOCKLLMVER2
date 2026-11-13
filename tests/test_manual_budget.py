"""Manual diagnostic cash reservations do not consume AI strategy allocation."""
from contextlib import closing
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from config import TAIPEI
from execution.account_guard import BuyingPower
from execution.auto_advice_executor import LiveExecutionConfig
from execution.execution_store import ExecutionStore
from execution.health_monitor import (_pending_buy_reservation,current_buy_block_reasons,
    persist_refresh_snapshot,run_health_check)


class ManualBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.store=ExecutionStore(self.root/'data/execution/live_execution.sqlite3')
        (self.root/'config').mkdir()
        (self.root/'config/execution_live.json').write_text(json.dumps(dict(enabled=True,min_available_to_buy_twd=5000,
            min_buy_fee_reserve_twd=20,minimum_commission_twd=1,max_daily_buy_twd=30000)),encoding='utf-8')
        analysis=self.root/'data/analysis/market_history.sqlite3';analysis.parent.mkdir(parents=True)
        with closing(sqlite3.connect(analysis)) as db:
            db.execute('CREATE TABLE paper_account(account_id INTEGER,capital_limit_cents INTEGER)')
            db.execute('INSERT INTO paper_account VALUES(1,1000000)')
            db.execute('CREATE TABLE positions(symbol TEXT,user_qty INTEGER,ai_managed_qty INTEGER)')
            db.commit()
        self.cfg=LiveExecutionConfig.load(self.root/'config/execution_live.json')

    def order(self,decision_id,*,manual):
        symbol='0050' if manual else '2330';qty=1 if manual else 2;price=80 if manual else 100
        detail=dict(seq13='1234567890123' if manual else '9876543210123',quantity=qty,limit_price=str(price),
            send_return_code=0,filled_quantity=0,required_twd_with_buffer=str(price*qty+20))
        if manual:detail.update(ownership='MANUAL_DIAGNOSTIC',management_policy='RECORD_ONLY_NO_AI_ACTION')
        self.store.claim(decision_id,'diagnostic' if manual else 'flow',symbol,'BUY')
        self.store.mark_send_pending(decision_id,detail);self.store.mark_submitted(decision_id,detail)
        self.store.finish(decision_id,'ACKNOWLEDGED',detail)
        return detail

    def report(self,**overrides):
        result=dict(checked_at=datetime.now(TAIPEI).isoformat(),status='OK',errors=[],inventory_checked=True,
            broker_balance_twd='30000',broker_one_account_balance_twd='30000',minimum_balance_twd='5000',
            inventory_comparison=[],broker_order_reconciliation=[],pending_buy_reserve_twd='0',
            manual_pending_buy_reserve_twd='100',account_pending_buy_reserve_twd='100',
            ai_committed_capital_twd='0',strategy_cap_twd='10000',daily_buy_limit_twd='30000',daily_buy_reserved_twd='0')
        result.update(overrides);return result

    def health(self):
        def replay(seq,*_args,**_kwargs):
            return SimpleNamespace(filled_quantity=0,accepted_rows=['ACK'] if seq=='9876543210123' else [],
                fill_rows=[],cancel_rows=[],replay_complete=True,report9_contains_seq=seq=='9876543210123')
        session=SimpleNamespace(ensure_ready=lambda:SimpleNamespace(action='REUSE'),crosscheck_seq13=replay)
        power=BuyingPower(True,True,Decimal(30000),Decimal(30000),Decimal(30000),'ISOLATED')
        with patch('execution.health_monitor.query_buying_power',return_value=power),patch(
            'execution.health_monitor.query_inventory',return_value=[]),patch(
            'execution.health_monitor.query_settlement_dues',return_value=[]):
            return run_health_check(self.root,session=session)

    def test_pending_reservation_api_still_returns_manual_and_ai_total(self):
        self.order('manual',manual=True);detail=self.order('ai',manual=False)
        check=dict(decision_id='ai',seq13=detail['seq13'],status='NO_FILL_YET_VERIFIED',broker_replay_complete=True,
            unconfirmed_qty=2,broker_verified_filled_qty=0,broker_event_status='ACKNOWLEDGED_NO_FILL_EVENT')
        rows,total=_pending_buy_reservation(self.root,dict(broker_order_reconciliation=[check]),self.cfg)
        self.assertEqual(total,Decimal(320));self.assertEqual(len(rows),2)
        self.assertEqual(sum(Decimal(r['reserve_twd']) for r in rows if r.get('ownership')=='MANUAL_DIAGNOSTIC'),Decimal(100))

    def test_live_health_splits_manual_and_ai_without_changing_daily_ai_allowance(self):
        self.order('manual',manual=True);self.order('ai',manual=False)
        report=self.health()
        self.assertEqual(report['errors'],[],report)
        self.assertEqual(Decimal(report['pending_buy_reserve_twd']),Decimal(220))
        self.assertEqual(Decimal(report['manual_pending_buy_reserve_twd']),Decimal(100))
        self.assertEqual(Decimal(report['account_pending_buy_reserve_twd']),Decimal(320))
        self.assertEqual(Decimal(report['daily_buy_reserved_twd']),Decimal(220))
        snapshot=persist_refresh_snapshot(self.root,report)
        self.assertEqual(Decimal(snapshot['strategy_remaining_twd']),Decimal(9780))
        self.assertEqual(Decimal(snapshot['daily_buy_remaining_twd']),Decimal(29780))
        self.assertEqual(Decimal(snapshot['broker_remaining_after_pending_twd']),Decimal(24680))

    def test_manual_only_reservation_leaves_full_ai_strategy_and_daily_allowance(self):
        self.order('manual',manual=True)
        report=self.health();snapshot=persist_refresh_snapshot(self.root,report)
        self.assertEqual(Decimal(report['pending_buy_reserve_twd']),0)
        self.assertEqual(Decimal(report['manual_pending_buy_reserve_twd']),100)
        self.assertEqual(Decimal(snapshot['strategy_remaining_twd']),10000)
        self.assertEqual(Decimal(snapshot['daily_buy_remaining_twd']),30000)
        self.assertEqual(Decimal(snapshot['broker_remaining_after_pending_twd']),24900)
        self.assertEqual(Decimal(snapshot['current_buy_budget_twd']),10000)
        self.assertEqual(snapshot['budget_status'],'READY')

    def test_manual_pending_does_not_trigger_ai_cap_but_still_reduces_real_cash(self):
        report=self.report(ai_committed_capital_twd='9950')
        self.assertNotIn('AI_CAPITAL_LIMIT_REACHED',current_buy_block_reasons(report))
        snapshot=persist_refresh_snapshot(self.root,report)
        self.assertEqual(Decimal(snapshot['strategy_remaining_twd']),50)
        self.assertEqual(Decimal(snapshot['current_buy_budget_twd']),50)
        self.assertEqual(Decimal(snapshot['broker_remaining_after_pending_twd']),24900)
        self.assertEqual(snapshot['manual_pending_buy_reserve_twd'],'100')
        self.assertEqual(snapshot['account_pending_buy_reserve_twd'],'100')

    def test_manual_cash_reserve_can_block_buying_when_broker_headroom_is_insufficient(self):
        report=self.report(broker_balance_twd='5080')
        self.assertIn('NO_BROKER_HEADROOM_AFTER_RESERVE',current_buy_block_reasons(report))
        snapshot=persist_refresh_snapshot(self.root,report)
        self.assertEqual(Decimal(snapshot['strategy_remaining_twd']),10000)
        self.assertEqual(Decimal(snapshot['broker_remaining_after_pending_twd']),0)
        self.assertEqual(snapshot['budget_status'],'BLOCKED')

    def test_legacy_reports_without_new_reserve_fields_keep_previous_budget_math(self):
        report=self.report(pending_buy_reserve_twd='1200',ai_committed_capital_twd='3000')
        report.pop('manual_pending_buy_reserve_twd');report.pop('account_pending_buy_reserve_twd')
        snapshot=persist_refresh_snapshot(self.root,report)
        self.assertEqual(Decimal(snapshot['strategy_remaining_twd']),5800)
        self.assertEqual(Decimal(snapshot['broker_remaining_after_pending_twd']),23800)
        self.assertEqual(Decimal(snapshot['current_buy_budget_twd']),5800)
        self.assertEqual(snapshot['manual_pending_buy_reserve_twd'],'0')
        self.assertEqual(snapshot['account_pending_buy_reserve_twd'],'1200')


if __name__=='__main__':unittest.main()
