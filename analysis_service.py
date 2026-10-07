"""Background SKCOM -> ranking/position union -> OpenAI -> immutable research run."""
import json
from contextlib import closing
from pathlib import Path
import threading
import uuid
from config import ROOT, load_env, now, redact
from main import Audit, save_json, single_instance
from universe import load_universe, load_rules
from capital_multi_quote import MultiCapitalMarket, multi_snapshot
from quant_scanner import scan_and_rank, scan_quote
from multi_scan import candidate_payload
import position_store
import position_analyzer
import scan_store


def build_union(top, ranked, quotes, positions, db_path, at, scanned=None):
    qmap = {q['symbol']: q for q in quotes}
    pmap = {p['symbol']: p for p in positions}
    rmap = {r['symbol']: r for r in ranked}
    metrics_map = {r['symbol']:r for r in (scanned or [])}
    top_symbols = {r['symbol'] for r in top}
    managed = {p['symbol'] for p in positions if p['ai_managed_qty'] > 0}
    symbols = [r['symbol'] for r in top] + sorted(managed-top_symbols)
    result = []
    for s in symbols:
        q, r = qmap[s], rmap.get(s)
        if r:
            c = candidate_payload([r], quotes, db_path, at)[0]
        else:
            metric = metrics_map.get(s, {})
            c = dict(symbol=s, quant_rank=None, quant_score=metric.get('quant_score'),
                     components=metric.get('components',{}), metrics=metric.get('metrics',{}),
                     **{k:q.get(k) for k in ('last_price', 'open', 'high', 'low', 'bid', 'ask', 'volume', 'quote_status', 'quote_timestamp')},
                     volume_unit='shares', warnings=q.get('warnings', []),
                     market_history=scan_store.recent_history(db_path, s, at))
        c.pop('position_qty', None)
        c['is_trial'] = bool(q.get('is_trial'))
        c['analysis_source'] = 'TOP10+POSITION' if s in top_symbols & managed else ('TOP10' if s in top_symbols else 'POSITION')
        c['position'] = position_store.context(pmap.get(s, position_store.empty(s)), q['last_price'])
        result.append(c)
    return result


