"""SKCOM snapshot -> OpenAI advice -> display -> exit. No broker execution."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import sys
import uuid
from config import ROOT, load_env, now, redact, MAX_QUOTE_AGE_SECONDS
from capital import CapitalMarket
from history import History
from universe import load_universe
import llm


def save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(redact(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)), encoding='utf-8')
    temp.replace(path)


class Audit:
    def __init__(self, directory):
        self.directory = directory
        self.events = []

    def event(self, kind, detail):
        self.events.append(dict(timestamp=now().isoformat(), kind=kind, detail=detail))
        save_json(self.directory / 'connection.json', self.events)


@contextmanager
def single_instance(path):
    f = path.open('a+b')
    f.seek(0, 2)
    if not f.tell():
        f.write(b'0')
        f.flush()
    f.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        f.close()


def snapshot(symbol, quote=None, warnings=None, at=None, asset_type='UNKNOWN'):
    at = at or now()
    result = dict(symbol=symbol, name=None, asset_type=asset_type, source='CAPITAL_SKCOM', data_quality='UNAVAILABLE',
                  exchange_time=None, fetched_at=None, price=None, bid=None, ask=None,
                  volume_shares=None, warnings=list(warnings or []))
    if not quote or quote.get('source') != 'CAPITAL_SKCOM' or quote.get('symbol') != symbol:
        result['warnings'].append('沒有取得指定標的的SKCOM行情。')
        return result
    try:
        stamp = datetime.fromisoformat(quote['exchange_time'])
        if stamp.tzinfo is None:
            raise ValueError('NAIVE_TIMESTAMP')
        age = (at - stamp).total_seconds()
        if age < 0:
            raise ValueError('FUTURE_TIMESTAMP')
        values = [quote.get(k) for k in ('price', 'bid', 'ask')]
        if any(v is not None and (type(v) not in (int, float) or not math.isfinite(v) or v <= 0) for v in values):
            raise ValueError('INVALID_PRICE')
        if not any(v is not None for v in values):
            raise ValueError('NO_PRICES')
    except (ValueError, TypeError, KeyError):
        result['warnings'].append('行情價格或時間無法驗證。')
        return result
    result['name'] = quote.get('name')
    for key in ('exchange_time', 'fetched_at', 'received_at', 'callback_received_at',
                'price', 'bid', 'ask', 'volume_shares', 'quote_basis', 'is_trial', 'retrieval_method'):
        result[key] = quote.get(key)
    market_open = at.weekday() < 5 and 9 * 60 <= at.hour * 60 + at.minute < 13 * 60 + 30
    result['data_quality'] = 'LIVE' if market_open and age <= MAX_QUOTE_AGE_SECONDS else 'LAST_KNOWN'
    if result['data_quality'] == 'LAST_KNOWN':
        result['warnings'].append('這不是即時行情；交易時間為 ' + quote['exchange_time'])
    if quote.get('is_trial'):
        result['warnings'].append('試撮資訊不是確定成交。')
    return result


def analyze(snap, held_qty=None, budget=None, provider=llm.decide, observations=None, on_request=None):
    payload = dict(market_snapshot=snap, user_position={'qty': held_qty, 'source': 'USER_PROVIDED' if held_qty is not None else 'UNKNOWN'},
                   user_budget_twd=budget, purpose='ANALYSIS_ONLY_NO_ORDER',
                   market_history=observations or [],
                   history_note='Real stored snapshots only; duplicate exchange times excluded; not daily bars')
    raw, api = '', {}
    started = now().isoformat()
    request = llm.build_request(payload)
    try:
        if on_request:
            on_request(request)
        raw, api = provider(payload)
        result = llm.validate(json.loads(raw), snap, held_qty)
        if snap.get('is_trial'):
            result = llm.fallback(snap, '試撮行情不足以確認交易動作，等待正式行情。', 'TRIAL_QUOTE')
        if result['decision'] == 'BUY' and budget is None:
            result['suggested_qty'] = None
        if result['data_quality'] == 'LAST_KNOWN' and '不是即時行情' not in result['reason']:
            result['reason'] = '這不是即時行情。' + result['reason']
    except Exception as exc:
        api['error'] = {'type': type(exc).__name__, 'detail': redact(exc)}
        result = llm.fallback(snap, '分析服務或JSON驗證未完成，等待有效分析。', 'OPENAI_OR_JSON_ERROR')
    name = snap.get('name') or '股票名稱未取得'
    prefix = f"{name}（{snap['symbol']}）："
    if not result['reason'].startswith(prefix):
        result['reason'] = prefix + result['reason']
    return result, dict(input=payload, request=request, raw_response=redact(raw), api=api,
                        request_started_at=started, response_received_at=now().isoformat())


def display(result, name=None):
    value = lambda x: 'null' if x is None else str(x)
    print('\n==============================\nAI TRADING DECISION\n==============================')
    for label, data in [('Symbol', f"{name or '名稱未取得'} ({result['symbol']})"), ('Market Data', result['data_quality']),
                        ('Decision', result['decision']), ('Confidence', f"{result['confidence']:.0%}"),
                        ('Reference', value(result['reference_price'])),
                        ('Suggested', value(result['suggested_price'])), ('Suggested Qty', value(result['suggested_qty'])),
                        ('Reason', result['reason']), ('Warnings', '; '.join(result['warnings']) or 'None')]:
        print(f'{label:<13}: {data}')
    print('==============================\nREAL ORDER SENT = NO', flush=True)


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbol', default='0050', help='TWSE stock/ETF symbol')
    parser.add_argument('--held-qty', type=int, default=None, help='Optional position supplied by user; never modified')
    parser.add_argument('--budget-twd', type=float, default=None, help='Optional user budget; never maintained as an account')
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data' / 'analysis')
    parser.add_argument('--asset-type', choices=['AUTO', 'STOCK', 'ETF'], default='AUTO',
                        help='Optional type for symbols outside the configured universe')
    args = parser.parse_args()
    if not re.fullmatch(r'(?:[0-9]{4,6}|[0-9]{4,5}[A-Z])', args.symbol):
        parser.error('symbol must be a 4..6 character TWSE security code')
    if args.held_qty is not None and args.held_qty < 0:
        parser.error('held-qty cannot be negative')
    if args.budget_twd is not None and (not math.isfinite(args.budget_twd) or args.budget_twd <= 0):
        parser.error('budget must be finite and positive')
    load_env()
    universe = load_universe(ROOT/'config'/'universe_tw.json')
    configured_etfs = set(universe.get('etf_symbols', []))
    asset_type = ('ETF' if args.symbol in configured_etfs else
                  'STOCK' if args.symbol in universe['symbols'] else 'UNKNOWN')
    if args.asset_type != 'AUTO':
        asset_type = args.asset_type
    args.data_dir.mkdir(parents=True, exist_ok=True)
    with single_instance(args.data_dir / 'runtime.lock'), History(args.data_dir / 'market_history.sqlite3') as history:
        run_id = now().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
        run_dir = args.data_dir / 'runs' / run_id
        run_dir.mkdir(parents=True)
        request_started = now().isoformat()
        stop = args.data_dir / 'stop.signal'
        stop.unlink(missing_ok=True)
        audit = Audit(run_dir)
        market = CapitalMarket(audit, stopped=stop.exists)
        quote, warnings, retrieval_mode = None, [], 'BROKER'
        try:
            quotes, warnings = market.fetch([args.symbol])
            quote = next((q for q in quotes if q['symbol'] == args.symbol), None)
            if quote:
                save_json(args.data_dir / f'quote_{args.symbol}.json', quote)
        except Exception as exc:
            warnings.append('SKCOM行情取得失敗：' + redact(exc))
            audit.event('QUOTE_ERROR', {'type': type(exc).__name__, 'detail': redact(exc)})
            cache = args.data_dir / f'quote_{args.symbol}.json'
            if cache.exists():
                try:
                    quote = json.loads(cache.read_text(encoding='utf-8'))
                    retrieval_mode = 'CACHE'
                    warnings.append('使用先前SKCOM最後已知快照。')
                except (ValueError, OSError):
                    pass
        finally:
            request_completed = now().isoformat()
            market.close()
            save_json(args.data_dir / 'connection.json', audit.events)
        snap = snapshot(args.symbol, quote, warnings, asset_type=asset_type)
        if quote and any('使用先前' in w for w in warnings) and snap['data_quality'] != 'UNAVAILABLE':
            snap['data_quality'] = 'LAST_KNOWN'
            snap['warnings'].append('快取快照不是即時行情。')
        snap.update(run_id=run_id, request_started_at=request_started, request_completed_at=request_completed,
                    retrieval_mode=retrieval_mode, evaluated_at=now().isoformat(),
                    timestamp_timezone='Asia/Taipei (UTC+08:00)',
                    exchange_time_meaning='SDK nTradingDay + nDealTime: last trade time, not local request time')
        if snap.get('exchange_time'):
            snap['data_age_seconds'] = round((now() - datetime.fromisoformat(snap['exchange_time'])).total_seconds(), 3)
        save_json(run_dir / 'raw_quote.json', quote)
        save_json(run_dir / 'market_snapshot.json', snap)
        save_json(args.data_dir / 'market_snapshot.json', snap)
        history.save_snapshot(run_id, snap, quote)
        observations = history.recent(args.symbol, now().isoformat())
        if stop.exists():
            result, record = llm.fallback(snap, '使用者已要求停止。', 'STOP_REQUESTED'), {}
        else:
            result, record = analyze(snap, args.held_qty, args.budget_twd, observations=observations,
                                     on_request=lambda body: save_json(run_dir / 'openai_request.json', body))
        save_json(run_dir / 'openai_response.json', record)
        save_json(run_dir / 'decision.json', result)
        history.save_analysis(run_id, record, result)
        manifest = dict(run_id=run_id, run_directory=str(run_dir.resolve()), symbol=args.symbol,
                        request_started_at=request_started, request_completed_at=request_completed,
                        exchange_time=snap.get('exchange_time'), received_at=snap.get('received_at'),
                        llm_request_started_at=record.get('request_started_at'),
                        llm_response_received_at=record.get('response_received_at'),
                        data_quality=snap['data_quality'], real_order_sent=False)
        save_json(run_dir / 'run.json', manifest)
        save_json(args.data_dir / 'latest_run.json', manifest)
        save_json(args.data_dir / 'openai_request.json', record.get('request', {}))
        save_json(args.data_dir / 'openai_response.json', record)
        save_json(args.data_dir / 'decision.json', result)
        display(result, snap.get('name'))
        print('RUN RECORD: ' + str(run_dir.resolve()), flush=True)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('STOPPED\nREAL ORDER SENT = NO')
    except Exception as exc:
        print('ERROR: ' + redact(exc) + '\nREAL ORDER SENT = NO', file=sys.stderr)
        sys.exit(1)
