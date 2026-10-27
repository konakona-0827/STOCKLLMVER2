import ast
from datetime import datetime, timedelta
import io
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import patch
from config import TAIPEI
from main import snapshot, analyze, display
from llm import validate, SCHEMA
from capital import normalize_stock
from history import History


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 10, 7, 10, tzinfo=TAIPEI)
        self.quote = dict(symbol='0050', source='CAPITAL_SKCOM', exchange_time=self.at.isoformat(),
                          fetched_at=self.at.isoformat(), price=100, bid=99.9, ask=100.1,
                          volume_shares=1000, quote_basis='capital_intraday_odd_lot', is_trial=False)
        self.snap = snapshot('0050', self.quote, at=self.at)
        self.reply = dict(decision='BUY', symbol='0050', confidence=.6, reason='測試',
                          reference_price=100, suggested_price=99.9, suggested_qty=2,
                          data_quality='LIVE', warnings=[])

    def provider(self, payload):
        return json.dumps(self.reply), {'model': 'TEST_ONLY'}

    def test_live_timestamp(self):
        self.assertEqual(self.snap['data_quality'], 'LIVE')

    def test_closed_market_last_known(self):
        q = {**self.quote, 'exchange_time': self.at.replace(hour=16).isoformat()}
        s = snapshot('0050', q, at=self.at.replace(hour=16))
        self.assertEqual(s['data_quality'], 'LAST_KNOWN')
        self.assertIn('不是即時行情', s['warnings'][0])

    def test_stale_last_known(self):
        self.assertEqual(snapshot('0050', self.quote, at=self.at+timedelta(minutes=3))['data_quality'], 'LAST_KNOWN')

    def test_unavailable_without_fabrication(self):
        s = snapshot('0050', at=self.at)
        r, _ = analyze(s, provider=self.provider)
        self.assertEqual(r['decision'], 'WAIT')
        for k in ('reference_price', 'suggested_price', 'suggested_qty'):
            self.assertIsNone(r[k])

    def test_reject_wrong_source_symbol_and_future(self):
        for change in [dict(source='TWSE_MIS'), dict(symbol='2330'),
                       dict(exchange_time=(self.at+timedelta(seconds=1)).isoformat()), dict(price=float('nan'))]:
            self.assertEqual(snapshot('0050', {**self.quote, **change}, at=self.at)['data_quality'], 'UNAVAILABLE')

    def test_schema_exact_keys(self):
        self.assertEqual(set(SCHEMA['required']), set(self.reply))
        with self.assertRaises(ValueError):
            validate({**self.reply, 'order': True}, self.snap)

    def test_invalid_confidence(self):
        for v in [True, -1, 1.1, float('nan')]:
            with self.assertRaises(ValueError):
                validate({**self.reply, 'confidence': v}, self.snap)

    def test_invalid_qty(self):
        for v in [True, 0, -1, 1.5, 1000]:
            with self.assertRaises(ValueError):
                validate({**self.reply, 'suggested_qty': v}, self.snap)

    def test_invalid_price(self):
        for v in [True, -1, float('inf')]:
            with self.assertRaises(ValueError):
                validate({**self.reply, 'suggested_price': v}, self.snap)

    def test_no_budget_no_buy_quantity(self):
        result, _ = analyze(self.snap, provider=self.provider)
        self.assertIsNone(result['suggested_qty'])
        self.assertEqual(result['decision'], 'BUY')

    def test_hold_sell_require_position(self):
        for decision in ['HOLD', 'SELL']:
            self.assertEqual(validate({**self.reply, 'decision': decision}, self.snap)['decision'], 'WAIT')

    def test_sell_provided_position(self):
        r = validate({**self.reply, 'decision': 'SELL'}, self.snap, held_qty=3)
        self.assertEqual(r['suggested_qty'], 2)
        with self.assertRaises(ValueError):
            validate({**self.reply, 'decision': 'SELL'}, self.snap, held_qty=1)

    def test_last_known_cannot_be_upgraded_by_model(self):
        s = {**self.snap, 'data_quality': 'LAST_KNOWN'}
        r, _ = analyze(s, provider=self.provider)
        self.assertEqual(r['data_quality'], 'LAST_KNOWN')
        self.assertIn('不是即時行情', r['reason'])

    def test_reference_price_is_actual_snapshot(self):
        self.assertEqual(validate({**self.reply, 'reference_price': 999}, self.snap)['reference_price'], 100)

    def test_bad_json_wait_and_save_raw(self):
        result, record = analyze(self.snap, provider=lambda _: ('bad json', {}))
        self.assertEqual(result['decision'], 'WAIT')
        self.assertEqual(record['raw_response'], 'bad json')

    def test_api_failure_wait(self):
        def fail(_): raise TimeoutError('test')
        result, _ = analyze(self.snap, provider=fail)
        self.assertEqual(result['decision'], 'WAIT')

    def test_trial_wait(self):
        r, _ = analyze({**self.snap, 'is_trial': True}, provider=self.provider)
        self.assertEqual(r['decision'], 'WAIT')

    def test_output_format(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer): display(self.reply)
        self.assertIn('AI TRADING DECISION', buffer.getvalue())
        self.assertIn('60%', buffer.getvalue())
        self.assertIn('REAL ORDER SENT = NO', buffer.getvalue())

    def test_no_ledger_or_order_dependencies(self):
        for file in ['main.py', 'capital.py', 'llm.py']:
            tree = ast.parse(Path(__file__).with_name(file).read_text(encoding='utf-8'))
            imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            self.assertFalse(imports & {'ledger', 'strategy', 'sqlite3'})
            attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            self.assertFalse(any(a.startswith(('SendStock', 'SendFuture', 'SendOption', 'CancelOrder', 'paper_fill')) for a in attrs))
            if file == 'capital.py':
                self.assertNotIn('GetRealBalanceReport', attrs)
                self.assertNotIn('SKOrderLib_Initialize', attrs)

    def test_history_append_and_timestamp_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'history.sqlite3'
            with History(path) as h:
                base = {**self.snap, 'request_started_at': self.at.isoformat(),
                        'request_completed_at': self.at.isoformat(), 'retrieval_mode': 'BROKER'}
                h.save_snapshot('a', base, self.quote)
                h.save_snapshot('b', base, self.quote)
                self.assertEqual(h.db.execute('SELECT COUNT(*) FROM market_snapshots').fetchone()[0], 2)
                self.assertEqual(len(h.recent('0050', self.at.isoformat())), 1)
                self.assertEqual(h.recent('0050', (self.at-timedelta(seconds=1)).isoformat()), [])
                self.assertEqual(h.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            with History(path) as h:
                self.assertEqual(len(h.recent('0050', self.at.isoformat())), 1)

    def test_cache_not_counted_as_trend_sample(self):
        with tempfile.TemporaryDirectory() as tmp, History(Path(tmp)/'history.sqlite3') as h:
            s = {**self.snap, 'request_started_at': self.at.isoformat(),
                 'request_completed_at': self.at.isoformat(), 'retrieval_mode': 'CACHE'}
            h.save_snapshot('cache', s, self.quote)
            self.assertEqual(h.recent('0050', self.at.isoformat()), [])

    def test_complete_prompt_request_and_reply_are_preserved(self):
        requests = []
        result, record = analyze(self.snap, provider=self.provider, observations=[self.snap], on_request=requests.append)
        self.assertEqual(json.loads(requests[0]['input']), record['input'])
        self.assertEqual(record['request'], requests[0])
        self.assertIn('market_history', record['input'])
        self.assertEqual(json.loads(record['raw_response']), self.reply)
        self.assertIn('request_started_at', record)
        self.assertIn('response_received_at', record)


if __name__ == '__main__': unittest.main()
