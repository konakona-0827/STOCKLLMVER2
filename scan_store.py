"""Additive multi-scan tables in the existing market research database."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from config import redact


def encode(value):
    return redact(json.dumps(value, ensure_ascii=False, allow_nan=False))


def saved_analysis(path, run_id=None):
    """Read an existing analysis without depending on disposable JSON exports."""
    path = Path(path)
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        try:
            condition, arguments = ('WHERE r.run_id=?', (run_id,)) if run_id else ('', ())
            row = db.execute('SELECT r.run_id,r.manifest_json,a.request_json,a.response_json,a.decision_json '
                'FROM scan_runs r JOIN selection_analyses a USING(run_id) ' + condition +
                ' ORDER BY r.rowid DESC LIMIT 1', arguments).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        return dict(run_id=row[0], **{key: json.loads(raw or '{}') for key, raw in zip(
            ('manifest', 'request', 'response', 'decision'), row[1:])})


def historical_dashboard(path):
    """Display-only reconstruction; never restore broker health, advice or executable orders."""
    record = saved_analysis(path)
    if record is None:
        return None
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        quotes = [json.loads(row[0]) for row in db.execute(
            'SELECT snapshot_json FROM multi_market_snapshots WHERE run_id=? ORDER BY rowid',
            (record['run_id'],))]
        top = [json.loads(row[0]) for row in db.execute(
            'SELECT ranking_json FROM candidate_rankings WHERE run_id=? AND rank<=10 ORDER BY rank',
            (record['run_id'],))]
    manifest = record['manifest']
    manifest['display_notice'] = ('歷史分析由資料庫還原；非本輪行情、對帳或送單結果。' +
                                  manifest.get('display_notice', ''))
    manifest.setdefault('universe_count', len(quotes))
    inputs = record['request'].get('input') or '{}'
    inputs = json.loads(inputs) if isinstance(inputs, str) else inputs
    response = record['response']
    return dict(manifest=manifest, historical_view_only=True,
        batch=dict(quotes=quotes, symbols_received=manifest.get('symbols_received', len(quotes))),
        top=top, analysis_set=inputs.get('analysis_set', []), positions=[], decision=record['decision'],
        pretrade_review=response.get('pretrade_review'), support_research=response.get('support_research'),
        web_search=response.get('api', {}).get('web_search'))


def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS scan_runs(run_id TEXT PRIMARY KEY, manifest_json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS multi_market_snapshots(
        run_id TEXT, symbol TEXT, exchange_time TEXT, received_at TEXT,
        request_completed_at TEXT, quote_status TEXT, snapshot_json TEXT, raw_json TEXT,
        PRIMARY KEY(run_id,symbol));
      CREATE INDEX IF NOT EXISTS idx_multi_history ON multi_market_snapshots(symbol,exchange_time);
      CREATE TABLE IF NOT EXISTS quant_metrics(run_id TEXT,symbol TEXT,metrics_json TEXT,
        PRIMARY KEY(run_id,symbol));
      CREATE TABLE IF NOT EXISTS candidate_rankings(run_id TEXT,symbol TEXT,rank INTEGER,
        quant_score REAL,ranking_json TEXT,PRIMARY KEY(run_id,symbol));
      CREATE TABLE IF NOT EXISTS selection_analyses(run_id TEXT PRIMARY KEY,
        request_json TEXT,response_json TEXT,decision_json TEXT);
    ''')


def recent_history(path, symbol, at):
    db = sqlite3.connect(path)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows = []
        for table, quality in [('market_snapshots', 'data_quality'), ('multi_market_snapshots', 'quote_status')]:
            if table not in tables:
                continue
            # Sampling only the newest few rows hides prior trading days when
            # intraday scans are frequent. Keep distinct timestamps per day.
            rows += db.execute(f'''WITH distinct_stamps AS (
                    SELECT snapshot_json, exchange_time,
                           ROW_NUMBER() OVER (
                               PARTITION BY exchange_time
                               ORDER BY request_completed_at DESC) AS stamp_rank
                    FROM {table} WHERE symbol=? AND exchange_time IS NOT NULL
                      AND exchange_time<=? AND request_completed_at<=?
                      AND {quality}!='UNAVAILABLE'
                ), sampled_days AS (
                    SELECT snapshot_json, exchange_time,
                           ROW_NUMBER() OVER (
                               PARTITION BY substr(exchange_time,1,10)
                               ORDER BY exchange_time DESC) AS day_rank
                    FROM distinct_stamps WHERE stamp_rank=1
                ) SELECT snapshot_json FROM sampled_days WHERE day_rank<=10
                  ORDER BY exchange_time DESC LIMIT 100''', (symbol, at, at)).fetchall()
        result, seen, per_day = [], set(), {}
        for row in sorted((json.loads(r[0]) for r in rows), key=lambda r: r['exchange_time'], reverse=True):
            stamp = row['exchange_time']
            if stamp in seen or row.get('retrieval_mode') == 'CACHE' or row.get('is_trial'):
                continue
            day = stamp[:10]
            if day not in per_day and len(per_day) >= 5:
                continue
            if per_day.get(day, 0) >= 3:
                continue
            seen.add(stamp)
            per_day[day] = per_day.get(day, 0) + 1
            result.append(dict(exchange_time=stamp, price=row.get('last_price', row.get('price')),
                               volume_shares=row.get('volume', row.get('volume_shares'))))
            if len(result) >= 15:
                break
        return list(reversed(result))
    finally:
        db.close()


def persist(path, manifest, batch, raw, scanned, ranking, request, response, decision):
    db = sqlite3.connect(path)
    try:
        initialize(db)
        run = manifest['run_id']
        with db:
            db.execute('INSERT INTO scan_runs VALUES(?,?)', (run, encode(manifest)))
            db.executemany('INSERT INTO multi_market_snapshots VALUES(?,?,?,?,?,?,?,?)',
                [(run, q['symbol'], q.get('exchange_time'), q.get('received_at'),
                  q['request_completed_at'], q['quote_status'], encode(q), encode(raw.get(q['symbol']))) for q in batch['quotes']])
            db.executemany('INSERT INTO quant_metrics VALUES(?,?,?)', [(run, q['symbol'], encode(q)) for q in scanned])
            db.executemany('INSERT INTO candidate_rankings VALUES(?,?,?,?,?)',
                [(run, q['symbol'], q['rank'], q['quant_score'], encode(q)) for q in ranking])
            db.execute('INSERT INTO selection_analyses VALUES(?,?,?,?)',
                       (run, encode(request), encode(response), encode(decision)))
        return dict(market_snapshots=len(batch['quotes']), quant_metrics=len(scanned),
                    candidate_rankings=len(ranking), ai_decisions=1,
                    integrity_check=db.execute('PRAGMA integrity_check').fetchone()[0])
    finally:
        db.close()
