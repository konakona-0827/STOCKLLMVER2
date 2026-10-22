from datetime import datetime
import unittest

from config import TAIPEI
from market_calendar import previous_trading_day
from position_analyzer import validate
from strategy_evidence import assess


class StrategyEvidenceTests(unittest.TestCase):
    def quote(self, symbol='2330'):
        return dict(symbol=symbol, quote_status='LIVE', is_trial=False,
                    quote_timestamp='2026-10-12T11:00:00+08:00',
                    last_price=100, open=99, high=102, low=95,
                    bid=99.9, ask=100, volume=10000, asset_type='STOCK',
                    max_buy_qty=5, paper_position={'qty': 0})

    def decision(self, symbol, strategy='intraday_recovery', target=120):
        return dict(symbol=symbol, decision='BUY', confidence=.5,
                    action_ratio=0, suggested_qty=1, reference_price=100,
                    suggested_price=100, strategy_id=strategy,
                    target_price=target, invalidation_price=95, holding_days=5,
                    reason='當日區間可評估；若未改善則退出',
                    data_quality='LIVE', warnings=['預算限制'])

    def test_missing_daily_history_only_disables_history_dependent_setups(self):
        result = assess(self.quote(), [])
        self.assertEqual(result['eligible_strategies'], ['intraday_recovery'])
        self.assertFalse(result['strategies']['multi_day_trend']['eligible'])

    def test_previous_trading_session_and_intraday_samples_enable_other_setups(self):
        history = [
            {'exchange_time': '2026-10-08T13:00:00+08:00', 'price': 98},
            {'exchange_time': '2026-10-12T09:30:00+08:00', 'price': 97},
            {'exchange_time': '2026-10-12T10:30:00+08:00', 'price': 99},
        ]
        result = assess(self.quote(), history)
        self.assertEqual(result['eligible_strategies'],
                         ['intraday_recovery', 'short_term_reversal', 'breakout'])
        self.assertEqual(result['strategies']['breakout']['observed']['prior_sample_high'], 99)
        self.assertFalse(result['strategies']['multi_day_trend']['eligible'])

    def test_old_snapshot_does_not_count_as_short_term_reversal(self):
        old = [{'exchange_time': '2026-10-01T13:00:00+08:00', 'price': 98}]
        self.assertNotIn('short_term_reversal', assess(self.quote(), old)['eligible_strategies'])

    def test_verified_consecutive_daily_bars_enable_only_trend_setup(self):
        q = self.quote()
        cursor = datetime(2026, 10, 12, tzinfo=TAIPEI).date()
        bars = []
        for _ in range(5):
            cursor = previous_trading_day(cursor)
            bars.append(dict(date=cursor.isoformat(), source='TWSE_DAILY',
                             complete=True, open=96, high=103, low=94,
                             close=100, volume=10000))
        q['daily_bars'] = bars
        self.assertIn('multi_day_trend', assess(q)['eligible_strategies'])
        q['daily_bars'] = [{**bar, 'complete': False} for bar in bars]
        self.assertNotIn('multi_day_trend', assess(q)['eligible_strategies'])

    def test_one_unsupported_strategy_does_not_block_another_buy(self):
        a, b = self.quote('2330'), self.quote('2454')
        for candidate in (a, b):
            candidate['eligible_strategies'] = assess(candidate)['eligible_strategies']
        result = validate(dict(market_view='測試', decisions=[
            self.decision('2330', strategy='multi_day_trend'),
            self.decision('2454'),
        ]), [a, b], available_cash_cents=50000)
        self.assertEqual([d['decision'] for d in result['decisions']], ['WAIT', 'BUY'])
        self.assertIn('BUY_STRATEGY_EVIDENCE_UNAVAILABLE', result['decisions'][0]['warnings'])

    def test_insufficient_net_upside_rejects_only_that_buy(self):
        candidate = self.quote()
        candidate['eligible_strategies'] = assess(candidate)['eligible_strategies']
        result = validate(dict(market_view='測試', decisions=[
            self.decision('2330', target=100.5),
        ]), [candidate], available_cash_cents=50000)
        self.assertEqual(result['decisions'][0]['decision'], 'WAIT')
        self.assertIn('BUY_NET_UPSIDE_OR_RISK_REWARD_INSUFFICIENT',
                      result['decisions'][0]['warnings'])

    def test_budget_uses_higher_proposed_limit_price(self):
        candidate = self.quote()
        candidate['eligible_strategies'] = assess(candidate)['eligible_strategies']
        order = self.decision('2330', target=400)
        order.update(suggested_price=200, invalidation_price=190)
        with self.assertRaisesRegex(ValueError, 'AGGREGATE_BUY_COST_EXCEEDS_AVAILABLE_CASH'):
            validate(dict(market_view='測試', decisions=[order]),
                     [candidate], available_cash_cents=15000)

    def test_non_action_null_quantity_is_accepted_but_positive_quantity_is_rejected(self):
        candidate = self.quote()
        for decision, held in [('WAIT', 0), ('HOLD', 2)]:
            candidate['paper_position']['qty'] = held
            row = self.decision('2330')
            row.update(decision=decision, suggested_qty=None, suggested_price=None,
                       strategy_id='NONE', target_price=None,
                       invalidation_price=None, holding_days=None)
            result = validate(dict(market_view='測試', decisions=[row]), [candidate])
            self.assertEqual(result['decisions'][0]['suggested_qty'], 0)
            row['suggested_qty'] = 1
            with self.assertRaisesRegex(ValueError, 'NON_ACTION_QUANTITY'):
                validate(dict(market_view='測試', decisions=[row]), [candidate])

    def test_non_action_reference_uses_verified_quote(self):
        candidate = self.quote()
        row = self.decision('2330')
        row.update(decision='WAIT', suggested_qty=None, suggested_price=None,
                   reference_price=101, strategy_id='NONE', target_price=None,
                   invalidation_price=None, holding_days=None)
        result = validate(dict(market_view='測試', decisions=[row]), [candidate])
        self.assertEqual(result['decisions'][0]['reference_price'], 100)
        self.assertTrue(any('參考價' in warning for warning in result['decisions'][0]['warnings']))
        row.update(decision='BUY', suggested_qty=1, suggested_price=100,
                   reference_price=101)
        with self.assertRaisesRegex(ValueError, 'INCORRECT_REFERENCE'):
            validate(dict(market_view='測試', decisions=[row]), [candidate])

    def test_missing_candidate_is_audited_wait_without_losing_valid_buy(self):
        buy, omitted = self.quote('2330'), self.quote('2317')
        buy['eligible_strategies'] = assess(buy)['eligible_strategies']
        omitted['paper_position']['qty'] = 2
        result = validate(dict(market_view='測試', decisions=[self.decision('2330')]),
                          [buy, omitted], available_cash_cents=50000)
        self.assertEqual([d['decision'] for d in result['decisions']], ['BUY', 'WAIT'])
        missing = result['decisions'][1]
        self.assertEqual(missing['symbol'], '2317')
        self.assertEqual(missing['suggested_qty'], 0)
        self.assertEqual(missing['reference_price'], omitted['last_price'])
        self.assertIn('AI_MISSING_SYMBOL_WAIT', missing['warnings'][0])
        self.assertIn('2317', result['market_view'])
        with self.assertRaisesRegex(ValueError, 'UNKNOWN_OR_DUPLICATE_SYMBOL'):
            validate(dict(market_view='測試', decisions=[
                self.decision('2330'), self.decision('2330')]), [buy, omitted])


if __name__ == '__main__':
    unittest.main()
