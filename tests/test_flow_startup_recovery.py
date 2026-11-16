"""Startup can research with below-cap cash while all other account gates remain."""
from contextlib import redirect_stdout
from copy import deepcopy
from decimal import Decimal
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from analysis_service import live_buy_budget
from execution.execution_store import ExecutionStore
from execution.health_monitor import persist_refresh_snapshot
from opportunity_flow.common import policy,stamp
from opportunity_flow.data import load_dataset
from opportunity_flow.demo import create
from opportunity_flow.live import LiveCoordinator
from opportunity_flow.public_quotes import parse
from opportunity_flow.state import Ledger
from test_flow_execution_bridge import FakeRuntime
from test_opportunity_flow import FakeRunner


def account_report(**updates):
    return dict(dict(checked_at='2026-11-16T10:59:30+08:00',status='OK',errors=[],
        broker_balance_twd='8373',broker_one_account_balance_twd='9000',minimum_balance_twd='5000',
        daily_buy_limit_twd='20000',daily_buy_reserved_twd='0',strategy_cap_twd='10000',
        pending_buy_reserve_twd='0',account_pending_buy_reserve_twd='0',
        ai_committed_capital_twd='0',inventory_checked=True),**updates)


class StartupRecoveryTests(unittest.TestCase):
    def test_snapshot_and_analysis_keep_the_other_budget_limits(self):
        cases=[({},'3373'),
            (dict(ai_committed_capital_twd='9000'),'1000'),
            (dict(pending_buy_reserve_twd='1000',account_pending_buy_reserve_twd='1500'),'1873'),
            (dict(daily_buy_reserved_twd='19500'),'500'),
            (dict(broker_balance_twd='5000'),'0'),
            (dict(ai_committed_capital_twd='10000'),'0'),
            (dict(daily_buy_reserved_twd='20000'),'0')]
        for updates,expected in cases:
            with self.subTest(updates=updates),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);ExecutionStore(root/'data/execution/live_execution.sqlite3')
                report=account_report(**updates)
                snapshot=persist_refresh_snapshot(root,report)
                self.assertEqual(Decimal(str(snapshot['current_buy_budget_twd'])),Decimal(expected))
                analyzed=live_buy_budget(report,Decimal(report['daily_buy_reserved_twd']),Decimal('10000'),Decimal('20000'))
                self.assertEqual(analyzed['available'],Decimal(expected))
                self.assertEqual(snapshot['budget_status'],analyzed['status'])

    def test_broker_errors_and_inventory_uncertainty_still_block_new_buys(self):
        for updates in (dict(errors=['GetBalance failed'],status='ERROR'),
                        dict(inventory_checked=False),dict(pending_buy_reserve_error='unknown'),
                        dict(inventory_comparison=[dict(status='MISMATCH')])):
            with self.subTest(updates=updates):
                report=account_report(**updates)
                self.assertEqual(live_buy_budget(report,Decimal(0),Decimal(10000),Decimal(20000))['available'],0)

    def test_default_live_watch_reaches_both_researchers_after_mis_fallback(self):
        from opportunity_flow.__main__ import main
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);at=stamp(create(root/'dataset')).replace(hour=10)
            ds=load_dataset(root/'dataset');p=policy();price=ds['grouped']['1111'][-1]['close']
            ExecutionStore(root/'account/data/execution/live_execution.sqlite3')
            snapshot=persist_refresh_snapshot(root/'account',account_report(checked_at=at.isoformat()))
            quote=parse(dict(rtcode='0000',msgArray=[dict(ex='tse',c='1111',d=at.strftime('%Y%m%d'),
                t='10:01:00',z='-',trade=dict(z=str(price),t='10:00:00'))]),[('TWSE','1111')])[0][0]
            context=dict(available_cash_twd=float(snapshot['current_buy_budget_twd']),held_qty=0,
                minimum_fee_reserve_twd=20,buy_cash_buffer_rate=.01)
            runtime=FakeRuntime(p,at.isoformat(),quote,context)
            runtime.snapshot=snapshot
            stop=threading.Event();wait=stop.wait;polls=[]
            coordinator=LiveCoordinator('unused',root/'watch',p,stop,runtime=runtime)
            coordinator.bridge.step=Mock(return_value=dict(reports=[],blocked=[]))
            runner=FakeRunner()

            def tick(seconds):
                wait(.02);polls.append(seconds)
                if coordinator.bridge.step.called or len(polls)>=100:stop.set()

            with patch('opportunity_flow.__main__.datetime') as clock,\
                 patch('opportunity_flow.public_quotes.fetch',return_value=dict(quotes=[quote],errors=[],http_requests=1)),\
                 patch('opportunity_flow.company_events.current_events',return_value={}),\
                 patch('opportunity_flow.company_events.for_symbol',return_value=dict(status='NOT_CHECKED')),\
                 patch('opportunity_flow.review.CodexRunner',return_value=runner),\
                 patch.object(stop,'wait',side_effect=tick),redirect_stdout(io.StringIO()):
                clock.now.return_value=at
                main(['watch','--dataset',str(root/'dataset'),'--state',str(coordinator.state),
                      '--live','--llm','--public-quotes','--poll-seconds','1'],
                     coordinator=coordinator,external_stop=stop)
            self.assertEqual([role for packets,role in runner.calls],['researcher','challenger'])
            self.assertTrue(coordinator.bridge.step.called,'Background research must reach the decision handoff')
            handed=coordinator.bridge.step.call_args.args[0]
            candidate=next(c for c in handed['candidates'] if c['symbol']=='1111')
            self.assertEqual(candidate['state'],'RESEARCH_READY')
            self.assertGreater(len(candidate['packet']['selected_routes']),0)
            self.assertFalse(runtime.sent)
            ledger=Ledger(coordinator.state)
            try:
                self.assertEqual(ledger.db.execute("SELECT COUNT(*) FROM calls WHERE status='VALID'").fetchone()[0],2)
                self.assertGreater(ledger.db.execute('SELECT COUNT(*) FROM cycles').fetchone()[0],0)
            finally:ledger.close()


if __name__=='__main__':
    unittest.main()
