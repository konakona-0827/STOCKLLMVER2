from decimal import Decimal
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from execution.account_guard import BuyingPower
from execution.execution_store import ExecutionStore
from execution.health_monitor import (current_buy_block_reasons, persist_refresh_snapshot,
                                      prior_execution_error_resolved, run_health_check)
import paper_portfolio
import position_store


class ExecutionErrorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'config').mkdir()
        (self.root/'config/execution_live.json').write_text(json.dumps(dict(enabled=True,
            min_available_to_buy_twd=0,max_daily_buy_twd=30000)),encoding='utf-8')
        paper_portfolio.set_capital_limit(self.root/'data/analysis/market_history.sqlite3',10000)
        position_store.load(self.root/'data/analysis/market_history.sqlite3')
        self.store=ExecutionStore(self.root/'data/execution/live_execution.sqlite3')
        self.previous=dict(status='ERROR',run_id='R1',execution_origin='ANALYSIS_SERVICE',execution_attempted=True)

    def health(self, previous, inventory_error=False):
        (self.root/'data/execution/latest_live_execution.json').write_text(json.dumps(previous),encoding='utf-8')
        power=BuyingPower(True,True,Decimal('30000'),Decimal('30000'),Decimal('30000'),'test')
        session=SimpleNamespace(ensure_ready=lambda:SimpleNamespace(action='REUSE'))
        with patch('execution.health_monitor.query_buying_power',return_value=power), \
             patch('execution.health_monitor.query_inventory',**(
                 dict(side_effect=RuntimeError('CURRENT_INVENTORY_FAILURE')) if inventory_error else dict(return_value=[]))), \
             patch('execution.health_monitor.query_settlement_dues',return_value=[]):
            return run_health_check(self.root,session=session)

    def test_definite_pre_dispatch_failure_recovers_using_current_health(self):
        report=self.health(dict(self.previous,execution_attempted=False))
        self.assertEqual(report['status'],'OK')
        self.assertTrue(report['prior_execution_error_resolved'])
        snapshot=persist_refresh_snapshot(self.root,report)
        self.assertEqual(snapshot['budget_status'],'READY')
        self.assertTrue(snapshot['prior_execution_error_resolved'])
        self.assertNotIn('PRIOR_EXECUTION_ERROR',current_buy_block_reasons(snapshot))

    def test_own_cycle_without_a_durable_send_marker_can_be_researched_again(self):
        report=self.health(self.previous)
        self.assertTrue(report['prior_execution_error_resolved'])
        self.assertNotIn('PRIOR_EXECUTION_ERROR',current_buy_block_reasons(report))

    def test_legacy_error_without_dispatch_evidence_remains_blocked(self):
        report=self.health(dict(status='ERROR'))
        self.assertEqual(report['status'],'WARN')
        self.assertIn('PRIOR_EXECUTION_ERROR',current_buy_block_reasons(report))

    def test_uncertain_send_requires_exact_current_broker_reconciliation(self):
        self.store.claim('D1','R1','1101','BUY')
        self.store.mark_send_pending('D1',{})
        self.assertFalse(prior_execution_error_resolved(self.root,{},self.previous))
        for status in ('FILL_UNVERIFIED','FILL_MISMATCH'):
            report=dict(broker_order_reconciliation=[dict(decision_id='D1',status=status)])
            self.assertFalse(prior_execution_error_resolved(self.root,report,self.previous))
        verified=dict(broker_order_reconciliation=[dict(decision_id='D1',status='NO_FILL_YET_VERIFIED')])
        self.assertTrue(prior_execution_error_resolved(self.root,verified,self.previous))
        # Resolving the global failure never releases a pending symbol.
        self.assertIn('1101',self.store.pending_reconciliation_symbols())

    def test_current_broker_failure_stays_blocked_after_a_known_unsent_error(self):
        report=self.health(dict(self.previous,execution_attempted=False),inventory_error=True)
        self.assertEqual(report['status'],'ERROR')
        self.assertIn('BROKER_CHECK_ERROR',current_buy_block_reasons(report))
        self.assertIn('INVENTORY_UNVERIFIED',current_buy_block_reasons(report))
