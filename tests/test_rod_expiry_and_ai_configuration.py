from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from config import TAIPEI
import position_store
from analysis_service import build_union
from execution.account_guard import BuyingPower
from execution.account_snapshot import ExecutionAccountSnapshot
from execution.auto_advice_executor import execute_advice_file, LiveExecutionConfig
from execution.execution_store import ExecutionStore
from execution.health_monitor import (_ai_committed_capital, persist_refresh_snapshot, run_health_check)
from execution.models import Side
from execution.position_configuration import save_ai_holding_configuration


class RodAndConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'config').mkdir()
        self.config_path = self.root/'config'/'execution_live.json'
        self.config_path.write_text(json.dumps(dict(enabled=True, min_available_to_buy_twd=0)), encoding='utf-8')
        self.store = ExecutionStore(self.root/'data'/'execution'/'live_execution.sqlite3')
        self.analysis = self.root/'data'/'analysis'/'market_history.sqlite3'
        position_store.connect(self.analysis).close()
        with closing(sqlite3.connect(self.analysis)) as con:
            con.execute('CREATE TABLE paper_account(account_id INTEGER,capital_limit_cents INTEGER)')
            con.execute('INSERT INTO paper_account VALUES(1,1000000)')
            con.commit()

    def order(self, decision_id='D1', *, status='ACKNOWLEDGED', filled=0,
              side='BUY', start='2026-10-12T13:00:00+08:00', **extra):
        detail = dict(seq13='1234567890123', quantity=10, limit_price='100',
                      send_return_code=0, filled_quantity=filled,
                      required_twd_with_buffer='1020', **extra)
        self.store.claim(decision_id, 'R1', '0050', side)
        self.store.mark_send_pending(decision_id, detail)
        self.store.mark_submitted(decision_id, detail)
        if filled:
            self.store.finish_with_fill(decision_id, status, detail, '0050', Side(side), filled)
        elif status != 'SUBMITTED':
            self.store.finish(decision_id, status, detail)
        with self.store._connect() as con:
            con.execute('UPDATE execution_attempts SET started_at=? WHERE decision_id=?', (start, decision_id))
        return detail

    def snapshot(self, *, qty=0, broker_qty=20, cost='0', at=None):
        at = at or datetime.now(TAIPEI)
        return dict(checked_at=at.isoformat(), broker_inventory_verified=True, errors=[],
                    ai_positions={'0050': qty},
                    inventory_comparison=[dict(symbol='0050', broker_cash_qty=broker_qty,
                                               ai_qty=qty, configured_user_qty=broker_qty-qty, status='MATCH')],
                    ai_committed_positions=[dict(symbol='0050', qty=qty, committed_cost_twd=cost)])

    def configure(self, qty, snapshot=None, **kwargs):
        return save_ai_holding_configuration(self.root, self.analysis, '0050', qty,
                                             snapshot or self.snapshot(), **kwargs)

    def test_rod_expires_at_taipei_midnight_and_is_idempotent(self):
        self.order(start='2026-10-12T15:59:59+00:00')  # Taipei 23:59:59
        self.assertEqual([], self.store.expire_rod_orders(at=datetime(2026,10,12,23,59,59,tzinfo=TAIPEI)))
        changes = self.store.expire_rod_orders(at=datetime(2026,10,12,16,0,tzinfo=timezone.utc))
        self.assertEqual('EXPIRED', changes[0]['status'])
        self.assertEqual(10, changes[0]['expired_qty'])
        self.assertEqual(set(), self.store.pending_reconciliation_symbols())
        self.assertEqual([], self.store.unresolved_attempts())
        self.assertEqual([], self.store.expire_rod_orders(at=datetime(2026,10,14,tzinfo=TAIPEI)))
        self.assertEqual(1, sum(e['status']=='ROD_EXPIRED' for e in self.store.events_for('D1')))
        with self.store._connect() as con:
            self.assertEqual(1, con.execute('SELECT count(*) FROM execution_attempts').fetchone()[0])

    def test_partial_fill_is_retained_and_late_broker_fill_remains_reconcilable(self):
        self.order(status='PARTIALLY_FILLED', filled=3, crosscheck_performed=True, deal_event_count=1)
        changes = self.store.expire_rod_orders(at=datetime(2026,10,13,tzinfo=TAIPEI))
        self.assertEqual('PARTIALLY_FILLED_EXPIRED', changes[0]['status'])
        self.assertEqual(3, self.store.get_ai_qty('0050'))
        fills = [dict(quantity=5, price_twd='99')]
        result = self.store.reconcile_broker_fill('D1', '1234567890123', 5, fills, 5, 0)
        self.assertEqual('PARTIALLY_FILLED_EXPIRED', result['status'])
        self.assertEqual(5, self.store.get_ai_qty('0050'))
        self.assertFalse(self.store.reconcile_broker_fill('D1','1234567890123',5,fills,5,0)['changed'])
        result = self.store.reconcile_broker_fill('D1', '1234567890123', 10,
                                                [dict(quantity=10,price_twd='99')], 10, 0)
        self.assertEqual('FILLED',result['status'])
        with self.store._connect() as con:
            detail=json.loads(con.execute("SELECT detail_json FROM execution_attempts WHERE decision_id='D1'").fetchone()[0])
        self.assertEqual(0,detail['expired_quantity'])

    def test_expired_partial_sell_preserves_the_remaining_holding(self):
        self.store.bootstrap_ai_qty('0050',10)
        self.order(status='PARTIALLY_FILLED',filled=3,side='SELL')
        self.store.expire_rod_orders(at=datetime(2026,10,13,tzinfo=TAIPEI))
        self.assertEqual(7,self.store.get_ai_qty('0050'))
        self.assertEqual(set(),self.store.pending_reconciliation_symbols())

    def test_today_and_other_order_types_are_not_expired(self):
        self.order('today', start='2026-10-13T01:00:00+08:00')
        self.order('ioc', time_in_force='IOC')
        self.order('other-session', market_session='AFTER_HOURS')
        self.order('delayed-send', submitted_at='2026-10-13T09:00:00+08:00')
        self.assertEqual([], self.store.expire_rod_orders(at=datetime(2026,10,13,10,tzinfo=TAIPEI)))

    def test_submitted_rod_after_crash_can_expire(self):
        self.order(status='SUBMITTED', time_in_force='ROD')
        self.assertEqual('EXPIRED', self.store.expire_rod_orders(at=datetime(2026,10,13,tzinfo=TAIPEI))[0]['status'])

    def test_health_releases_expired_reserve_without_claiming_a_cancel(self):
        self.order(start=(datetime.now(TAIPEI)-timedelta(days=3)).isoformat())
        power = BuyingPower(True,True,Decimal('30000'),Decimal('30000'),Decimal('30000'),'raw')
        cross = SimpleNamespace(filled_quantity=0,accepted_rows=[],fill_rows=[],cancel_rows=[],
                                replay_complete=True,report9_contains_seq=False)
        session = SimpleNamespace(ensure_ready=lambda: SimpleNamespace(action='REUSE'),
                                  crosscheck_seq13=lambda *a,**kw: cross)
        with patch('execution.health_monitor.query_buying_power',return_value=power), \
                patch('execution.health_monitor.query_inventory',return_value=[]), \
                patch('execution.health_monitor.query_settlement_dues',return_value=[]):
            report = run_health_check(self.root,session=session)
        self.assertEqual('0',report['pending_buy_reserve_twd'])
        self.assertEqual([],report['unresolved_attempts'])
        row = report['broker_order_reconciliation'][0]
        self.assertEqual('ROD_EXPIRED',row['status'])
        self.assertEqual(0,row['unconfirmed_qty'])
        self.assertEqual(0,row['cancelled_qty'])
        self.assertEqual(10,row['expired_qty'])
        self.assertEqual('READY',persist_refresh_snapshot(self.root,report)['budget_status'])

    def test_setting_transfers_exact_quantity_and_zero_disables_both_sides(self):
        result = self.configure(10, unit_cost_twd='100')
        self.assertEqual(dict(symbol='0050',ai_qty=10,user_qty=10,enabled=True),result)
        cfg = LiveExecutionConfig.load(self.config_path)
        self.assertEqual(Decimal('1020'),_ai_committed_capital(self.root,cfg)[0])
        self.configure(4,self.snapshot(qty=10,cost='1020'))
        self.assertEqual(Decimal('408'),_ai_committed_capital(self.root,cfg)[0])
        self.configure(0,self.snapshot(qty=4,cost='408'))
        self.assertEqual(0,self.store.get_ai_qty('0050'))
        self.assertFalse(self.store.ai_trading_enabled('0050'))
        self.assertEqual(20,position_store.load(self.analysis)[0]['user_qty'])
        self.assertEqual(Decimal('0'),_ai_committed_capital(self.root,cfg)[0])
        self.configure(3,unit_cost_twd='90')
        self.assertTrue(self.store.ai_trading_enabled('0050'))
        self.assertEqual(Decimal('290'),_ai_committed_capital(self.root,cfg)[0])

    def test_checkpoint_replays_only_subsequent_fills(self):
        self.order(status='FILLED',filled=10)
        self.configure(4,self.snapshot(qty=10,cost='1020'))
        self.store.claim('D2','R2','0050','SELL')
        detail=dict(quantity=2,filled_quantity=2,limit_price='100')
        self.store.mark_send_pending('D2',detail)
        self.store.mark_submitted('D2',detail)
        self.store.finish_with_fill('D2','FILLED',detail,'0050',Side.SELL,2)
        self.assertEqual(Decimal('204'),_ai_committed_capital(self.root,LiveExecutionConfig.load(self.config_path))[0])

    def test_late_old_fill_after_zero_setting_preserves_user_ownership(self):
        self.order(start=(datetime.now(TAIPEI)-timedelta(days=2)).isoformat())
        self.store.expire_rod_orders()
        self.configure(0,self.snapshot(broker_qty=10))
        self.store.reconcile_broker_fill('D1','1234567890123',10,
                                        [dict(quantity=10,price_twd='100')],10,10)
        self.assertEqual(0,self.store.get_ai_qty('0050'))
        self.assertFalse(self.store.ai_trading_enabled('0050'))
        self.assertEqual(Decimal('0'),_ai_committed_capital(self.root,LiveExecutionConfig.load(self.config_path))[0])

    def test_invalid_or_stale_configuration_does_not_change_either_ledger(self):
        for qty, snapshot, kwargs in [
            (21,self.snapshot(),{'unit_cost_twd':'100'}),
            (-1,self.snapshot(),{}), (True,self.snapshot(),{}),
            (1,self.snapshot(),{}),
            (1,self.snapshot(),{'unit_cost_twd':'NaN'}),
            (0,self.snapshot(at=datetime.now(TAIPEI)-timedelta(seconds=121)),{}),
            (0,{**self.snapshot(),'broker_inventory_verified':False},{}),
        ]:
            with self.subTest(qty=qty,kwargs=kwargs), self.assertRaises(ValueError):
                self.configure(qty,snapshot,**kwargs)
        self.assertIsNone(self.store.get_ai_qty('0050'))
        self.assertEqual([],position_store.load(self.analysis))

    def test_pending_order_blocks_ownership_changes(self):
        self.order(start=datetime.now(TAIPEI).isoformat())
        with self.assertRaisesRegex(ValueError,'未結委託'):
            self.configure(0)
        self.assertTrue(self.store.ai_trading_enabled('0050'))

    def test_zero_can_disable_holding_even_when_old_cost_cannot_be_rebuilt(self):
        self.store.bootstrap_ai_qty('0050',5)
        self.configure(0,self.snapshot(qty=5))
        self.assertFalse(self.store.ai_trading_enabled('0050'))
        self.assertEqual(Decimal('0'),_ai_committed_capital(self.root,LiveExecutionConfig.load(self.config_path))[0])

    def test_dashboard_prefills_selected_stock_and_routes_save_to_formal_configuration(self):
        import tkinter as tk
        from tkinter import ttk
        from dashboard import Dashboard
        gui = tk.Tk()
        gui.withdraw()
        self.addCleanup(gui.destroy)
        with patch('dashboard.ROOT',self.root), patch.object(Dashboard,'refresh'), \
                patch.object(Dashboard,'update_live_strategy_label'):
            app=Dashboard(gui,self.analysis.parent)
            app.last_health=self.snapshot(qty=5,cost='500')
            app.last_health['ai_position_settings']={'0050':{'enabled':False}}
            app.render()
            item=app.position_tree.get_children()[0]
            self.assertEqual('禁止 AI 買賣',app.position_tree.set(item,'AI 運用'))
            app.position_tree.selection_set(item)
            app.position_settings()
            win=next(child for child in gui.winfo_children() if isinstance(child,tk.Toplevel))
            win.withdraw()
            entry=next(child for child in win.winfo_children() if isinstance(child,ttk.Entry)
                       and int(child.grid_info()['row'])==2)
            self.assertEqual('5',entry.get())
            entry.delete(0,'end')
            entry.insert(0,'0')
            button=next(child for child in win.winfo_children() if isinstance(child,ttk.Button))
            with patch('execution.position_configuration.configure_ai_holding',return_value={
                    'symbol':'0050','ai_qty':0,'user_qty':20,'enabled':False}) as configure:
                button.invoke()
                kind,payload=app.events.get(timeout=3)
            self.assertEqual('position_saved',kind)
            configure.assert_called_once_with(self.root,self.analysis,'0050',0,unit_cost_twd=None)

    def test_database_error_rolls_back_configuration_in_both_files(self):
        with self.store._connect() as con:
            con.execute('''CREATE TRIGGER fail_adjustment BEFORE INSERT ON ai_position_adjustments
                BEGIN SELECT RAISE(ABORT,'test rollback'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.configure(10,unit_cost_twd='100')
        self.assertIsNone(self.store.get_ai_qty('0050'))
        self.assertEqual({},self.store.position_settings())
        self.assertEqual([],position_store.load(self.analysis))

    def test_disabled_stock_is_excluded_from_live_ai_candidates(self):
        self.configure(0)
        self.assertEqual([],build_union([{'symbol':'0050'}],[],[],[],self.analysis,
            datetime.now(TAIPEI).isoformat(),live_managed=True,ai_settings=self.store.position_settings()))

    def test_stale_advice_cannot_send_buy_or_sell_for_disabled_stock(self):
        self.configure(0)
        at = datetime.now(timezone.utc).isoformat()
        advice = dict(interface_version='1.0',generated_at=at,run_id='R',run_status='READY',
            llm_validation='VALID',real_order_sent=False,
            market={'quotes':[dict(symbol='0050',quote_status='LIVE',price=100,bid=99,ask=100,
                                   exchange_time=at,age_seconds=0)]},
            recommendations=[],execution_budget={'available_cash_twd':1000})
        session = SimpleNamespace(connected=True,ensure_ready=lambda: SimpleNamespace(
            ready=True,reply_state=1,reply_connected=True,action='REUSE'))
        for side in ('BUY','SELL'):
            advice['run_id']=side
            advice['recommendations']=[dict(symbol='0050',decision=side,confidence=80,
                suggested_qty=1,action_ratio=0,ai_managed_qty=20,quote_status='LIVE',
                quote_timestamp=at,quote_age_seconds=0)]
            path=self.root/'advice.json'
            path.write_text(json.dumps(advice),encoding='utf-8')
            with patch('execution.auto_advice_executor.CapitalOddLotExecutor.send_limit_order') as send:
                report=execute_advice_file(path,project_root=self.root,config_path=self.config_path,
                    session=session,account_snapshot=ExecutionAccountSnapshot(captured_at=at))
            send.assert_not_called()
            self.assertEqual('SKIPPED_CHECK_FAILED',report['results'][0]['status'])
            self.assertIn('AI trading disabled',report['results'][0]['reason'])


if __name__ == '__main__':
    unittest.main()
