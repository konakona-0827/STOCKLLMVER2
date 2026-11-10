from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from execution.auto_advice_executor import LiveExecutionConfig
from opportunity_engine import policy, refresh, select
from opportunity_search_cycle import run, merge_responses
from entry_structure import resistance_levels, stop_evidence
from test_opportunity_search import candidate
from test_market_opportunity_repair import declining
from test_market_discovery import fixtures, answer

ROOT = Path(__file__).resolve().parents[1]


def wait_batch(batch, index):
    return dict(market_view=f'round {index}', decisions=[dict(symbol=c['symbol'], decision='WAIT',
        reason='具體反證') for c in batch]), dict(validation='VALID', api={}, tool_calls=[])


class SearchCycleTests(unittest.TestCase):
    def setUp(self):
        self.p = policy()
        self.account = dict(capital_limit_twd=10000, available_cash_cents=1000000,
                            available_cash_twd=10000, budget_status='READY', positions=[])
        self.rules = LiveExecutionConfig(minimum_commission_twd=Decimal(1))

    def test_negative_batch_continues_without_reasking_reviewed_symbols(self):
        pool = [candidate(str(i)) for i in range(7)]
        initial, _ = select(pool, self.account, self.rules, self.p)
        analyze = Mock(side_effect=wait_batch)
        decision, response, handoff = run(pool, initial, self.account, self.rules, self.p, analyze)
        self.assertEqual(analyze.call_count, 3)
        self.assertEqual(len({d['symbol'] for d in decision['decisions']}), 7)
        self.assertEqual(len(handoff), 7)
        self.assertEqual(response['candidate_search']['stop_reason'], 'OBSERVED_POOL_EXHAUSTED')
        self.assertTrue(all(d['decision'] == 'WAIT' for d in decision['decisions']))

    def test_pretrade_veto_allows_other_symbols_but_approved_intent_stops_cash_reuse(self):
        pool = [candidate(str(i)) for i in range(8)]
        initial, _ = select(pool, self.account, self.rules, self.p)
        def analyze(batch, index):
            decision, response = wait_batch(batch, index)
            if index == 0:
                decision['decisions'][0]['reason'] = 'PRETRADE_REVIEW_VETO'
            else:
                decision['decisions'][0]['decision'] = 'BUY'
            return decision, response
        decision, response, handoff = run(pool, initial, self.account, self.rules, self.p, analyze)
        self.assertEqual(len(handoff), 6)
        self.assertEqual(sum(d['decision'] == 'BUY' for d in decision['decisions']), 1)
        self.assertEqual(response['candidate_search']['stop_reason'], 'REVIEWED_TRADE_INTENT_PRODUCED')

    def test_watch_lane_is_not_starved_by_three_plans(self):
        pool = [candidate(str(i)) for i in range(3)] + [declining('WATCH1'), declining('WATCH2')]
        selected, audit = select(pool, self.account, self.rules, self.p)
        self.assertEqual(len(selected), 5)
        self.assertEqual(set(audit['watch_research_symbols']), {'WATCH1', 'WATCH2'})
        self.assertTrue(all(not c['trade_setups'] for c in selected if c['research_only']))

    def test_expansion_tracks_untradable_names_and_stops_on_duplicate_output(self):
        calls = []
        def expand(known, decisions):
            calls.append(set(known))
            if len(calls) == 1:
                return dict(status='COMPLETE', observed_symbols=['NO_ASK'], candidates=[])
            if len(calls) == 2:
                return dict(status='COMPLETE', observed_symbols=['NEXT'], candidates=[candidate('NEXT')])
            return dict(status='COMPLETE', observed_symbols=['NEXT'], candidates=[candidate('NEXT')])
        decision, response, handoff = run([], [], self.account, self.rules, self.p, wait_batch, expand=expand)
        self.assertIn('NO_ASK', calls[1])
        self.assertIn('NEXT', calls[2])
        self.assertEqual([c['symbol'] for c in handoff], ['NEXT'])
        self.assertEqual(len(decision['decisions']), 1)

    def test_read_only_stop_error_and_budget_cannot_trigger_replacement_research(self):
        for mode in ['read_only', 'stopped', 'budget', 'model_error']:
            with self.subTest(mode=mode):
                pool = [candidate(str(i)) for i in range(5)]
                initial, _ = select(pool, self.account, self.rules, self.p)
                account = deepcopy(self.account)
                if mode == 'budget': account['budget_status'] = 'BLOCKED'
                def analyze(batch, index):
                    decision, response = wait_batch(batch, index)
                    if mode == 'model_error': response['validation'] = 'ERROR'
                    return decision, response
                analyze = Mock(side_effect=analyze)
                expand = Mock(side_effect=AssertionError('must not scout'))
                _, response, _ = run(pool, initial, account, self.rules, self.p, analyze,
                    expand=expand, stopped=lambda: mode == 'stopped', read_only=mode == 'read_only')
                expand.assert_not_called()
                self.assertEqual(analyze.call_count, 0 if mode in ('stopped', 'budget') else 1)

    def test_failed_expansion_is_not_reported_as_no_market_opportunity(self):
        _, response, _ = run([], [], self.account, self.rules, self.p, wait_batch,
            expand=Mock(side_effect=OSError('data unavailable')))
        self.assertEqual(response['candidate_search']['stop_reason'], 'ALTERNATIVE_SEARCH_FAILED')
        self.assertEqual(response['candidate_search']['expansions'][0]['error'], 'data unavailable')

    def test_usage_merge_counts_unique_responses_once_and_keeps_errors(self):
        row = dict(response_id='unique', input_tokens=100, output_tokens=10, requests=1)
        responses = [dict(validation='VALID', api=dict(usage_requests=[row], usage=row)),
                     dict(validation='ERROR', api=dict(usage_requests=[row], usage=row,
                          error='MODEL_FAILED', usage_accounting_complete=False))]
        result = merge_responses(responses)
        self.assertEqual(result['api']['usage']['input_tokens'], 100)
        self.assertFalse(result['api']['usage_accounting_complete'])
        self.assertEqual(result['validation'], 'ERROR')
        self.assertEqual(result['api']['error'], 'MODEL_FAILED')

    def test_each_vetoed_pretrade_review_is_included_in_aggregate_usage(self):
        responses = [dict(validation='VALID', api={}, pretrade_review=dict(status='VALID',
            reviews=[dict(symbol=str(i), approved=False)],
            api=dict(usage=dict(input_tokens=100, output_tokens=10, requests=1)))) for i in range(2)]
        result = merge_responses(responses)
        self.assertEqual(result['pretrade_review']['api']['usage']['input_tokens'], 200)
        self.assertEqual(len(result['pretrade_review']['reviews']), 2)

    def test_production_agent_wrapper_uses_new_batch_and_preserves_previous_reasons(self):
        import agent_decision
        pool = [candidate(str(i)) for i in range(5)]
        initial, _ = select(pool, self.account, self.rules, self.p)
        seen = []
        def analyze(service, directory, manifest, request, batch, *args):
            seen.append(json.loads(request['input']))
            return wait_batch(batch, len(seen))
        with tempfile.TemporaryDirectory() as tmp, patch('agent_decision._analyze_batch', side_effect=analyze):
            service = Mock();service.stop.is_set.return_value = False
            decision, response = agent_decision.analyze_existing(service, Path(tmp), dict(run_id='run'),
                dict(input='{}'), initial, self.account, self.rules, True, {}, opportunity_pool=pool)
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(seen[1]['previous_candidate_rejections']), 3)
        self.assertEqual(len(initial), 5)
        self.assertEqual(len(decision['decisions']), 5)

    def test_replacement_scout_bypasses_cache_only_for_distinct_alternatives_and_receives_feedback(self):
        from config import TAIPEI
        from datetime import datetime, timedelta
        from market_discovery import discover, record_entry_feedback
        daily, companies = fixtures()
        at = datetime(2026, 11, 4, 10, 5, tzinfo=TAIPEI)
        fetch = Mock(side_effect=lambda url: daily if url.endswith('STOCK_DAY_ALL') else companies)
        runner = Mock(side_effect=answer)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);directory = root/'run';directory.mkdir()
            first = discover(root, directory, self.account, self.p, at=at, runner=runner, fetcher=fetch)
            c = declining(first['symbols'][0]);refresh(c, self.account, self.rules, self.p)
            record_entry_feedback(root, [c], at, [dict(symbol=c['symbol'], decision='WAIT', reason='支撐風險尚未解除')])
            second = discover(root, directory/'replacement', self.account, self.p,
                at=at+timedelta(minutes=1), runner=runner, fetcher=fetch,
                excluded=set(first['symbols']), reconsider=True)
            self.assertTrue(second['model_called'])
            self.assertTrue(set(first['symbols']).isdisjoint(second['symbols']))
            packet = runner.call_args.args[0]
            self.assertEqual(packet['recent_entry_feedback'][0]['final_decision'], 'WAIT')
            self.assertEqual(runner.call_count, 2)


