import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from execution.trade_dates import dated_ai_trades
from position_analyzer import build_request


class TradeDatesTests(unittest.TestCase):
    def test_only_filled_trades_and_labelled_manual_entry_reach_ai(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'execution.sqlite3'
            with closing(sqlite3.connect(path)) as con:
                con.executescript('''
                    CREATE TABLE execution_attempts (
                        symbol TEXT, side TEXT, started_at TEXT, detail_json TEXT);
                    CREATE TABLE manual_ai_adoptions (
                        symbol TEXT, qty INTEGER, adopted_at TEXT);
                ''')
                con.executemany('INSERT INTO execution_attempts VALUES(?,?,?,?)', [
                    ('1101', 'BUY', '2026-10-12T03:00:00+00:00',
                     json.dumps({'filled_quantity': 0})),
                    ('1101', 'SELL', '2026-10-14T03:00:00+00:00',
                     json.dumps({'filled_quantity': 30})),
                    ('0050', 'BUY', '2026-10-14T03:00:00+00:00',
                     json.dumps({'filled_quantity': 10})),
                ])
                con.execute('INSERT INTO manual_ai_adoptions VALUES(?,?,?)',
                            ('1101', 100, '2026-10-13T03:12:42+00:00'))
                con.commit()
            dates = dated_ai_trades(path, ['1101', '0050'])
            self.assertEqual([
                {'side': 'BUY', 'date': '2026-10-13', 'quantity': 100,
                 'date_basis': 'MANUAL_PURCHASE_ENTRY_DATE'},
                {'side': 'SELL', 'date': '2026-10-14', 'quantity': 30,
                 'date_basis': 'FILLED_ORDER_SUBMISSION_DATE'},
            ], dates['1101']['records'])
            self.assertEqual('2026-10-14', dates['0050']['records'][0]['date'])
            request = build_request([
                {'symbol': '1101', 'ai_trade_dates': dates['1101'], 'max_sell_qty': 0}
            ], strategy_prompt='測試策略')
            self.assertEqual(dates['1101'],
                             json.loads(request['input'])['analysis_set'][0]['ai_trade_dates'])
            self.assertIn('MANUAL_PURCHASE_ENTRY_DATE', request['instructions'])


if __name__ == '__main__':
    unittest.main()
