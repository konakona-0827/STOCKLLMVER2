"""Offline research semantics, conservative path ordering and immutable run inputs."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from research.multi_horizon.engine import (
    DEFAULT_PROTOCOL, DescriptiveSummary, label_path, read_bars,
    read_sessions, run_research, validate_protocol,
)


class MultiHorizonResearchTests(unittest.TestCase):
    def setUp(self):
        self.protocol = json.loads(DEFAULT_PROTOCOL.read_text(encoding='utf-8'))
        self.protocol['costs']['slippage_bps_per_side'] = '0'
        self.barrier = dict(take_profit_rate='0.02', stop_loss_rate='0.02')

    def bars(self, prices):
        return [dict(symbol='DEMO', date=date(2026, 1, 5) + timedelta(days=i),
                     open=Decimal(str(o)), high=Decimal(str(h)), low=Decimal(str(l)),
                     close=Decimal(str(c)), volume=1000, session_index=i)
                for i, (o, h, l, c) in enumerate(prices)]

    def label(self, prices, horizon=3):
        return label_path(self.bars(prices), 0, horizon, self.barrier, self.protocol)

    def test_enters_next_open_and_never_uses_signal_day_extremes(self):
        row = self.label([(100, 150, 50, 100), (120, 121, 119, 120),
                          (120, 121, 119, 120)], horizon=1)
        self.assertEqual(row['entry_scenario_price'], '120')
        self.assertEqual(row['outcome'], 'TIMEOUT')
        self.assertEqual(row['entry_date'], '2026-01-06')

    def test_entry_day_cannot_trigger_oddlot_exit(self):
        row = self.label([(100, 100, 100, 100), (100, 110, 90, 100),
                          (100, 101, 99, 100)], horizon=1)
        self.assertEqual(row['outcome'], 'TIMEOUT')
        self.assertEqual(row['holding_trading_days'], 1)
        self.assertEqual(row['exit_date'], '2026-01-07')

    def test_same_bar_both_barriers_is_stop_first(self):
        row = self.label([(100, 100, 100, 100), (100, 100, 100, 100),
                          (100, 103, 97, 100)])
        self.assertEqual(row['outcome'], 'SL')
        self.assertTrue(row['ambiguous_bar'])
        self.assertEqual(Decimal(row['exit_scenario_price']), 98)

    def test_stop_gap_uses_worse_open(self):
        row = self.label([(100, 100, 100, 100), (100, 100, 100, 100),
                          (90, 103, 89, 100)])
        self.assertEqual(row['outcome'], 'SL')
        self.assertEqual(Decimal(row['exit_scenario_price']), 90)
        self.assertFalse(row['ambiguous_bar'])

    def test_known_open_above_target_precedes_later_low(self):
        row = self.label([(100, 100, 100, 100), (100, 100, 100, 100),
                          (105, 106, 95, 100)])
        self.assertEqual(row['outcome'], 'TP')
        self.assertEqual(Decimal(row['exit_scenario_price']), 102)
        self.assertFalse(row['ambiguous_bar'])

    def test_early_observed_target_completes_even_with_short_tail(self):
        row = self.label([(100, 100, 100, 100), (100, 100, 100, 100),
                          (100, 103, 99, 102)], horizon=60)
        self.assertEqual(row['status'], 'COMPLETE')
        self.assertEqual(row['outcome'], 'TP')

    def test_incomplete_tail_is_censored_without_profit(self):
        row = self.label([(100, 100, 100, 100), (100, 100, 100, 100),
                          (100, 101, 99, 100)], horizon=5)
        self.assertEqual(row['status'], 'CENSORED')
        self.assertIsNone(row['outcome'])
        self.assertNotIn('net_pnl_twd', row)

    def test_zero_volume_entry_is_not_entered(self):
        bars = self.bars([(100, 100, 100, 100)] * 3)
        bars[1]['volume'] = 0
        row = label_path(bars, 0, 1, self.barrier, self.protocol)
        self.assertEqual(row['status'], 'NOT_ENTERED')

    def test_zero_volume_path_is_censored(self):
        bars = self.bars([(100, 100, 100, 100)] * 4)
        bars[2]['volume'] = 0
        row = label_path(bars, 0, 2, self.barrier, self.protocol)
        self.assertEqual(row['reason'], 'ZERO_VOLUME_PATH_REQUIRES_REVIEW')
        self.assertNotIn('net_pnl_twd', row)

    def test_costs_include_two_commissions_and_sell_tax(self):
        row = self.label([(100, 100, 100, 100), (100, 100, 100, 100),
                          (100, 103, 99, 102)])
        self.assertEqual(row['quantity'], 199)
        self.assertEqual(Decimal(row['commission_twd']), 58)
        self.assertEqual(Decimal(row['sell_tax_twd']), 61)
        self.assertEqual(Decimal(row['net_pnl_twd']), 279)
        self.assertLessEqual(Decimal(row['buy_cost_twd']), 20000)

    def test_slippage_reduces_price_baseline_pnl(self):
        bars = self.bars([(100, 100, 100, 100)] * 3)
        baseline = dict(take_profit_rate=None, stop_loss_rate=None)
        no_slip = label_path(bars, 0, 1, baseline, self.protocol)
        self.protocol['costs']['slippage_bps_per_side'] = '10'
        with_slip = label_path(bars, 0, 1, baseline, self.protocol)
        self.assertLess(Decimal(with_slip['net_pnl_twd']), Decimal(no_slip['net_pnl_twd']))

    def test_oddlot_quantity_and_cash_caps(self):
        self.protocol['notional_per_opportunity_twd'] = '1000000'
        row = self.label([(1, 1, 1, 1)] * 3, horizon=1)
        self.assertEqual(row['quantity'], 999)
        self.protocol['notional_per_opportunity_twd'] = '1'
        row = self.label([(1, 1, 1, 1)] * 3, horizon=1)
        self.assertEqual(row['reason'], 'INSUFFICIENT_NOTIONAL')

    def test_summary_excludes_censored_rows_from_pnl_and_frequencies(self):
        complete = self.label([(100, 100, 100, 100), (100, 100, 100, 100),
                               (100, 103, 99, 102), (102, 102, 102, 102), (102, 102, 102, 102)])
        censored = self.label([(100, 100, 100, 100)] * 2)
        summary = DescriptiveSummary()
        for row in (complete, censored):
            summary.add(dict(profile='A', **row))
        result = summary.rows()[0]
        self.assertEqual(result['complete'], 1)
        self.assertEqual(result['censored'], 1)
        self.assertEqual(result['mean_scenario_net_pnl_twd'], 279)
        self.assertEqual(result['empirical_tp_fraction'], 1)

    def test_summary_excludes_early_exits_in_unmatured_windows(self):
        row = self.label([(100, 100, 100, 100), (100, 100, 100, 100),
                          (100, 103, 99, 102)], horizon=60)
        self.assertEqual(row['status'], 'COMPLETE')
        self.assertFalse(row['window_complete'])
        summary = DescriptiveSummary()
        summary.add(dict(profile='A', **row))
        stats = summary.rows()[0]
        self.assertEqual(stats['partial_window_exits_excluded'], 1)
        self.assertEqual(stats['evaluated'], 0)
        self.assertIsNone(stats['mean_scenario_net_pnl_twd'])
        self.assertIsNone(stats['empirical_tp_fraction'])

    def test_price_baseline_has_no_tp_probability(self):
        bars = self.bars([(100, 100, 100, 100)] * 3)
        row = label_path(bars, 0, 1, dict(take_profit_rate=None, stop_loss_rate=None), self.protocol)
        summary = DescriptiveSummary()
        summary.add(dict(profile='C', **row))
        self.assertIsNone(summary.rows()[0]['empirical_tp_fraction'])

    def test_invalid_protocols_fail_closed(self):
        variants = [dict(mode='LIVE'), dict(max_oddlot_quantity=1000), dict(profiles=[])]
        for update in variants:
            protocol = deepcopy(self.protocol)
            protocol.update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate_protocol(protocol)
        for rate in ('NaN', '-1', 'Infinity'):
            protocol = deepcopy(self.protocol)
            protocol['costs']['commission_rate'] = rate
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                validate_protocol(protocol)

    def test_calendar_is_explicit_unique_sorted_and_counts_sessions(self):
        sessions = read_sessions(io.StringIO('date\n2026-01-09\n2026-01-12\n2026-01-13\n'))
        bars = read_bars(io.StringIO('symbol,date,open,high,low,close,volume\n'
                                    'DEMO,2026-01-09,100,100,100,100,100\n'
                                    'DEMO,2026-01-12,100,100,100,100,100\n'
                                    'DEMO,2026-01-13,100,100,100,100,100\n'), sessions)
        row = label_path(bars['DEMO'], 0, 1, self.barrier, self.protocol)
        self.assertEqual(row['holding_trading_days'], 1)
        for text in ('date\n', 'date\n2026-01-09\n2026-01-09\n',
                     'date\n2026-01-12\n2026-01-09\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                read_sessions(io.StringIO(text))

    def test_missing_duplicate_invalid_and_nonfinite_bars_are_rejected(self):
        sessions = [date(2026, 1, day) for day in (5, 6, 7)]
        header = 'symbol,date,open,high,low,close,volume\n'
        first = 'DEMO,2026-01-05,100,101,99,100,100\n'
        variants = [first + first, first + 'DEMO,2026-01-07,100,101,99,100,100\n',
                    'DEMO,2026-01-05,100,90,99,100,100\n',
                    'DEMO,2026-01-05,NaN,101,99,100,100\n',
                    'DEMO,2026-01-08,100,101,99,100,100\n']
        for text in variants:
            with self.subTest(text=text), self.assertRaises(ValueError):
                read_bars(io.StringIO(header + text), sessions)

    def test_run_snapshots_inputs_and_keeps_independent_profile_summaries(self):
        examples = DEFAULT_PROTOCOL.parent / 'examples'
        with tempfile.TemporaryDirectory() as folder:
            destination = run_research(examples / 'synthetic_bars.csv', examples / 'synthetic_sessions.csv',
                                       output_root=folder, dataset_note='SYNTHETIC TEST ONLY')
            manifest = json.loads((destination / 'run.json').read_text(encoding='utf-8'))
            summary = json.loads((destination / 'summary.json').read_text(encoding='utf-8'))
            self.assertFalse(manifest['broker_order_sent'])
            self.assertEqual({row['profile'] for row in summary}, {'A', 'B', 'C'})
            self.assertTrue(all(row['complete'] == 0 for row in summary if row['profile'] == 'C'))
            for name in ('bars.csv', 'sessions.csv', 'protocol.json'):
                self.assertEqual(hashlib.sha256((destination / name).read_bytes()).hexdigest(),
                                 manifest['input_sha256'][name])
            second = run_research(examples / 'synthetic_bars.csv', examples / 'synthetic_sessions.csv',
                                  output_root=folder, dataset_note='SYNTHETIC TEST ONLY')
            self.assertNotEqual(destination, second)
            self.assertEqual((destination / 'labels.csv').read_bytes(), (second / 'labels.csv').read_bytes())

    def test_invalid_dataset_writes_no_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'bad.csv').write_text('bad\n', encoding='utf-8')
            (root / 'sessions.csv').write_text('date\n2026-01-05\n', encoding='utf-8')
            output = root / 'output'
            with self.assertRaises(ValueError):
                run_research(root / 'bad.csv', root / 'sessions.csv', output_root=output, dataset_note='test')
            self.assertFalse(output.exists())

    def test_cli_works_with_invalid_live_state_and_does_not_import_live_modules(self):
        examples = DEFAULT_PROTOCOL.parent / 'examples'
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            script = '''import runpy, sys
runpy.run_module('research.multi_horizon', run_name='__main__')
assert not {'config', 'capital', 'openai', 'strategy_experiment', 'paper_portfolio'} & sys.modules.keys()
'''
            args = ['--bars', str(examples / 'synthetic_bars.csv'), '--sessions',
                    str(examples / 'synthetic_sessions.csv'), '--output-root', str(root / 'runs'),
                    '--dataset-note', 'SYNTHETIC TEST ONLY']
            environment = dict(os.environ, TRADING_STATE_ROOT=str(root / 'does-not-exist'))
            completed = subprocess.run([sys.executable, '-c', script, *args], env=environment,
                                       capture_output=True, text=True, timeout=20)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(len(list((root / 'runs').iterdir())), 1)


if __name__ == '__main__':
    unittest.main()
