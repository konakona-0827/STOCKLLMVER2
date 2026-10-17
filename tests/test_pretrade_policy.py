import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from market_calendar import previous_trading_day
from position_analyzer import review_trades, validate
from pretrade_policy import buy_block, price_levels
from trade_plan_context import live_plans, remaining_plans
from execution.auto_advice_executor import check_refreshed_buy
from execution.risk_guard import RiskRejected
import paper_portfolio


class PretradePolicyTests(unittest.TestCase):
    def candidate(self):
        return dict(symbol='2330', name='測試', quote_status='LIVE', is_trial=False,
                    quote_timestamp='2026-10-12T11:00:00+08:00', last_price=100,
                    ask=100, bid=99.9, open=99, high=120, low=95,
                    asset_type='STOCK', eligible_strategies=['intraday_recovery'],
                    max_buy_qty=100, paper_position={'qty': 0, 'cost_cents': 0})

    def decision(self, qty=10):
        return dict(symbol='2330', decision='BUY', confidence=.6, action_ratio=0,
                    suggested_qty=qty, reference_price=100, suggested_price=100,
                    strategy_id='intraday_recovery', target_price=120,
                    invalidation_price=95, holding_days=10,
                    reason='盤中修復，失效價 95', data_quality='LIVE', warnings=['預算'])

    def test_levels_require_verified_consecutive_sessions(self):
        candidate = self.candidate()
        cursor = date(2026, 10, 12)
        bars = []
        for index in range(5):
            cursor = previous_trading_day(cursor)
            bars.append(dict(date=cursor.isoformat(), complete=True, source='TWSE_DAILY',
                             high=110+index, low=90-index, close=100))
        candidate['daily_bars'] = bars
        levels = {row['id']: row['price'] for row in price_levels(candidate)}
        self.assertEqual(levels['previous_close'], 100)
        self.assertEqual(levels['high_5d'], 114)
        self.assertEqual(levels['low_5d'], 86)
        self.assertNotIn('high_10d', levels)
        candidate['daily_bars'][2]['complete'] = False
        levels = {row['id'] for row in price_levels(candidate)}
        self.assertNotIn('high_5d', levels)

    def test_unsupported_price_and_risk_budget_are_local_waits(self):
        candidate = self.candidate()
        candidate['price_levels'] = price_levels(candidate)
        decision = self.decision()
        decision['target_price'] = 130
        result = validate(dict(market_view='測試', decisions=[decision]), [candidate],
                          1000000, capital_twd=100000)
        self.assertEqual(result['decisions'][0]['decision'], 'WAIT')
        self.assertIn('BUY_UNSUPPORTED_PRICE_LEVEL', result['decisions'][0]['warnings'])

        decision = self.decision(qty=100)
        result = validate(dict(market_view='測試', decisions=[decision]), [candidate],
                          1000000, capital_twd=10000)
        self.assertEqual(result['decisions'][0]['decision'], 'BUY')
        self.assertLess(decision['suggested_qty'], 100)
        self.assertTrue(any('縮減' in warning for warning in decision['warnings']))

    def test_cooldown_and_losing_add_block(self):
        candidate = self.candidate()
        candidate['ai_trade_dates'] = {'records': [
            {'side': 'SELL', 'date': '2026-10-08', 'quantity': 2}]}
        self.assertEqual(buy_block(candidate), 'BUY_REENTRY_COOLDOWN')
        candidate['ai_trade_dates'] = {'records': []}
        candidate['paper_position'] = {'qty': 10, 'cost_cents': 110000}
        self.assertEqual(buy_block(candidate), 'BUY_LOSING_POSITION_ADD_BLOCKED')

    def test_reviewer_veto_and_unavailable_are_wait(self):
        candidate = self.candidate()
        trade = self.decision()
        result = dict(market_view='測試', decisions=[trade])
        request = {'model': 'test', 'input': json.dumps({'analysis_set': [candidate]})}
        def deny(_):
            return json.dumps({'reviews': [{'symbol': '2330', 'approved': False,
                                            'reason': '反面證據未處理'}]}), {}
        review = review_trades(result, request, deny)
        self.assertEqual(review['status'], 'VALID')
        self.assertEqual(trade['decision'], 'WAIT')
        self.assertIn('PRETRADE_REVIEW_VETO', trade['warnings'])
        trade = self.decision()
        def broken(_):
            raise TimeoutError('timeout')
        review = review_trades(dict(decisions=[trade]), request, broken)
        self.assertEqual(review['status'], 'ERROR')
        self.assertEqual(trade['decision'], 'WAIT')

    def test_only_confirmed_remaining_lots_carry_plan(self):
        events = [
            dict(side='BUY', at='2026-10-08T10:00:00+08:00', quantity=5,
                 plan={'strategy_id': 'breakout'}, date_basis='TEST'),
            dict(side='BUY', at='2026-10-09T10:00:00+08:00', quantity=3,
                 plan=None, date_basis='TEST'),
            dict(side='SELL', at='2026-10-12T10:00:00+08:00', quantity=4,
                 date_basis='TEST'),
        ]
        plans = remaining_plans(events, 4)
        self.assertFalse(plans['available'])
        self.assertEqual([row['quantity'] for row in plans['lots']], [1, 3])

    def test_live_confirmed_fill_plan_survives_reconstruction(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'execution.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE execution_attempts '
                           '(symbol TEXT, side TEXT, started_at TEXT, detail_json TEXT)')
                db.execute('INSERT INTO execution_attempts VALUES(?,?,?,?)',
                           ('2330', 'BUY', '2026-10-08T02:00:00+00:00',
                            json.dumps({'filled_quantity': 3, 'buy_plan':
                                        {'target_price': 120, 'invalidation_price': 95}})))
                db.commit()
            context = live_plans(path, {'2330': 3})['2330']
            self.assertTrue(context['available'])
            self.assertEqual(context['lots'][0]['entry_date'], '2026-10-08')

    def test_refreshed_ask_rechecks_reviewed_economics(self):
        recommendation = dict(asset_type='STOCK', buy_plan={
            'target_price': 120, 'invalidation_price': 95})
        plan = SimpleNamespace(symbol='2330', quantity=10, limit_price=100)
        quote = SimpleNamespace(ask=100, bid=99.9)
        check_refreshed_buy(quote, plan, recommendation, 100000, 20)
        quote.ask = 110
        quote.bid = 109.9
        plan.limit_price = 110
        with self.assertRaises(RiskRejected):
            check_refreshed_buy(quote, plan, recommendation, 100000, 20)

    def test_paper_fill_saves_plan_for_next_analysis(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'paper.sqlite3'
            paper_portfolio.set_capital_limit(path, 100000)
            candidate = self.candidate()
            decision = self.decision(qty=2)
            paper_portfolio.apply_decisions(path, 'run-1', [decision], [candidate])
            context = paper_portfolio.trade_context(path, {'2330': 2})['2330']
            self.assertTrue(context['buy_plans']['available'])
            self.assertEqual(context['buy_plans']['lots'][0]['plan']['target_price'], 120)
            paper_portfolio.apply_decisions(path, 'run-2',
                [dict(symbol='2330', decision='SELL', suggested_qty=2, reason='目標已達')],
                [candidate])
            context = paper_portfolio.trade_context(path, {'2330': 0})['2330']
            self.assertEqual(context['ai_trade_dates']['records'][-1]['side'], 'SELL')
            self.assertEqual(context['buy_plans']['lots'], [])


if __name__ == '__main__':
    unittest.main()
