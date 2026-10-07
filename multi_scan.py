"""Fixed universe -> SKCOM -> deterministic ranking -> one OpenAI comparison -> STOP."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import uuid
from config import ROOT, load_env, now, redact
from main import Audit, save_json, single_instance
from universe import load_universe, load_rules
from capital_multi_quote import MultiCapitalMarket, multi_snapshot
from quant_scanner import scan_and_rank
import candidate_analyzer
import scan_store


def candidate_payload(top, quotes, db_path, at, etf_symbols=()):
    lookup = {q['symbol']: q for q in quotes}
    etf_symbols = set(etf_symbols)
    result = []
    for r in top:
        q = lookup[r['symbol']]
        result.append(dict(symbol=q['symbol'], name=q.get('name'), asset_type='ETF' if q['symbol'] in etf_symbols else 'STOCK',
            quant_rank=r['rank'], quant_score=r['quant_score'],
            components=r['components'], metrics=r['metrics'],
            last_price=q['last_price'], open=q['open'], high=q['high'], low=q['low'],
            bid=q['bid'], ask=q['ask'], volume=q['volume'], volume_unit='shares',
            quote_status=q['quote_status'], quote_timestamp=q['quote_timestamp'],
            position_qty=None, market_history=scan_store.recent_history(db_path, q['symbol'], at),
            warnings=r['warnings']))
    return result


def display(batch, top, selection, response):
    print('\n========================================\nMARKET SCAN\n========================================')
    print(f"Universe       : {batch['symbols_requested']}\nQuotes Received: {batch['symbols_received']}\nUnavailable    : {batch['symbols_unavailable']}")
    print('\nTOP QUANT CANDIDATES\n========================================')
    for q in top:
        components = ' '.join(f'{k}={v:+.3f}' for k,v in q['components'].items())
        print(f"{q['rank']}. {q['symbol']} Score {q['quant_score']:.4f} [{q['quote_status']}] {components}")
    print('\nOPENAI SELECTION\n========================================')
    print(selection['market_view'])
    for q in selection['selected']:
        print(f"{q['rank']}. {q.get('name') or '名稱未取得'} ({q['symbol']})\n   Decision   : {q['decision']}\n   Confidence : {q['confidence']:.0%}\n   Price      : {q['reference_price']}\n   Reason     : {q['reason']}")
    print(f"\nFINAL\n========================================\nSelected Symbols : {len(selection['selected'])}\nAI Validation    : {response['validation']}\nReal Order Sent  : NO\n========================================", flush=True)


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--universe', type=Path, default=ROOT/'config'/'universe_tw.json')
    ap.add_argument('--rules', type=Path, default=ROOT/'config'/'scan_rules.json')
    ap.add_argument('--quote-timeout', type=float, default=12)
    ap.add_argument('--data-dir', type=Path, default=ROOT/'data'/'analysis')
    args = ap.parse_args()
    if not 1 <= args.quote_timeout <= 30:
        ap.error('quote-timeout must be 1..30 seconds')
    load_env()
    universe, rules = load_universe(args.universe), load_rules(args.rules)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    with single_instance(args.data_dir/'runtime.lock'):
        run_id = now().strftime('%Y%m%dT%H%M%S') + '-scan-' + uuid.uuid4().hex[:8]
        directory = args.data_dir/'runs'/run_id
        directory.mkdir(parents=True)
        stop = args.data_dir/'stop.signal'
        stop.unlink(missing_ok=True)
        universe_record = dict(**universe, requested_at=now().isoformat(),
                              config_path=str(args.universe.resolve()),
                              sha256=hashlib.sha256(args.universe.read_bytes()).hexdigest())
        save_json(directory/'universe_snapshot.json', universe_record)
        audit = Audit(directory)
        market = MultiCapitalMarket(audit, stopped=stop.exists)
        started, raw, errors = now().isoformat(), {}, {}
        symbols = universe['symbols']
        print(f'SKCOM MULTI SCAN | universe={len(symbols)} | one login | no orders', flush=True)
        try:
            raw, errors = market.fetch_batch(symbols, args.quote_timeout)
        except Exception as exc:
            detail = redact(f'{type(exc).__name__}: {exc}')
            errors = {s: detail for s in symbols}
            audit.event('BATCH_ERROR', {'detail': detail})
        finally:
            completed = now().isoformat()
            market.close()
        batch = multi_snapshot(symbols, raw, errors, started, completed)
        batch['run_id'] = run_id
        save_json(directory/'raw_quotes.json', raw)
        save_json(directory/'multi_market_snapshot.json', batch)
        scanned, ranked, top = scan_and_rank(batch['quotes'], rules, now())
        quant = dict(rule_config=rules, quotes=scanned)
        ranking = dict(top_n=rules['candidate_top_n'], eligible_count=len(ranked), ranking=ranked,
                       top_candidates=top, excluded=[{'symbol':r['symbol'], 'reasons':r['exclusion_reasons']} for r in scanned if not r['eligible']])
        save_json(directory/'quant_scan.json', quant)
        save_json(directory/'candidate_ranking.json', ranking)
        db_path = args.data_dir/'market_history.sqlite3'
        candidates = candidate_payload(top, batch['quotes'], db_path, started, universe.get('etf_symbols', []))
        request = candidate_analyzer.build_request(candidates)
        save_json(directory/'openai_request.json', request)
        if candidates and not stop.exists():
            selection, response = candidate_analyzer.analyze(candidates, request)
        else:
            why = 'STOP_REQUESTED' if stop.exists() else ('NO_VALID_MARKET_DATA' if not batch['symbols_received'] else 'NO_ELIGIBLE_CANDIDATES')
            selection = dict(market_view=why, selected=[], rejected_candidates=[], data_quality=candidate_analyzer.overall_quality(candidates))
            response = dict(validation='NOT_CALLED', raw_response='', api={'reason': why})
        save_json(directory/'openai_response.json', response)
        save_json(directory/'selection_decision.json', selection)
        manifest = dict(run_id=run_id, run_directory=str(directory.resolve()),
                        request_started_at=started, request_completed_at=completed,
                        completed_at=now().isoformat(), universe_count=len(symbols),
                        symbols_received=batch['symbols_received'], unavailable=[q['symbol'] for q in batch['quotes'] if q['quote_status']=='UNAVAILABLE'],
                        candidates_sent=len(candidates) if response['validation']!='NOT_CALLED' else 0,
                        ai_validation=response['validation'], selected_count=len(selection['selected']),
                        login_attempts=sum(e['kind']=='CAPITAL_LOGIN' for e in audit.events),
                        real_order_sent=False)
        db_result = scan_store.persist(db_path, manifest, batch, raw, scanned, ranked, request, response, selection)
        save_json(directory/'sqlite_result.json', db_result)
        save_json(directory/'run.json', manifest)
        for name, value in [('universe_snapshot', universe_record), ('multi_market_snapshot', batch),
                            ('quant_scan', quant), ('candidate_ranking', ranking), ('selection_decision', selection)]:
            save_json(args.data_dir/(name+'.json'), value)
        save_json(args.data_dir/'latest_scan.json', manifest)
        display(batch, top, selection, response)
        print('RUN RECORD: ' + str(directory.resolve()), flush=True)
        return 0 if response['validation']=='VALID' else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('STOPPED\nREAL ORDER SENT = NO')
    except Exception as exc:
        print('SCAN ERROR: ' + redact(exc) + '\nREAL ORDER SENT = NO', file=sys.stderr)
        raise SystemExit(1)
