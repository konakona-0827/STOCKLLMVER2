"""Preserve historical trading days when sampling frequent intraday scans."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import scan_store


class RecentHistoryTests(unittest.TestCase):
    def test_recent_history_keeps_prior_trading_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'history.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                scan_store.initialize(db)
                for day, count in [('2026-10-07', 4), ('2026-10-08', 8),
                                   ('2026-10-12', 50)]:
                    for minute in range(count):
                        stamp = (datetime.fromisoformat(day + 'T09:00:00+08:00')
                                 + timedelta(minutes=minute)).isoformat()
                        payload = {'exchange_time': stamp, 'last_price': 100 + minute,
                                   'volume': minute * 100}
                        db.execute('INSERT INTO multi_market_snapshots VALUES(?,?,?,?,?,?,?,?)',
                                   (day + str(minute), '0050', stamp, stamp, stamp,
                                    'LIVE', json.dumps(payload), '{}'))
                db.commit()
            history = scan_store.recent_history(
                path, '0050', '2026-10-12T11:00:00+08:00')
            dates = Counter(row['exchange_time'][:10] for row in history)
            self.assertEqual(dates, {'2026-10-07': 3, '2026-10-08': 3,
                                     '2026-10-12': 3})
            self.assertEqual(history, sorted(history, key=lambda x: x['exchange_time']))


if __name__ == '__main__':
    unittest.main()