class SharedStructureTests(unittest.TestCase):
    def test_broken_support_is_not_restored_by_an_unrelated_later_close(self):
        bars = [dict(date='2026-11-02', low=100, close=102),
                dict(date='2026-11-03', low=98, close=99),
                dict(date='2026-11-04', low=101, close=103)]
        evidence = stop_evidence(bars, Decimal(100), 102, '2026-11-05')
        self.assertEqual(evidence['formed_on'], '2026-11-02')
        self.assertEqual(evidence['broken_on'], '2026-11-03')
        # A new completed session actually establishing the same low is a new
        # reference; the older break cannot invalidate it retrospectively.
        bars.append(dict(date='2026-11-05', low=100, close=104))
        evidence = stop_evidence(bars, Decimal(100), 103, '2026-11-06')
        self.assertEqual(evidence['formed_on'], '2026-11-05')
        self.assertIsNone(evidence['broken_on'])

    def test_support_break_must_follow_formation(self):
        bars = [dict(date=str(i), low=low, high=low+8, close=close)
                for i, (low, close) in enumerate([(90, 91), (100, 105), (99, 104), (101, 106), (102, 105)])]
        levels = resistance_levels(bars, [], recovery=False)
        self.assertFalse(any(r['price'] == 99 for r in levels))
        bars[-1]['close'] = 98
        levels = resistance_levels(bars, [], recovery=False)
        self.assertTrue(any(r['price'] == 99 and r['broken_on'] == '4' for r in levels))

    def test_archived_recovery_no_longer_skips_yesterday_high(self):
        base = ROOT/'reports/tw_trade_20261110_until_112203_Taipei'
        snapshot = json.loads((base/'runs/20261110T091100-gui-ccd92776/candidates.json').read_text('utf-8'))
        account = json.loads((base/'runs/20261110T091100-gui-ccd92776/run.json').read_text('utf-8'))['account_snapshot']
        rules = LiveExecutionConfig(minimum_commission_twd=Decimal(1), max_order_twd=Decimal(100000))
        for symbol in ['2376', '2377']:
            c = deepcopy(next(c for c in snapshot['before_selection'] if c['symbol'] == symbol))
            refresh(c, account, rules, policy())
            self.assertFalse(c['trade_setups'], c['symbol'])
            high = c['daily_bars'][-1]['high']
            self.assertTrue(any(r['price'] == high and r['kind'] == 'OBSERVED_RECENT_SESSION_HIGH'
                                for r in c['entry_structure']['resistance_levels']))

    def test_known_positive_trend_plan_is_not_deleted_by_daily_highs(self):
        benchmark = json.loads((ROOT/'reports/october_expanded_20261109/competitive/benchmark.json').read_text('utf-8'))
        c = deepcopy(next(e['candidate'] for e in benchmark['events'] if e['event_id'] == '2026-10-19|2377'))
        refresh(c, benchmark['account'], LiveExecutionConfig(minimum_commission_twd=Decimal(1)), policy())
        self.assertTrue(any(s['entry'] == 159 and s['target'] == 167.5 and s['invalidation'] == 156
                            and s['holding_days'] == 10 for s in c['trade_setups']))
        self.assertTrue(all(s['entry_evidence']['requires_today_completed_bar'] is False for s in c['trade_setups']))

    def test_shared_family_matches_the_actual_early_breakout_path(self):
        c = candidate()
        c['daily_bars'][-1].update(open=121, high=124, low=120, close=121, volume=1000)
        from pretrade_policy import price_levels
        c['price_levels'] = price_levels(c)
        account = dict(capital_limit_twd=10000, available_cash_cents=1000000, budget_status='READY')
        refresh(c, account, LiveExecutionConfig(minimum_commission_twd=Decimal(1)), policy())
        self.assertEqual(c['entry_structure']['setup_family'], 'FIRST_DAY_BREAKOUT')
        self.assertTrue(all(s['setup_family'] == c['entry_structure']['setup_family'] for s in c['trade_setups']))
