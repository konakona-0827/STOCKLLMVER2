from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
import json
import tempfile
import unittest

from quant_research.core import Costs, Policy, features, predict, outcome, validate_bars, multi_horizon
from quant_research.runner import replay, RiskEngine
from quant_research.forward import ForwardPaper, exit_signal
from quant_research.scanner import scan
from token_budget import compact
from quant_research.validation import walk_forward


def bars(n=100, price=100):
    return [dict(date=(date(2024, 1, 1)+timedelta(days=i)).isoformat(),
                 open=price*(1.002**i), close=price*(1.002**i),
                 high=price*(1.002**i)*1.003, low=price*(1.002**i)*.997,
                 volume=1000000, source='SYNTHETIC_TEST', complete=True) for i in range(n)]


class QuantTests(unittest.TestCase):
    def test_costs_include_both_fees_tax_slippage_and_minimum(self):
        c = Costs(commission=0, minimum=20, tax=.003, slippage=0)
        self.assertEqual(c.pnl(100, 102, 1), -39)
        self.assertLess(Costs().pnl(100, 100, 100), -70)
        with self.assertRaises(ValueError):
            Costs(slippage=float('nan'))

    def test_daily_table_is_lossless_without_mutating_audit(self):
        rows = bars(22)
        original = json.dumps(rows)
        packed = compact(dict(daily_bars=rows))['daily_bars']
        restored = [dict(zip(packed['columns'], r)) for r in packed['rows']]
        self.assertEqual(restored, rows)
        self.assertEqual(original, json.dumps(rows))
        self.assertLess(len(json.dumps(packed)), len(original)*.75)

    def test_future_features_and_events_are_excluded(self):
        rows = bars()
        at = rows[80]['date']
        past = features(rows, at)
        rows[-1]['close'] = 99999
        result = features(rows, at, [dict(kind='revenue', source='TEST', published_at=rows[90]['date'], score=1)])
        self.assertEqual(past, result)
        visible = features(rows, at, [dict(kind='revenue', source='TEST', published_at=rows[70]['date'], score=1)])
        self.assertGreater(visible['score'], past['score'])

    def test_training_purges_future_and_reports_insufficient_evidence(self):
        rows = bars(140)
        p = Policy('A')
        x = predict(rows, rows[100]['date'], p, Costs())
        self.assertIsNone(x['expected_net'])
        self.assertLess(x['sample_count'], 30)
        self.assertLess(x['training_end'], rows[100]['date'])
        rows[-1]['high'] = 9999
        self.assertEqual(x, predict(rows, rows[100]['date'], p, Costs()))

    def test_target_touch_is_not_fill_and_gap_can_lose(self):
        rows = bars(7)
        rows[0].update(open=100, high=103, low=99, close=102)
        rows[1].update(open=90, high=91, low=89, close=90)
        y = outcome(rows, 0, Policy('A'), Costs())
        self.assertEqual(y['reason'], 'TARGET_SIGNAL')
        self.assertEqual(y['exit'], 90)
        self.assertLess(y['net'], 0)

    def test_ambiguous_bar_is_stop_first(self):
        rows = bars(7)
        rows[0].update(open=100, high=103, low=97, close=100)
        y = outcome(rows, 0, Policy('A'), Costs())
        self.assertTrue(y['stop_first'])
        self.assertIsNone(y['tp_day'])
        self.assertGreaterEqual(y['days'], 1)

    def test_horizons_are_separate_and_data_quality_missing(self):
        rows = bars(140)
        model = multi_horizon(rows, rows[-1]['date'], Policy('B', max_days=20), Costs(), 100)
        self.assertEqual(set(model), {'5', '10', '20'})
        self.assertGreater(model['5']['sample_count'], model['20']['sample_count'])
        self.assertIn('earnings', features(rows, rows[-1]['date'])['missing_factors'])

    def test_duplicate_and_invalid_ohlc_are_rejected(self):
        rows = bars(2)
        with self.assertRaises(ValueError):
            validate_bars(rows+rows)
        rows[0]['open'] = float('nan')
        with self.assertRaises(ValueError):
            validate_bars(rows)

    def test_scanner_batches_1000_and_records_each_rejection(self):
        calls = []
        def provider(symbols, at):
            calls.append(symbols)
            return {}
        results = scan([str(i) for i in range(1000)], provider, '2026-10-30', Policy('A'), Costs())
        self.assertEqual(len(results), 1000)
        self.assertEqual(len(calls), 20)
        self.assertTrue(all(not r['passed'] for r in results))

    def test_shadow_separate_and_no_forced_daily_trade(self):
        result = replay({'2330': bars(80)}, Policy('A'))
        self.assertFalse(result['trades'])
        self.assertFalse(result['shadow'])
        self.assertEqual(result['metrics']['net_asset_return'], 0)
        base = replay({'2330': bars(80)}, Policy('BASELINE'), initial=100)
        self.assertTrue(base['shadow'])
        self.assertFalse(base['trades'])

    def test_version_and_strategy_isolation(self):
        self.assertNotEqual(replay({}, Policy('A'))['identity']['config_sha256'],
                            replay({}, Policy('B'))['identity']['config_sha256'])

    def test_walk_forward_strict_chronological_folds(self):
        result = walk_forward({'2330': bars(100)}, Policy('A'), train_sessions=80, test_sessions=10)
        self.assertEqual(len(result['folds']), 2)
        for fold in result['folds']:
            self.assertLess(fold['train_end'], fold['test_start'])
        self.assertLess(result['folds'][0]['test_end'], result['folds'][1]['test_start'])

    def test_future_changes_do_not_change_earlier_decisions(self):
        rows = bars(90)
        a = replay({'2330': rows}, Policy('BASELINE'), end=rows[75]['date'])
        rows[-1].update(open=10000, high=11000, low=9000, close=10000)
        b = replay({'2330': rows}, Policy('BASELINE'), end=rows[75]['date'])
        self.assertEqual(a, b)

    def test_risk_caps(self):
        r = RiskEngine()
        args = ('2330', 'UNKNOWN', 20, 100, 100000, 100000, 100000, {}, Policy('A'), Costs())
        self.assertIsNone(r.check(*args))
        self.assertEqual(r.check(*args[:4], 0, *args[5:]), 'SETTLED_CASH')
        self.assertEqual(r.check(*args[:5], 80000, 100000, *args[7:]), 'DRAWDOWN_LIMIT')


class ForwardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'A.sqlite3'
        self.paper = ForwardPaper(self.path, Policy('A'))
        self.at = '2026-10-30T10:00:00+08:00'
        self.q = dict(symbol='2330', quote_status='LIVE', quote_timestamp=self.at,
                      bid=99.9, ask=100, last_price=100, ask_size=10, bid_size=10,
                      lot_type='INTRADAY_ODD_LOT', book_size_unit='shares')

    def test_idempotency_partial_and_same_day_sell(self):
        fill = self.paper.execute('one', self.at, '2330', 'BUY', 20, self.q)
        self.assertEqual(fill['filled_qty'], 10, fill)
        self.assertEqual(fill['status'], 'PARTIAL_REMAINDER_CANCELLED')
        self.assertEqual(fill, self.paper.execute('one', self.at, '2330', 'BUY', 20, self.q))
        sell = self.paper.execute('two', self.at, '2330', 'SELL', 10, self.q)
        self.assertEqual(sell['status'], 'REJECTED')
        with self.paper.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM positions').fetchone()[0], 1)

    def test_settlement_not_reusable_and_version_immutable(self):
        self.paper.execute('buy', self.at, '2330', 'BUY', 10, self.q)
        at = '2026-11-02T10:00:00+08:00'
        quote = dict(self.q, quote_timestamp=at, bid=102, ask=102.1)
        result = self.paper.execute('sell', at, '2330', 'SELL', 10, quote,
                                    settlement_date='2026-11-04', sessions=['2026-11-03', '2026-11-04'])
        self.assertEqual(result['status'], 'FILLED', result)
        with self.paper.connect() as db:
            self.assertGreater(db.execute('SELECT sum(amount) FROM settlement').fetchone()[0], 0)
            self.assertLess(db.execute('SELECT cash FROM state').fetchone()[0], 100000)
        with self.assertRaises(ValueError):
            ForwardPaper(self.path, replace(Policy('A'), target=.02))

    def test_future_stale_and_missing_book_fail_closed(self):
        for i, quote in enumerate([dict(self.q, quote_timestamp='2026-10-30T11:00:00+08:00'),
                                   dict(self.q, quote_timestamp='2026-10-30T09:00:00+08:00'),
                                   dict(self.q, ask_size=None), dict(self.q, is_trial=True)]):
            r = self.paper.execute(str(i), self.at, '2330', 'BUY', 10, quote)
            self.assertEqual(r['status'], 'REJECTED')

    def test_swing_hard_time_limit(self):
        signal, _ = exit_signal(dict(stop=95, target=110), dict(bid=102), 20,
                                Policy('B', target=.06, stop=.03, max_days=20))
        self.assertEqual(signal, 'SELL_TIME_REEVALUATION')

    def test_older_forward_cycle_cannot_rewrite_the_past(self):
        self.paper.execute('buy', self.at, '2330', 'BUY', 10, self.q)
        with self.assertRaises(ValueError):
            self.paper.execute('old', '2026-10-29T10:00:00+08:00', '2330', 'BUY', 10, self.q)

    def test_forward_cycle_is_replayable_and_independent(self):
        from quant_research.cycle import cycle
        data = {'2330': bars(80)}
        bundle = dict(at=self.at, quotes=[self.q], sessions=['2026-10-29', '2026-10-30'])
        a = cycle(self.path, data, bundle, Policy('A'))
        self.assertEqual(a, cycle(self.path, data, bundle, Policy('A')))
        self.assertFalse(a['execution'])
        b = cycle(Path(self.tmp.name)/'B.sqlite3', data, bundle,
                  Policy('B', max_days=20, target=.06, stop=.03))
        self.assertEqual(b['strategy'], 'B')
