"""Real quotes + real OpenAI requests + exclusively simulated orders."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
import uuid
from config import ROOT, load_env, now, current_slot, next_slot, symbols, redact, QUOTE_INTERVAL_SECONDS
from ledger import Ledger, dumps
from capital import CapitalMarket
from strategy import run_cycle


@contextmanager
def single_instance(path):
    # OS releases this lock on crash; no persistent lockout state.
    f = path.open('a+b')
    f.seek(0, 2)
    if f.tell() == 0:
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
    except OSError:
        f.close()
        raise RuntimeError('ALREADY_RUNNING') from None
    try:
        yield
    finally:
        f.close()


def write_json(path, payload):
    temp = path.with_suffix('.tmp')
    temp.write_text(dumps(payload), encoding='utf-8')
    temp.replace(path)


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='One real quote/API probe, no simulated fills; also works after close')
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--status', action='store_true', help='Print last cycle and paper portfolio, no network')
    args = parser.parse_args()
    load_env()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    if args.status:
        ledger = Ledger(args.data_dir / 'trading.sqlite3')
        row = ledger.db.execute('SELECT cycle_id,status,summary,api_json FROM strategy_cycles ORDER BY timestamp DESC LIMIT 1').fetchone()
        print(dumps({'portfolio': ledger.portfolio(), 'last_cycle': dict(row) if row else None}))
        ledger.close()
        return
    with single_instance(args.data_dir / 'runtime.lock'):
        stop = args.data_dir / 'stop.signal'
        stop.unlink(missing_ok=True)
        ledger = Ledger(args.data_dir / 'trading.sqlite3')
        market = CapitalMarket(ledger, stopped=stop.exists)
        watch = symbols()
        last_quote, last_cycle, llm_state = None, None, 'NOT_CALLED'
        warnings = []
        def health(state):
            write_json(args.data_dir / 'health.json', dict(
                runtime=state, execution_mode='SIMULATION_ONLY', real_order_send_enabled=False,
                quote_source='CAPITAL_SKCOM', broker=market.status, broker_inventory=market.inventory(),
                llm=llm_state, last_cycle=last_cycle, last_quote=last_quote,
                max_capital=10000, warnings=warnings[-5:], **ledger.portfolio()))
        try:
            ledger.event('BOT_STARTED', {'mode': 'PAPER', 'pid': os.getpid()})
            print('BOT STARTED | SIMULATION ONLY | Capital SKCOM odd-lot quotes + real LLM | virtual capital TWD 10000', flush=True)
            print('NEXT DECISION SLOT (Asia/Taipei): ' + next_slot(now()), flush=True)
            print('INITIAL PARTIAL SLOT: runs once if fresh quotes are available; later slots every 30 minutes', flush=True)
            next_poll = 0.0
            while not stop.exists():
                at = now()
                # Startup probe or market hours only; stale exchange timestamps also handle holidays.
                in_market = at.weekday() < 5 and 9 * 60 <= at.hour * 60 + at.minute < 13 * 60 + 30
                if time.monotonic() >= next_poll and (in_market or args.once or next_poll == 0):
                    warnings.clear()
                    try:
                        quotes, issues = market.fetch(list(dict.fromkeys([*watch, *ledger.portfolio()['positions']])))
                        ledger.store_quotes(quotes)
                        last_quote = max(q['exchange_time'] for q in quotes)
                        warnings.extend(issues)
                        for q in quotes:
                            print(f"QUOTE RECEIVED: {q['symbol']} bid={q['bid']} ask={q['ask']} {q['quality']} exchange_time={q['exchange_time']}", flush=True)
                        if all(q['quality'] == 'STALE' for q in quotes):
                            warnings.append('ALL_QUOTES_STALE')
                    except Exception as exc:
                        warnings.append('QUOTE_FETCH_FAILED')
                        ledger.error('QUOTE_FETCH_FAILED', f'{type(exc).__name__}: {redact(exc)}')
                        print('CAPITAL WARNING: ' + redact(exc) + '; retry interval 60 seconds; no public quote fallback', flush=True)
                    next_poll = time.monotonic() + QUOTE_INTERVAL_SECONDS
                slot = ('PROBE:' + now().isoformat() + ':' + uuid.uuid4().hex[:8]) if args.once else current_slot(now())
                if slot and not stop.exists():
                    result = run_cycle(ledger, slot, watch, probe=args.once, stopped=stop.exists,
                                       broker_inventory=market.inventory())
                    if result:
                        last_cycle = slot
                        llm_state = result.get('error', 'OK')
                        write_json(args.data_dir / 'latest_decision.json', result)
                        p = ledger.portfolio()
                        print(f"CAPITAL USED: {p['used_capital']} + reserved {p['reserved_capital']} / 10000", flush=True)
                        print('NEXT DECISION SLOT (Asia/Taipei): ' + next_slot(now()), flush=True)
                health('RUNNING')
                if args.once:
                    break
                for _ in range(10):
                    if stop.exists():
                        break
                    if market.client:
                        market.pump(.1)
                    else:
                        time.sleep(.1)
        except KeyboardInterrupt:
            print('STOP REQUESTED', flush=True)
        finally:
            ledger.event('BOT_STOPPED', {'mode': 'PAPER'})
            health('STOPPED')
            market.close()
            ledger.close()
            print('BOT STOPPED | SQLite flushed', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'BOT START ERROR: {type(exc).__name__}: {redact(exc)}', file=sys.stderr)
        sys.exit(1)