class AnalysisService:
    def __init__(self, directory=ROOT/'data'/'analysis'):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db_path = self.directory/'market_history.sqlite3'
        self.stop = threading.Event()

    def run(self, trigger, phase=lambda value: None):
        if trigger not in ('AUTO', 'MANUAL'):
            raise ValueError('INVALID_TRIGGER')
        load_env()
        run_id = now().strftime('%Y%m%dT%H%M%S')+'-gui-'+uuid.uuid4().hex[:8]
        directory = self.directory/'runs'/run_id
        directory.mkdir(parents=True)
        manifest = dict(run_id=run_id, trigger_type=trigger, started_at=now().isoformat(),
                        run_directory=str(directory.resolve()), real_order_sent=False)
        save_json(directory/'run.json', manifest)
        try:
            with single_instance(self.directory/'runtime.lock'):
                return self._run(directory, manifest, phase)
        except Exception as exc:
            manifest.update(finished_at=now().isoformat(), status='ERROR', error=redact(f'{type(exc).__name__}: {exc}'))
            save_json(directory/'run.json', manifest)
            save_json(directory/'error.json', manifest)
            try:
                import sqlite3
                with closing(sqlite3.connect(self.db_path, timeout=10)) as db:
                    scan_store.initialize(db)
                    db.execute('INSERT OR REPLACE INTO scan_runs VALUES(?,?)', (run_id, scan_store.encode(manifest)))
                    db.commit()
            except Exception as storage_error:
                save_json(directory/'storage_error.json', {'error':redact(storage_error)})
            raise

    def _run(self, directory, manifest, phase):
        universe = load_universe(ROOT/'config'/'universe_tw.json')
        rules = load_rules(ROOT/'config'/'scan_rules.json')
        # The GUI contract is Top 10, independent of a one-shot CLI override.
        rules['candidate_top_n'] = 10
        positions = position_store.load(self.db_path)
        symbols = list(dict.fromkeys(universe['symbols'] + [p['symbol'] for p in positions if p['user_qty']+p['ai_managed_qty'] > 0]))
        save_json(directory/'universe_snapshot.json', universe)
        save_json(directory/'positions_snapshot.json', positions)
        audit = Audit(directory)
        phase('SCANNING_QUOTES')
        started, raw, errors = now().isoformat(), {}, {}
        # Initialize and destroy every COM object on this same worker thread.
        import comtypes
        comtypes.CoInitialize()
        market = MultiCapitalMarket(audit, stopped=self.stop.is_set)
        try:
            try:
                raw, errors = market.fetch_batch(symbols, 12)
            except Exception as exc:
                errors = {s:redact(f'{type(exc).__name__}: {exc}') for s in symbols}
                audit.event('BATCH_ERROR', {'detail':redact(exc)})
        finally:
            try:
                market.close()
            finally:
                comtypes.CoUninitialize()
        completed = now().isoformat()
        batch = multi_snapshot(symbols, raw, errors, started, completed)
        batch['run_id'] = manifest['run_id']
        save_json(directory/'raw_quotes.json', raw)
        save_json(directory/'multi_market_snapshot.json', batch)
        phase('QUANT_ANALYSIS')
        at = now()
        scanned, ranked, top = scan_and_rank([q for q in batch['quotes'] if q['symbol'] in universe['symbols']], rules, at)
        # Position-only stocks outside the universe still receive available
        # metrics, but cannot displace the universe's Top 10 ranking.
        scanned += [scan_quote(q, rules, at) for q in batch['quotes'] if q['symbol'] not in universe['symbols']]
        save_json(directory/'quant_scan.json', dict(rule_config=rules, quotes=scanned))
        save_json(directory/'candidate_ranking.json', dict(ranking=ranked, top_candidates=top))
        candidates = build_union(top, ranked, batch['quotes'], positions, self.db_path, started, scanned)
        save_json(directory/'analysis_set.json', candidates)
        request = position_analyzer.build_request(candidates)
        save_json(directory/'openai_request.json', request)
        phase('WAITING_OPENAI')
        if candidates and not self.stop.is_set():
            decision, response = position_analyzer.analyze(candidates, request)
        else:
            decision = dict(market_view='沒有可分析集合，或已要求停止。', decisions=[])
            response = dict(validation='NOT_CALLED', api={}, raw_response='')
        phase('PROCESSING_RESULT')
        save_json(directory/'openai_response.json', response)
        save_json(directory/'selection_decision.json', decision)
        price_map = {q['symbol']:q['last_price'] for q in batch['quotes']}
        contexts = [position_store.context(p, price_map.get(p['symbol'])) for p in positions]
        failures = list(dict.fromkeys(errors.values()))
        if response['validation'] == 'ERROR':
            failures.append(str(response['api'].get('error')))
        if not batch['symbols_received']:
            failures.append('NO_VALID_MARKET_DATA')
        manifest.update(finished_at=now().isoformat(), request_started_at=started, request_completed_at=completed,
                        universe_count=len(universe['symbols']), quote_symbols_count=len(symbols),
                        symbols_received=batch['symbols_received'], ai_validation=response['validation'],
                        status='ERROR' if failures else 'COMPLETE', error='; '.join(failures),
                        candidates_sent=len(candidates) if response['validation']!='NOT_CALLED' else 0)
        result = dict(manifest=manifest, batch=batch, top=top, analysis_set=candidates, positions=contexts, decision=decision)
        save_json(directory/'dashboard_result.json', result)
        db_result = scan_store.persist(self.db_path, manifest, batch, raw, scanned, ranked, request, response, decision)
        save_json(directory/'sqlite_result.json', db_result)
        save_json(directory/'run.json', manifest)
        save_json(self.directory/'latest_dashboard.json', result)
        return result

    def refresh(self):
        path = self.directory/'latest_dashboard.json'
        result = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        positions = position_store.load(self.db_path)
        quotes = {q['symbol']:q['last_price'] for q in result['batch']['quotes']} if result else {}
        return result, [position_store.context(p, quotes.get(p['symbol'])) for p in positions]
