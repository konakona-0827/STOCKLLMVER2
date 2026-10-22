"""Realized price difference uses confirmed sale prices and historical cost."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from execution.trade_pnl import realized_price_difference, remaining_acquisition_costs


class TradePnlTests(unittest.TestCase):
    def test_partial_sale_and_estimated_purchase_cost(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'trades.sqlite3'
            with closing(sqlite3.connect(path)) as con:
                con.executescript('''
                    CREATE TABLE execution_attempts (
                        decision_id TEXT, symbol TEXT, side TEXT,
                        started_at TEXT, detail_json TEXT);
                ''')
                rows = [
                    ('buy-a', 'AAA', 'BUY', '2026-10-14T01:00:00+00:00',
                     {'filled_quantity': 10, 'broker_replay_fill_details':
                      [{'quantity': 10, 'price_twd': '100'}]}),
                    ('sell-a', 'AAA', 'SELL', '2026-10-14T02:00:00+00:00',
                     {'filled_quantity': 4}),
                    ('sell-b', 'AAA', 'SELL', '2026-10-14T03:00:00+00:00',
                     {'filled_quantity': 6}),
                    ('buy-b', 'BBB', 'BUY', '2026-10-14T01:00:00+00:00',
                     {'filled_quantity': 5, 'limit_price': '50'}),
                    ('sell-c', 'BBB', 'SELL', '2026-10-14T02:00:00+00:00',
                     {'filled_quantity': 5}),
                ]
                con.executemany('INSERT INTO execution_attempts VALUES(?,?,?,?,?)',
                                [(key, symbol, side, at, json.dumps(detail))
                                 for key, symbol, side, at, detail in rows])
                con.commit()
            checks = [
                {'decision_id': 'sell-a', 'broker_replay_fill_details':
                 [{'quantity': 4, 'price_twd': '110'}]},
                {'decision_id': 'sell-c', 'broker_replay_fill_details':
                 [{'quantity': 5, 'price_twd': '55'}]},
            ]
            result = realized_price_difference(path, checks)
            self.assertEqual((Decimal('40'), False), result['sell-a'])
            self.assertNotIn('sell-b', result)  # No verified sale price.
            self.assertEqual((Decimal('25'), True), result['sell-c'])
            self.assertNotIn('buy-a', result)

    def test_adopted_cost_is_marked_estimated(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'trades.sqlite3'
            with closing(sqlite3.connect(path)) as con:
                con.executescript('''
                    CREATE TABLE execution_attempts (
                        decision_id TEXT, symbol TEXT, side TEXT,
                        started_at TEXT, detail_json TEXT);
                    CREATE TABLE manual_ai_adoptions (
                        id INTEGER PRIMARY KEY, symbol TEXT, qty INTEGER,
                        unit_cost_twd TEXT, adopted_at TEXT);
                ''')
                con.execute('INSERT INTO manual_ai_adoptions VALUES(1,?,?,?,?)',
                            ('AAA', 3, '80', '2026-10-14T01:00:00+00:00'))
                con.execute('INSERT INTO execution_attempts VALUES(?,?,?,?,?)',
                            ('sell', 'AAA', 'SELL', '2026-10-14T02:00:00+00:00',
                             json.dumps({'filled_quantity': 3})))
                con.commit()
            result = realized_price_difference(path, [{
                'decision_id': 'sell', 'broker_replay_fill_details':
                [{'quantity': 3, 'price_twd': '85'}],
            }])
            self.assertEqual((Decimal('15'), True), result['sell'])

    def test_remaining_manual_acquisition_cost_is_available_to_ai(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'trades.sqlite3'
            with closing(sqlite3.connect(path)) as con:
                con.executescript('''
                    CREATE TABLE execution_attempts (
                        decision_id TEXT, symbol TEXT, side TEXT,
                        started_at TEXT, detail_json TEXT);
                    CREATE TABLE manual_ai_adoptions (
                        id INTEGER PRIMARY KEY, symbol TEXT, qty INTEGER,
                        unit_cost_twd TEXT, adopted_at TEXT);
                ''')
                con.execute('INSERT INTO manual_ai_adoptions VALUES(1,?,?,?,?)',
                            ('1101', 100, '26.5', '2026-10-13T03:12:42+00:00'))
                con.commit()
            self.assertEqual(
                {'quantity': 100, 'average_unit_cost_twd': '26.5', 'estimated': True},
                remaining_acquisition_costs(path, [])['1101'])


if __name__ == '__main__':
    unittest.main()
