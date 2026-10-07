"""Append-only market/analysis research history. No orders, cash or positions."""
import json
import sqlite3
from config import redact


class History:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS market_snapshots (
            run_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
            request_started_at TEXT NOT NULL, request_completed_at TEXT NOT NULL,
            received_at TEXT, exchange_time TEXT, data_quality TEXT NOT NULL,
            retrieval_mode TEXT NOT NULL, snapshot_json TEXT NOT NULL, raw_quote_json TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS idx_market_symbol_time ON market_snapshots(symbol,exchange_time);
          CREATE TABLE IF NOT EXISTS analyses (
            run_id TEXT PRIMARY KEY, request_started_at TEXT, response_received_at TEXT,
            request_json TEXT NOT NULL, raw_response TEXT NOT NULL,
            api_json TEXT NOT NULL, decision_json TEXT NOT NULL);
        ''')

    def save_snapshot(self, run_id, snapshot, raw):
        with self.db:
            self.db.execute('INSERT INTO market_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (run_id, snapshot['symbol'], snapshot['request_started_at'],
                             snapshot['request_completed_at'], snapshot.get('received_at'),
                             snapshot.get('exchange_time'), snapshot['data_quality'],
                             snapshot['retrieval_mode'], self.encode(snapshot), self.encode(raw)))

    def recent(self, symbol, at, limit=20):
        # Same exchange timestamp retrieved twice is not a new trend observation.
        rows = self.db.execute('''SELECT snapshot_json FROM market_snapshots
            WHERE symbol=? AND data_quality!='UNAVAILABLE' AND exchange_time<=?
              AND request_completed_at<=? AND retrieval_mode='BROKER'
            ORDER BY exchange_time DESC, request_completed_at DESC''', (symbol, at, at))
        result, seen = [], set()
        for row in rows:
            item = json.loads(row[0])
            stamp = item['exchange_time']
            if stamp in seen or item.get('is_trial'):
                continue
            seen.add(stamp)
            result.append({k: item.get(k) for k in (
                'symbol', 'exchange_time', 'received_at', 'price', 'bid', 'ask',
                'volume_shares', 'data_quality', 'source')})
            if len(result) >= limit:
                break
        return list(reversed(result))

    def save_analysis(self, run_id, record, decision):
        with self.db:
            self.db.execute('INSERT INTO analyses VALUES(?,?,?,?,?,?,?)',
                            (run_id, record.get('request_started_at'), record.get('response_received_at'),
                             self.encode(record.get('request', {})), record.get('raw_response', ''),
                             self.encode(record.get('api', {})), self.encode(decision)))

    @staticmethod
    def encode(value):
        return redact(json.dumps(value, ensure_ascii=False, allow_nan=False))

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
