from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import daily_participation as daily
from config import now
from operations.dashboard_supervisor import watchdog_reason
import position_analyzer


class DailyParticipationTests(unittest.TestCase):
    def setUp(self):
        # Feature tests must not depend on the live account's enable switch.
        enabled = patch.object(daily, 'policy', return_value=dict(enabled=True, max_notional_twd=300))
        enabled.start()
        self.addCleanup(enabled.stop)

    def test_disabled_policy_blocks_probes_even_with_old_enabled_request(self):
        context, decision = self.fixture()
        before = deepcopy(decision)
        with patch.object(daily, 'policy', return_value=dict(enabled=False, max_notional_twd=300)), \
                patch('scheduler.is_twse_oddlot_market_window', return_value=True):
            self.assertFalse(daily.probe_allowed('BUY', 1, 100))
            self.assertFalse(daily.probe_allowed('SELL', 1, 100))
            audit = daily.add_probes(context, decision)
            self.assertEqual(audit['proposed'], [])
            self.assertEqual(decision, before)

    def test_historical_failure_current_claim_disables_its_own_probe_exemption(self):
        from execution.execution_store import ExecutionStore
        from execution.auto_advice_executor import check_refreshed_buy
        from execution.risk_guard import RiskRejected
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'execution.sqlite3'
            store = ExecutionStore(path)
            read_progress = daily.progress
            quote = SimpleNamespace(ask=Decimal('62.8'), bid=Decimal('62.7'))
            plan = SimpleNamespace(symbol='2609', quantity=1, limit_price=Decimal('62.8'))
            rec = dict(asset_type='STOCK', warnings=[daily.MARKER],
                       buy_plan=dict(target_price=63, invalidation_price=62))
            with patch.object(daily, 'progress', side_effect=lambda: read_progress(path)):
                self.assertTrue(daily.probe_allowed('BUY', 1, 62.8))
                check_refreshed_buy(quote, plan, rec, 10000, 20)
                store.claim('historical:2609', 'historical', '2609', 'BUY')
                self.assertEqual(read_progress(path)['pending']['BUY'], 1)
                self.assertFalse(daily.probe_allowed('BUY', 1, 62.8))
                with self.assertRaises(RiskRejected):
                    check_refreshed_buy(quote, plan, rec, 10000, 20)

    def test_fill_pending_and_local_day_are_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'execution.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE execution_attempts(side,started_at,status,detail_json)')
                db.executemany('INSERT INTO execution_attempts VALUES(?,?,?,?)', [
                    ('BUY', '2026-10-28T16:30:00+00:00', 'FILLED', '{"filled_quantity":1}'),
                    ('SELL', '2026-10-29T10:00:00+08:00', 'ACKNOWLEDGED', '{}'),
                    ('SELL', '2026-10-28T10:00:00+08:00', 'FILLED', '{"filled_quantity":9}')])
                db.commit()
            state = daily.progress(path, datetime.fromisoformat('2026-10-29T12:00:00+08:00'))
            self.assertEqual(state['filled'], {'BUY': 1, 'SELL': 0})
            self.assertEqual(state['pending'], {'BUY': 0, 'SELL': 1})
            self.assertEqual(state['needed'], [])
            self.assertEqual(daily.progress(Path(folder)/'missing')['status'], 'UNAVAILABLE')

    def fixture(self):
        c = dict(symbol='1101', name='台泥', last_price=100, bid=99.9, ask=100,
                 quote_status='LIVE', quote_timestamp=now().isoformat(), is_trial=False,
                 asset_type='STOCK', max_buy_qty=20, max_sell_qty=49,
                 eligible_strategies=['multi_day_trend'],
                 price_levels=[dict(price=101), dict(price=95)],
                 paper_position=dict(qty=49, cost_cents=490000),
                 ai_trade_dates=dict(records=[]))
        d = dict(symbol='1101', decision='HOLD', confidence=0.5, suggested_qty=0,
                 suggested_price=None, action_ratio=0, strategy_id='NONE',
                 target_price=None, invalidation_price=None, holding_days=None,
                 reference_price=100, reason='持有', data_quality='LIVE', warnings=[])
        context = SimpleNamespace(candidates={'1101': c}, read_only=False, live_enabled=True,
                    stopped=lambda: False, support=None,
                    initial_input=dict(daily_participation=dict(policy=dict(enabled=True))),
                    get_market_data=lambda _: deepcopy(c))
        return context, dict(market_view='測試', decisions=[d])

    def test_sell_probe_is_one_share_and_cannot_sell_today_only_position(self):
        context, decision = self.fixture()
        state = dict(status='READY', needed=['SELL'], filled={'BUY':1,'SELL':0}, pending={'BUY':0,'SELL':0})
        with patch.object(daily, 'progress', return_value=state), \
                patch('scheduler.is_twse_oddlot_market_window', return_value=True):
            daily.add_probes(context, decision)
            position_analyzer.validate(decision, list(context.candidates.values()), 100000)
        self.assertEqual(decision['decisions'][0]['decision'], 'SELL')
        self.assertEqual(decision['decisions'][0]['suggested_qty'], 1)
        context, decision = self.fixture()
        context.candidates['1101']['max_sell_qty'] = 0
        with patch.object(daily, 'progress', return_value=state), \
                patch('scheduler.is_twse_oddlot_market_window', return_value=True):
            audit = daily.add_probes(context, decision)
        self.assertEqual(decision['decisions'][0]['decision'], 'HOLD')
        self.assertIn('SELL:NO_ELIGIBLE_PROBE', audit['blocked'])

    def test_probe_relaxes_profit_only_and_executor_rechecks_same_cap(self):
        from execution.auto_advice_executor import check_refreshed_buy
        context, decision = self.fixture()
        c = context.candidates['1101']
        c['paper_position'] = dict(qty=0)
        state = dict(status='READY', needed=['BUY'])
        with patch.object(daily, 'progress', return_value=state), \
                patch('scheduler.is_twse_oddlot_market_window', return_value=True):
            daily.add_probes(context, decision)
            normal = deepcopy(decision)
            normal['decisions'][0]['warnings'] = []
            position_analyzer.validate(normal, [c], 100000, 20, capital_twd=10000)
            self.assertEqual(normal['decisions'][0]['decision'], 'WAIT')
            position_analyzer.validate(decision, [c], 100000, 20, capital_twd=10000)
            self.assertEqual(decision['decisions'][0]['decision'], 'BUY')
            d = decision['decisions'][0]
            recommendation = dict(asset_type='STOCK', buy_plan=d, warnings=d['warnings'])
            quote = SimpleNamespace(ask=Decimal('100'), bid=Decimal('99.9'))
            plan = SimpleNamespace(symbol='1101', quantity=1, limit_price=Decimal('100'))
            check_refreshed_buy(quote, plan, recommendation, 10000, 20)
            with self.assertRaises(Exception):
                check_refreshed_buy(quote, plan, recommendation, 1000, 20)
            self.assertFalse(daily.probe_allowed('BUY', 2, 100, state=state))
            self.assertFalse(daily.probe_allowed('BUY', 1, 301, state=state))

    def test_watchdog_detects_frozen_ui_and_frozen_analysis(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'lifecycle.jsonl'
            at = datetime.now(timezone.utc)
            launched = at - timedelta(seconds=1000)
            self.assertIn('HEARTBEAT_STALE', watchdog_reason(path, 12, launched, at=at))
            path.write_text(json.dumps(dict(pid=12, time_utc=at.isoformat(),
                detail=dict(analysis_running=True, analysis_age_seconds=901)))+'\n', encoding='utf-8')
            self.assertIn('ANALYSIS_STALLED', watchdog_reason(path, 12, launched, at=at))
            path.write_text(json.dumps(dict(pid=12, time_utc=at.isoformat(),
                detail=dict(analysis_running=False)))+'\n', encoding='utf-8')
            self.assertIsNone(watchdog_reason(path, 12, launched, at=at))

    def test_agent_wait_gets_probe_but_independent_veto_still_wins(self):
        from trading_agent import TradingTools, analyze
        from execution.auto_advice_executor import LiveExecutionConfig
        for approved in (True, False):
            with self.subTest(approved=approved), tempfile.TemporaryDirectory() as folder:
                fixture, decision = self.fixture()
                c = fixture.candidates['1101']
                c['position'] = dict(ai_managed_qty=0)
                c['paper_position'] = dict(qty=0)
                decision['decisions'][0]['decision'] = 'WAIT'
                context = TradingTools([c], LiveExecutionConfig(), 'probe-test', Path(folder)/'tools.json',
                    lambda symbol, previous: deepcopy(previous),
                    lambda: dict(available_cash_cents=100000, capital_limit_twd=10000, positions=[]),
                    lambda: [], initial_input=fixture.initial_input)
                async def runner(ctx, *args):
                    ctx.get_portfolio()
                    ctx.get_trade_history()
                    return deepcopy(decision), {}
                def reviewer(request):
                    return json.dumps(dict(reviews=[dict(symbol='1101', approved=approved, reason='review')])), {}
                with patch.object(daily, 'progress', return_value=dict(status='READY', needed=['BUY'])), \
                        patch('scheduler.is_twse_oddlot_market_window', return_value=True):
                    output, response = analyze(context, 'test', runner=runner, reviewer=reviewer)
                self.assertEqual(response['validation'], 'VALID')
                self.assertEqual(output['decisions'][0]['decision'], 'BUY' if approved else 'WAIT')
                self.assertEqual(len(response['daily_participation']['proposed']), 1)

    def test_read_only_pending_and_support_rejection_cannot_generate_probe(self):
        state = dict(status='READY', needed=['BUY'])
        for blocked_by in ('read_only', 'pending', 'support'):
            context, decision = self.fixture()
            context.candidates['1101']['paper_position'] = dict(qty=0)
            if blocked_by == 'read_only':
                context.read_only = True
            if blocked_by == 'support':
                context.support = SimpleNamespace(buy_block_reasons=lambda _: ['SUPPORT_AGENT_REJECTED'])
            selected = dict(status='READY', needed=[]) if blocked_by == 'pending' else state
            with patch.object(daily, 'progress', return_value=selected), \
                    patch('scheduler.is_twse_oddlot_market_window', return_value=True):
                audit = daily.add_probes(context, decision)
            self.assertEqual(audit['proposed'], [])
            self.assertEqual(decision['decisions'][0]['suggested_qty'], 0)


if __name__ == '__main__':
    unittest.main()
