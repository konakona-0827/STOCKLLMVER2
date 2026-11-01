from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from execution.advice_adapter import build_advice_intents
from execution.risk_guard import build_order_plan, RiskConfig, RiskRejected
from quant_research.core import Policy, Costs
from quant_research.limit_orders import LimitOrderBook, PricePlan, floor_stock_price
from quant_research.planned_cycle import cycle, willing_entry


def plan(pid='p1', symbol='2330', qty=50, side='BUY', at='2026-11-02T10:00:00+08:00', price='98.5'):
    return PricePlan(pid, symbol, side, qty, price, '103', '97', at,
                     at[:10]+'T13:30:00+08:00', 'test pullback thesis', 'A-test', 'quant-test')


def quote(at='2026-11-02T10:00:00+08:00', symbol='2330', ask=100, bid=99.9, size=100, last=100):
    return dict(symbol=symbol, quote_status='LIVE', quote_timestamp=at, bid=bid, ask=ask,
                bid_size=size, ask_size=size, last_price=last,
                lot_type='INTRADAY_ODD_LOT', book_size_unit='shares')


class PlannedOrdersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'planned.sqlite3'
        self.book = LimitOrderBook(self.path, Policy('A'))
        self.at = '2026-11-02T10:00:00+08:00'
        self.q = quote()

    def submit(self, p=None):
        p = p or plan()
        self.book.create(p)
        return self.book.submit(p.plan_id, p.created_at, {p.symbol: quote(p.created_at, p.symbol)})

    def test_ack_preserves_planned_price_and_does_not_create_position(self):
        order = self.submit()
        self.assertEqual(order['limit_price'], '98.5')
        s = self.book.snapshot()
        self.assertEqual(s['orders'][0]['status'], 'ACKNOWLEDGED')
        self.assertFalse(s['positions'])
        self.assertEqual(s['cash'], 100000)
        self.assertEqual(s['reserved'], 4945)

    def test_existing_live_guard_cannot_execute_new_pricing_policy(self):
        from execution.models import AdviceIntent, Side
        from execution.risk_guard import MarketQuote
        i = AdviceIntent('r', 'd', '1.0', '2330', Side.BUY, Decimal(0), 'test', None, (),
            'LIVE', self.at, Decimal(0), Decimal(100), Decimal('98.5'), 50, Decimal(0), 0,
            pricing_policy='PLANNED_LIMIT')
        q = MarketQuote('2330', 'LIVE', Decimal(0), Decimal(100), Decimal('99.9'), Decimal(100))
        with self.assertRaises(RiskRejected):
            build_order_plan(i, quote=q)
        self.assertEqual(build_order_plan(i, quote=q,
            config=RiskConfig(allow_planned_limits=True)).limit_price, Decimal('98.5'))
        self.assertEqual(build_order_plan(replace(i, pricing_policy='AT_QUOTE'), quote=q).limit_price, Decimal(100))

    def test_same_snapshot_and_last_price_touch_do_not_fill(self):
        self.submit()
        self.assertFalse(self.book.process('t0', self.at, {'2330': self.q}))
        q = quote('2026-11-02T10:01:00+08:00', last=98.5)
        self.assertFalse(self.book.process('t1', q['quote_timestamp'], {'2330': q}))
        self.assertFalse(self.book.snapshot()['positions'])

    def test_partial_fills_update_only_confirmed_quantity_and_charge_one_fee(self):
        self.submit()
        q = quote('2026-11-02T10:01:00+08:00', ask=98.5, bid=98.4, size=20)
        first = self.book.process('t1', q['quote_timestamp'], {'2330': q})
        self.assertEqual(first[0]['qty'], 20)
        s = self.book.snapshot()
        self.assertEqual(s['positions']['2330']['qty'], 20)
        self.assertEqual(s['orders'][0]['status'], 'PARTIALLY_FILLED')
        self.assertEqual(s['reserved'], 98.5*30)
        self.assertEqual(first, self.book.process('t1', q['quote_timestamp'], {'2330': q}))
        # New tick ID cannot reuse the same displayed depth.
        self.assertFalse(self.book.process('t2', '2026-11-02T10:01:10+08:00', {'2330': q}))
        q = quote('2026-11-02T10:02:00+08:00', ask=98.4, bid=98.3, size=100)
        second = self.book.process('t3', q['quote_timestamp'], {'2330': q})
        self.assertEqual(second[0]['qty'], 30)
        s = self.book.snapshot()
        self.assertEqual(s['positions']['2330']['qty'], 50)
        self.assertEqual(sum(f['fee'] for f in s['fills']), 20)
        self.assertEqual(s['reserved'], 0)
        self.assertTrue(all(f['price'] <= 98.5 for f in s['fills']))

    def test_no_liquidity_or_stale_quote_cannot_fill(self):
        self.submit()
        q = quote('2026-11-02T10:01:00+08:00', ask=98.5, bid=98.4, size=0)
        self.assertFalse(self.book.process('t1', q['quote_timestamp'], {'2330': q}))
        self.assertFalse(self.book.process('t2', '2026-11-02T10:04:00+08:00', {'2330': q}))
        self.assertFalse(self.book.snapshot()['positions'])

    def test_cancel_request_keeps_reservation_and_allows_racing_fill(self):
        self.submit()
        self.book.request_cancel('p1', '2026-11-02T10:01:00+08:00', 'trend invalidated')
        self.assertGreater(self.book.snapshot()['reserved'], 0)
        q = quote('2026-11-02T10:02:00+08:00', ask=98.5, bid=98.4, size=10)
        self.book.process('t1', q['quote_timestamp'], {'2330': q})
        self.assertEqual(self.book.snapshot()['positions']['2330']['qty'], 10)
        self.assertEqual(self.book.snapshot()['orders'][0]['status'], 'CANCEL_PENDING')
        self.book.confirm_cancel('p1', '2026-11-02T10:03:00+08:00')
        self.assertEqual(self.book.snapshot()['reserved'], 0)
        q = quote('2026-11-02T10:04:00+08:00', ask=98.5, bid=98.4)
        self.assertFalse(self.book.process('t2', q['quote_timestamp'], {'2330': q}))
        self.assertEqual(self.book.snapshot()['positions']['2330']['qty'], 10)

    def test_invalidation_does_not_silently_release_money(self):
        self.submit()
        self.book.process('t', '2026-11-02T10:01:00+08:00', {}, invalidated_symbols=['2330'])
        self.assertEqual(self.book.snapshot()['orders'][0]['status'], 'CANCEL_PENDING')
        self.assertGreater(self.book.snapshot()['reserved'], 0)

    def test_expiry_no_force_fill_and_restart_no_resend(self):
        order = self.submit()
        restarted = LimitOrderBook(self.path, Policy('A'))
        self.assertEqual(order, restarted.submit('p1', self.at, {'2330': self.q}))
        self.book.process('close', '2026-11-02T13:30:00+08:00', {})
        s = self.book.snapshot()
        self.assertEqual(s['orders'][0]['status'], 'EXPIRED')
        self.assertEqual(s['available'], 100000)
        self.assertFalse(s['positions'])

    def test_duplicate_plan_is_immutable_and_same_symbol_pending_is_blocked(self):
        self.submit()
        with self.assertRaises(ValueError):
            self.book.create(replace(plan(), limit_price='98.4'))
        self.book.create(plan(pid='p2'))
        with self.assertRaisesRegex(ValueError, 'PENDING'):
            self.book.submit('p2', self.at, {'2330': self.q})

    def test_reservations_participate_in_sector_limit(self):
        quotes = {}
        for i, symbol in enumerate(('2330', '2317', '2454', '2308')):
            p = plan(pid='p'+str(i), symbol=symbol, qty=99)
            quotes[symbol] = quote(symbol=symbol)
            self.book.create(p)
            if i < 3:
                self.book.submit(p.plan_id, self.at, quotes)
            else:
                with self.assertRaisesRegex(ValueError, 'SECTOR_LIMIT'):
                    self.book.submit(p.plan_id, self.at, quotes)

    def test_sell_only_confirmed_holdings_then_t_plus_2(self):
        self.submit()
        q = quote('2026-11-02T10:01:00+08:00', ask=98.5, bid=98.4)
        self.book.process('buy', q['quote_timestamp'], {'2330': q})
        bad = plan(pid='same', side='SELL', at='2026-11-02T10:02:00+08:00', price='102')
        self.book.create(bad)
        with self.assertRaisesRegex(ValueError, 'SAME_DAY'):
            self.book.submit('same', bad.created_at, {'2330': quote(bad.created_at, ask=102.5, bid=102)})
        sell = plan(pid='sell', side='SELL', at='2026-11-03T10:00:00+08:00', price='102')
        self.book.create(sell)
        self.book.submit('sell', sell.created_at, {'2330': quote(sell.created_at, ask=102.5, bid=102)})
        q = quote('2026-11-03T10:01:00+08:00', ask=102.5, bid=102)
        before = self.book.snapshot()['cash']
        self.assertFalse(self.book.process('need-calendar', q['quote_timestamp'], {'2330': q}))
        q['quote_timestamp'] = '2026-11-03T10:02:00+08:00'
        fills = self.book.process('sell', q['quote_timestamp'], {'2330': q},
                                  sessions=['2026-11-04', '2026-11-05'])
        self.assertEqual(fills[0]['tax'], 16)
        self.assertEqual(self.book.snapshot()['cash'], before)
        self.assertFalse(self.book.snapshot()['positions'])
        self.book.process('settle', '2026-11-05T09:00:00+08:00', {})
        self.assertGreater(self.book.snapshot()['cash'], before)

    def test_price_ticks_and_invalid_prices(self):
        self.assertEqual(floor_stock_price('98.57'), Decimal('98.5'))
        self.assertEqual(floor_stock_price('100.49'), Decimal('100.0'))
        for price in ('98.57', 'NaN', 'Infinity', '-1'):
            with self.assertRaises(ValueError):
                plan(price=price)

    def test_planner_requires_evidence_and_preserves_price_independence(self):
        c = dict(symbol='2330', passed=True,
                 features=dict(ma={'5': 100}, atr=.02), prediction=dict(model_version='model-test'))
        rows = [dict(date='2026-10-30', close=101)]
        p = willing_entry(c, rows, Policy('A'), self.at, 50)
        self.assertLess(Decimal(p.limit_price), Decimal(100))
        self.assertIn('MA5', p.rationale)
        with self.assertRaises(ValueError):
            willing_entry(dict(c, passed=False), rows, Policy('A'), self.at, 50)

    def test_empty_quant_cycle_is_idempotent_without_llm_or_forced_orders(self):
        bundle = dict(at=self.at, quotes=[self.q])
        a = cycle(self.path, {}, bundle, Policy('A'))
        self.assertEqual(a, cycle(self.path, {}, bundle, Policy('A')))
        self.assertFalse(a['plans'])
        self.assertFalse(a['fills'])

    def test_ab_cycle_generates_resting_plan_then_fills_on_later_book(self):
        c = dict(symbol='2330', passed=True, features=dict(ma={'5': 100}, atr=.02),
                 prediction=dict(model_version='SYNTHETIC_TEST'))
        data = {'2330': [dict(date='2026-10-30', close=101)]}
        with patch('quant_research.planned_cycle.scan', return_value=[c]):
            first = cycle(self.path, data, dict(at=self.at, quotes=[self.q]), Policy('A'))
            self.assertEqual(len(first['plans']), 1)
            self.assertFalse(first['fills'])
            self.assertEqual(first['plans'][0]['limit_price'], '99.5')
            self.assertFalse(first['account']['positions'])
            q = quote('2026-11-02T10:01:00+08:00', ask=99.4, bid=99.3)
            second = cycle(self.path, data, dict(at=q['quote_timestamp'], quotes=[q]), Policy('A'))
            self.assertEqual(len(second['fills']), 1)
            self.assertFalse(second['plans'])
            self.assertEqual(second['account']['positions']['2330']['qty'], 99)

    def test_out_of_order_and_changed_tick_input_are_rejected(self):
        self.submit()
        self.book.process('tick', '2026-11-02T10:01:00+08:00', {})
        with self.assertRaisesRegex(ValueError, 'MISMATCH'):
            self.book.process('tick', '2026-11-02T10:01:00+08:00', {'2330': self.q})
        with self.assertRaisesRegex(ValueError, 'OUT_OF_ORDER'):
            self.book.process('old', '2026-11-02T10:00:30+08:00', {})
        self.book.create(plan(pid='other', symbol='2317'))
        with self.assertRaisesRegex(ValueError, 'OUT_OF_ORDER'):
            self.book.submit('other', self.at, {'2317': quote(symbol='2317')})

    def test_atomic_cash_reservation_under_competing_submissions(self):
        policy = Policy('A', position_fraction=1, sector_fraction=1, risk_fraction=1)
        book = LimitOrderBook(Path(self.tmp.name)/'concurrent.sqlite3', policy)
        for pid, symbol in (('a', '2330'), ('b', '2317')):
            book.create(plan(pid=pid, symbol=symbol, qty=700))
        def submit(pair):
            pid, symbol = pair
            try:
                book.submit(pid, self.at, {symbol: quote(symbol=symbol)})
                return 'ACCEPTED'
            except ValueError as exc:
                return str(exc)
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(submit, [('a', '2330'), ('b', '2317')]))
        self.assertEqual(results.count('ACCEPTED'), 1)
        self.assertIn('SETTLED_CASH', results)
        self.assertGreaterEqual(book.snapshot()['available'], 0)

    def test_adapter_preserves_explicit_planned_policy(self):
        from test_advice_adapter import base_payload
        payload = base_payload()
        payload['recommendations'][0]['pricing_policy'] = 'PLANNED_LIMIT'
        parsed = build_advice_intents(payload)
        self.assertEqual(parsed[0].pricing_policy, 'PLANNED_LIMIT')

    def test_formal_ledger_cannot_be_reused_as_paper_database(self):
        import sqlite3
        path = Path(self.tmp.name)/'formal.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE execution_attempts(id INTEGER)')
        db.close()
        with self.assertRaisesRegex(ValueError, 'FORBIDDEN'):
            LimitOrderBook(path, Policy('A'))
        with sqlite3.connect(path) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        db.close()
        self.assertEqual(tables, {'execution_attempts'})
