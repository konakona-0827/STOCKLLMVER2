"""Manual diagnostic receipts must not become AI holdings or AI history."""
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from config import TAIPEI
from execution.auto_advice_executor import LiveExecutionConfig
from execution.execution_store import ExecutionStore
from execution.health_monitor import (_ai_committed_capital,_pending_buy_reservation,
    current_buy_block_reasons,reconcile_confirmed_broker_events)
from execution.models import Side
from execution.trade_dates import dated_ai_trades
from execution.trade_pnl import remaining_acquisition_costs,realized_price_difference


class ManualDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.store=ExecutionStore(self.root/'data/execution/live_execution.sqlite3')
        self.cfg=LiveExecutionConfig(enabled=True,min_buy_fee_reserve_twd=Decimal(20),minimum_commission_twd=Decimal(1))

    def order(self,decision_id,*,manual=False,status='FILLED',filled=1,price='80',qty=1):
        detail=dict(seq13='1234567890123' if manual else '9876543210123',quantity=qty,limit_price=price,
            send_return_code=0,filled_quantity=filled,required_twd_with_buffer=str(Decimal(price)*qty+20),
            time_in_force='ROD',market_session='INTRADAY_ODD_LOT')
        if manual:
            detail.update(ownership='MANUAL_DIAGNOSTIC',management_policy='RECORD_ONLY_NO_AI_ACTION',
                analysis_source='USER_CONFIRMED_HEALTH_ASSUMPTION',requested_outside_session=True,
                valid_trading_date='2026-11-16')
        self.store.claim(decision_id,'manual-diagnostic' if manual else 'normal-flow','0050','BUY')
        self.store.mark_send_pending(decision_id,detail)
        self.store.mark_submitted(decision_id,detail)
        if filled:
            self.store.finish_with_fill(decision_id,status,detail,'0050',Side.BUY,filled)
        elif status!='SUBMITTED':
            self.store.finish(decision_id,status,detail)
        return detail

    def test_confirmed_manual_diagnostic_fill_is_recorded_without_adding_ai_shares(self):
        self.order('normal-ai',qty=3,filled=3,price='100')
        self.order('diagnostic',manual=True)
        self.assertEqual(self.store.get_ai_qty('0050'),3)
        receipt=self.store.events_for('diagnostic')[-1]
        self.assertEqual(receipt['status'],'FILLED')
        self.assertEqual(receipt['detail']['filled_quantity'],1)
        self.assertEqual(receipt['detail']['ownership'],'MANUAL_DIAGNOSTIC')
        self.assertEqual(receipt['detail']['management_policy'],'RECORD_ONLY_NO_AI_ACTION')
        self.assertFalse(self.store.claim('diagnostic','manual-diagnostic','0050','BUY'))

    def test_next_day_broker_fill_updates_only_manual_receipt_and_is_idempotent(self):
        self.order('normal-ai',qty=3,filled=3,price='100')
        self.order('diagnostic',manual=True,status='ACKNOWLEDGED',filled=0)
        fills=[dict(quantity=1,price_twd='79.95')]
        first=self.store.reconcile_broker_fill('diagnostic','1234567890123',1,fills,
            broker_cash_qty=6,configured_user_qty=3)
        self.assertTrue(first['changed']);self.assertEqual(self.store.get_ai_qty('0050'),3)
        second=self.store.reconcile_broker_fill('diagnostic','1234567890123',1,fills,
            broker_cash_qty=6,configured_user_qty=3)
        self.assertFalse(second['changed']);self.assertEqual(self.store.get_ai_qty('0050'),3)
        with self.store._connect() as con:
            status,raw=con.execute('SELECT status,detail_json FROM execution_attempts WHERE decision_id=?',('diagnostic',)).fetchone()
        detail=json.loads(raw)
        self.assertEqual(status,'FILLED');self.assertEqual(detail['filled_quantity'],1)
        self.assertEqual(detail['broker_reconciled_seq13'],'1234567890123')
        self.assertEqual(detail['ownership'],'MANUAL_DIAGNOSTIC')

    def test_manual_diagnostic_does_not_pollute_ai_acquisition_cost_or_price_difference(self):
        self.order('normal-ai',qty=3,filled=3,price='100')
        before=_ai_committed_capital(self.root,self.cfg)
        self.order('diagnostic',manual=True)
        after=_ai_committed_capital(self.root,self.cfg)
        self.assertEqual(before,after);self.assertEqual(after[0],Decimal(320))
        remaining=remaining_acquisition_costs(self.store.path,[])
        self.assertEqual(remaining['0050']['quantity'],3)
        self.assertEqual(Decimal(remaining['0050']['average_unit_cost_twd']),Decimal(100))
        self.assertEqual(realized_price_difference(self.store.path,[]),{})

    def test_manual_diagnostic_is_not_presented_as_an_ai_purchase_to_model_history(self):
        self.order('normal-ai',qty=3,filled=3,price='100')
        self.order('diagnostic',manual=True)
        rows=dated_ai_trades(self.store.path,['0050'])['0050']['records']
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]['side'],rows[0]['quantity']),('BUY',3))

    def test_next_day_pending_manual_preorder_keeps_cash_reserved_without_ai_management(self):
        detail=self.order('diagnostic',manual=True,status='ACKNOWLEDGED',filled=0)
        with self.store._connect() as con:
            con.execute('UPDATE execution_attempts SET started_at=? WHERE decision_id=?',
                ('2026-11-13T17:00:00+08:00','diagnostic'))
        monday=datetime(2026,11,16,9,0,tzinfo=TAIPEI)
        self.assertEqual(self.store.expire_rod_orders(at=monday),[])
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return monday.astimezone(tz) if tz else monday.replace(tzinfo=None)
        with patch('execution.execution_store.datetime',Clock):
            self.assertEqual(self.store.daily_reserved_buy_twd(),Decimal(0))
        report=dict(broker_order_reconciliation=[dict(decision_id='diagnostic',seq13=detail['seq13'],
            broker_replay_complete=True,status='NO_FILL_YET_VERIFIED',unconfirmed_qty=1,
            broker_verified_filled_qty=0,broker_event_status='ORDER_ACCEPTED')])
        rows,reserved=_pending_buy_reservation(self.root,report,self.cfg)
        self.assertEqual(reserved,Decimal(100));self.assertEqual(rows[0]['remaining_qty'],1)
        self.assertEqual(_ai_committed_capital(self.root,self.cfg)[0],Decimal(0))
        from execution.exit_cancel import pending_exit
        self.assertIsNone(pending_exit(self.store,'0050'))
        receipt=self.store.events_for('diagnostic')[-1]
        self.assertEqual(receipt['status'],'ACKNOWLEDGED')
        self.assertEqual(receipt['detail']['management_policy'],'RECORD_ONLY_NO_AI_ACTION')

    def test_manual_pending_and_filled_order_do_not_consume_ai_daily_allowance(self):
        at=datetime(2026,11,13,10,0,tzinfo=TAIPEI)
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return at.astimezone(tz) if tz else at.replace(tzinfo=None)
        with patch('execution.execution_store.datetime',Clock):
            self.order('normal-ai',qty=3,filled=3,price='100')
            baseline=self.store.daily_reserved_buy_twd()
            self.assertEqual(baseline,Decimal(320))
            self.order('diagnostic',manual=True,status='ACKNOWLEDGED',filled=0)
            self.assertEqual(self.store.daily_reserved_buy_twd(),baseline)
            result=self.store.reconcile_broker_fill('diagnostic','1234567890123',1,
                [dict(quantity=1,price_twd='79.95')],broker_cash_qty=4,configured_user_qty=1)
            self.assertTrue(result['changed']);self.assertEqual(self.store.get_ai_qty('0050'),3)
            self.assertEqual(self.store.daily_reserved_buy_twd(),baseline)

    def test_incomplete_manual_fill_evidence_is_warning_and_keeps_cash_without_global_ai_block(self):
        self.order('normal-ai',qty=3,filled=3,price='100')
        detail=self.order('diagnostic',manual=True,status='ACKNOWLEDGED',filled=0)
        for complete in (False,True):
            with self.subTest(broker_replay_complete=complete):
                check=dict(decision_id='diagnostic',symbol='0050',side='BUY',seq13=detail['seq13'],
                    ownership='MANUAL_DIAGNOSTIC',management_policy='RECORD_ONLY_NO_AI_ACTION',
                    status='FILL_MISMATCH',broker_replay_complete=complete,
                    ledger_filled_qty=0,broker_replay_filled_qty=1,broker_verified_filled_qty=1,
                    broker_replay_fill_details=[],broker_event_counts=dict(cancel=0),ordered_qty=1,unconfirmed_qty=1)
                report=dict(errors=[],inventory_checked=True,status='OK',broker_balance_twd='30000',
                    strategy_cap_twd='10000',minimum_balance_twd='5000',ai_committed_capital_twd='320',
                    pending_buy_reserve_twd='100',daily_buy_limit_twd='30000',daily_buy_reserved_twd='100',
                    inventory_comparison=[dict(symbol='0050',broker_cash_qty=6,configured_user_qty=3,status='MATCH')],
                    broker_order_reconciliation=[check])
                result=reconcile_confirmed_broker_events(self.root,report)
                self.assertEqual(result['errors'],[]);self.assertEqual(report['errors'],[])
                self.assertTrue(result.get('manual_diagnostic_warnings'))
                self.assertEqual(self.store.get_ai_qty('0050'),3)
                _rows,reserved=_pending_buy_reservation(self.root,report,self.cfg)
                self.assertEqual(reserved,Decimal(100))
                self.assertEqual(current_buy_block_reasons(report),[])

    def test_manual_receipt_does_not_replace_live_plan_or_invalidate_handoff_cost_basis(self):
        from trade_plan_context import live_plans
        from opportunity_flow.handoff_basis import remaining_manual_basis
        detail=self.order('normal-ai',qty=3,filled=3,price='100')
        detail['buy_plan']=dict(target_price=120,invalidation_price=95,holding_days=10,reason='verified original plan')
        with self.store._connect() as con:
            con.execute('UPDATE execution_attempts SET detail_json=? WHERE decision_id=?',
                (json.dumps(detail),'normal-ai'))
        before=live_plans(self.store.path,{'0050':3})['0050']
        self.assertTrue(before['available'])
        manual=self.order('diagnostic',manual=True)
        after=live_plans(self.store.path,{'0050':3})['0050']
        self.assertEqual(after,before)
        handoffs=[dict(previous_ai_qty=0,quantity=3,unit_cost_twd='100',transferred_at='2026-11-10T10:00:00+08:00')]
        attempts=[dict(side='BUY',started_at='2026-11-16T10:00:00+08:00',detail_json=json.dumps(manual))]
        self.assertEqual(remaining_manual_basis(handoffs,attempts,3),'100')

    def test_manual_pending_order_is_not_locally_expired_or_changed_after_estimated_trading_day(self):
        self.order('diagnostic',manual=True,status='ACKNOWLEDGED',filled=0)
        after_target=datetime(2026,11,17,17,0,tzinfo=TAIPEI)
        self.assertEqual(self.store.expire_rod_orders(at=after_target),[])
        events=self.store.events_for('diagnostic')
        self.assertEqual(events[-1]['status'],'ACKNOWLEDGED')
        self.assertFalse(any(row['status']=='ROD_EXPIRED' for row in events))
        self.assertEqual(events[-1]['detail']['management_policy'],'RECORD_ONLY_NO_AI_ACTION')


if __name__=='__main__':unittest.main()
