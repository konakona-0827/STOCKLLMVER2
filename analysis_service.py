"""Background SKCOM -> ranking/position union -> OpenAI -> immutable research run."""
import json
import sqlite3
from decimal import Decimal
from contextlib import closing
from pathlib import Path
import threading
import uuid
from config import ROOT, load_env, now, redact
from main import Audit, save_json, single_instance
from universe import load_universe, load_rules
from capital_multi_quote import multi_snapshot
from quant_scanner import scan_and_rank, scan_quote
from multi_scan import candidate_payload
import position_store
import position_analyzer
import scan_store
from advice_interface import write_advice_interface
import paper_portfolio
from scheduler import is_twse_oddlot_market_window


def should_call_openai(trigger, candidates, stopped, wall):
    if stopped or not candidates:
        return False
    if trigger == 'MANUAL':
        return True
    return is_twse_oddlot_market_window(wall) and any(
        c.get('quote_status') == 'LIVE' and not c.get('is_trial')
        for c in candidates
    )


def live_buy_budget(broker_health, daily_reserved, strategy_cap, daily_limit):
    """Return a fail-closed current BUY budget and its auditable components."""
    from execution.health_monitor import current_buy_sources_verified
    sources_verified = current_buy_sources_verified(broker_health)
    strategy_cap = Decimal(str(strategy_cap))
    minimum = Decimal(str(broker_health.get('minimum_balance_twd') or 0))
    balance = broker_health.get('broker_balance_twd')
    pending_raw = broker_health.get('pending_buy_reserve_twd')
    invested_raw = broker_health.get('ai_committed_capital_twd')
    if None in (balance, pending_raw, invested_raw):
        sources_verified = False
    pending = Decimal(str(pending_raw)) if pending_raw is not None else Decimal('0')
    invested = Decimal(str(invested_raw)) if invested_raw is not None else Decimal('0')
    broker_after_minimum = (max(Decimal('0'), Decimal(str(balance))-minimum)
                            if balance is not None else Decimal('0'))
    broker_remaining = max(Decimal('0'), broker_after_minimum-pending)
    strategy_remaining = max(Decimal('0'), strategy_cap-invested-pending)
    daily_limit = Decimal(str(daily_limit))
    daily_remaining = (max(Decimal('0'), daily_limit-Decimal(str(daily_reserved)))
                       if daily_reserved is not None else Decimal('0'))
    ready = sources_verified and daily_reserved is not None
    available = min(broker_remaining, strategy_remaining, daily_remaining) if ready else Decimal('0')
    if available <= 0:
        ready = False
    return dict(available=available,
                broker_available=Decimal(str(balance)) if balance is not None else None,
                broker_after_minimum=broker_after_minimum,
                broker_remaining=broker_remaining, pending_reserve=pending,
                ai_committed=invested, strategy_remaining=strategy_remaining,
                minimum_reserve=minimum, strategy_cap=strategy_cap,
                daily_limit=daily_limit,
                daily_reserved=None if daily_reserved is None else Decimal(str(daily_reserved)),
                daily_remaining=daily_remaining, status='READY' if ready else 'BLOCKED')


def build_union(top, ranked, quotes, positions, db_path, at, scanned=None, etf_symbols=(), paper_positions=(), live_managed=False):
    qmap = {q['symbol']: q for q in quotes}
    pmap = {p['symbol']: p for p in positions}
    rmap = {r['symbol']: r for r in ranked}
    metrics_map = {r['symbol']:r for r in (scanned or [])}
    etf_symbols = set(etf_symbols)
    paper_map = {p['symbol']: p for p in paper_positions}
    top_symbols = {r['symbol'] for r in top}
    managed = ({p['symbol'] for p in paper_positions if p['qty'] > 0} if live_managed
               else {p['symbol'] for p in positions if p['ai_managed_qty'] > 0})
    symbols = list(dict.fromkeys([r['symbol'] for r in top] + sorted(managed-top_symbols) +
                                 sorted(set(paper_map)-top_symbols)))
    result = []
    for s in symbols:
        q, r = qmap[s], rmap.get(s)
        if r:
            c = candidate_payload([r], quotes, db_path, at, etf_symbols)[0]
        else:
            metric = metrics_map.get(s, {})
            c = dict(symbol=s, name=q.get('name'), quant_rank=None, quant_score=metric.get('quant_score'),
                     asset_type='ETF' if s in etf_symbols else 'STOCK',
                     components=metric.get('components',{}), metrics=metric.get('metrics',{}),
                     **{k:q.get(k) for k in ('last_price', 'open', 'high', 'low', 'bid', 'ask', 'volume', 'quote_status', 'quote_timestamp')},
                     volume_unit='shares', warnings=q.get('warnings', []),
                     market_history=scan_store.recent_history(db_path, s, at))
        c.pop('position_qty', None)
        c['is_trial'] = bool(q.get('is_trial'))
        c['analysis_source'] = 'TOP10+POSITION' if s in top_symbols & managed else ('TOP10' if s in top_symbols else 'POSITION')
        configured = dict(pmap.get(s, position_store.empty(s)))
        if live_managed:
            configured['ai_managed_qty'] = paper_map.get(s, {}).get('qty', 0)
            configured['ai_average_cost'] = None  # The execution ledger has shares, not verified cost basis.
        c['position'] = position_store.context(configured, q['last_price'])
        if live_managed:
            c['position']['source'] = 'LIVE_AI_LEDGER_PLUS_USER_CONFIGURATION'
        c['paper_position'] = paper_map.get(s, dict(symbol=s, qty=0, cost_cents=0))
        if live_managed:
            c['paper_position']['source'] = 'LIVE_AI_LEDGER_COMPATIBILITY_FIELD'
        result.append(c)
    return result


def live_ai_positions(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError('正式交易資料庫不存在，停止分析')
    with sqlite3.connect('file:' + str(path.resolve()) + '?mode=ro', uri=True) as db:
        return [dict(symbol=symbol, qty=qty, cost_cents=None)
                for symbol, qty in db.execute('SELECT symbol,qty FROM ai_positions WHERE qty>0')]


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
        paper_state = paper_portfolio.snapshot(self.db_path)
        if paper_state is None:
            raise ValueError('請先在主畫面設定資金總上限，再開始分析')
        from execution.auto_advice_executor import LiveExecutionConfig
        live_config = LiveExecutionConfig.load(ROOT/'config'/'execution_live.json')
        live_enabled = (self.directory.resolve() == (ROOT/'data'/'analysis').resolve()
                        and live_config.enabled)
        broker_health = None
        if live_enabled:
            from execution.health_monitor import sync_broker_state
            broker_health, _ = sync_broker_state(ROOT, return_report=True)
        paper_positions = (live_ai_positions(ROOT/'data'/'execution'/'live_execution.sqlite3')
                           if live_enabled else paper_portfolio.positions(self.db_path))
        symbols = list(dict.fromkeys(universe['symbols'] +
                     # Manual holdings still need quote snapshots for current
                     # value display and inventory context, but are not added
                     # to the AI-managed candidate set by build_union().
                     [p['symbol'] for p in positions] +
                     [p['symbol'] for p in paper_positions if p['qty'] > 0]))
        save_json(directory/'paper_account_snapshot.json', paper_state)
        save_json(directory/'universe_snapshot.json', universe)
        save_json(directory/'positions_snapshot.json', positions)
        audit = Audit(directory)
        phase('SCANNING_QUOTES')
        started, raw, errors = now().isoformat(), {}, {}
        try:
            # Quotes, health checks and execution share the broker COM thread.
            # Its connected session is checked and reused before opening a
            # quote monitor, so scans never issue a second Center login.
            from execution.broker_runtime import get_broker_runtime
            raw, errors = get_broker_runtime(ROOT).fetch_quotes(
                symbols, audit, self.stop.is_set, timeout=12,
            )
        except Exception as exc:
            errors = {s:redact(f'{type(exc).__name__}: {exc}') for s in symbols}
            audit.event('BATCH_ERROR', {'detail':redact(exc)})
        completed = now().isoformat()
        batch = multi_snapshot(symbols, raw, errors, started, completed)
        batch['run_id'] = manifest['run_id']
        if live_enabled:
            from execution.execution_store import ExecutionStore
            daily_reserved = None
            try:
                daily_reserved = ExecutionStore(
                    ROOT/'data'/'execution'/'live_execution.sqlite3', must_exist=True
                ).daily_reserved_buy_twd()
            except Exception as exc:
                audit.event('BUY_BUDGET_UNAVAILABLE', {'reason': redact(exc)})
            budget = live_buy_budget(broker_health, daily_reserved,
                                     paper_state['capital_limit_twd'],
                                     live_config.max_daily_buy_twd)
            available = budget['available']
            paper_state = {**paper_state, 'available_cash_twd':float(available),
                           'available_cash_cents':int(available*100),
                           'positions':paper_positions,
                           'source':'FRESH_BROKER_BUYING_POWER_AFTER_RESERVE_STRATEGY_CAP_AND_DAILY_RESERVATIONS',
                           'broker_buying_power_twd':(float(budget['broker_available'])
                                                       if budget['broker_available'] is not None else None),
                           'broker_after_minimum_twd':float(budget['broker_after_minimum']),
                           'broker_after_pending_twd':float(budget['broker_remaining']),
                           'pending_buy_reserve_twd':float(budget['pending_reserve']),
                           'ai_committed_capital_twd':float(budget['ai_committed']),
                           'strategy_remaining_twd':float(budget['strategy_remaining']),
                           'minimum_balance_reserve_twd':float(budget['minimum_reserve']),
                           'strategy_cap_twd':float(budget['strategy_cap']),
                           'daily_buy_limit_twd':float(budget['daily_limit']),
                           'daily_buy_reserved_twd':None if budget['daily_reserved'] is None else float(budget['daily_reserved']),
                           'daily_buy_remaining_twd':float(budget['daily_remaining']),
                           'budget_status':budget['status']}
            save_json(directory/'paper_account_snapshot.json', paper_state)
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
        candidates = build_union(top, ranked, batch['quotes'], positions, self.db_path, started, scanned,
                                 universe.get('etf_symbols', []), paper_positions, live_managed=live_enabled)
        for c in candidates:
            if c['last_price'] is not None and c['quote_status'] == 'LIVE' and not c.get('is_trial'):
                entry_price = ((c.get('ask') or c['last_price'])
                               if live_enabled else c['last_price'])
                c['budget_price'] = entry_price
                c['max_buy_qty'] = paper_portfolio.max_affordable_qty(
                    paper_state['available_cash_cents'], entry_price)
                if live_enabled:
                    from paper_portfolio import price_cents
                    while c['max_buy_qty'] and (
                        Decimal(price_cents(entry_price)*c['max_buy_qty']) / 100
                        + max(live_config.min_buy_fee_reserve_twd,
                              Decimal(price_cents(entry_price)*c['max_buy_qty']) / 100
                              * live_config.buy_cash_buffer_rate)
                        > Decimal(str(paper_state['available_cash_twd']))
                        or (live_config.max_order_twd is not None and
                            Decimal(price_cents(entry_price)*c['max_buy_qty']) / 100
                            > live_config.max_order_twd)):
                        c['max_buy_qty']-=1
            else:
                c['max_buy_qty'] = 0
        save_json(directory/'analysis_set.json', candidates)
        request = position_analyzer.build_request(candidates, paper_state)
        save_json(directory/'openai_request.json', request)
        phase('WAITING_OPENAI')
        budget_ready = not live_enabled or paper_state.get('budget_status') == 'READY'
        if budget_ready and should_call_openai(trigger=manifest['trigger_type'], candidates=candidates,
                              stopped=self.stop.is_set(), wall=now().timestamp()):
            decision, response = position_analyzer.analyze(candidates, request)
        else:
            reason = ('券商可買額、未結委託保留或 AI 累計投入額不足以支持新買單，本輪已停止 AI 決策與委託。'
                      if not budget_ready else
                      'AUTO 等待盤中零股交易時段及有效即時行情。'
                      if manifest['trigger_type'] == 'AUTO' and not self.stop.is_set()
                      else '沒有可分析集合，或已要求停止。')
            decision = dict(market_view=reason, decisions=[])
            response = dict(validation='NOT_CALLED', api={
                'error':'BUY_BUDGET_CHECK_FAILED' if not budget_ready else None,
            }, raw_response='')
        phase('PROCESSING_RESULT')
        if response['validation'] == 'VALID' and not self.stop.is_set() and not live_enabled:
            simulation = paper_portfolio.apply_decisions(
                self.db_path, manifest['run_id'], decision['decisions'], candidates)
        else:
            simulation = dict(account=paper_portfolio.account(self.db_path),
                              available_cash_twd=paper_state['available_cash_twd'],
                              capital_limit_twd=paper_state['capital_limit_twd'],
                              positions=paper_positions, trades=[],
                              execution_mode='LIVE_NO_PAPER_FILL' if live_enabled else 'PAPER_NO_FILL')
        save_json(directory/'paper_simulation.json', simulation)
        save_json(directory/'openai_response.json', response)
        save_json(directory/'selection_decision.json', decision)
        price_map = {q['symbol']:q['last_price'] for q in batch['quotes']}
        contexts = [position_store.context(p, price_map.get(p['symbol'])) for p in positions]
        failures = list(dict.fromkeys(errors.values()))
        if response['validation'] == 'ERROR':
            failures.append(str(response['api'].get('error')))
        if not budget_ready:
            failures.append('BUY_BUDGET_CHECK_FAILED')
        if not batch['symbols_received']:
            failures.append('NO_VALID_MARKET_DATA')
        manifest.update(finished_at=now().isoformat(), request_started_at=started, request_completed_at=completed,
                        universe_count=len(universe['symbols']), quote_symbols_count=len(symbols),
                        symbols_received=batch['symbols_received'], ai_validation=response['validation'],
                        llm_model=request.get('model'),
                        llm_request_started_at=response.get('request_started_at'),
                        llm_response_received_at=response.get('response_received_at'),
                        status='ERROR' if failures else 'COMPLETE', error='; '.join(failures),
                        candidates_sent=len(candidates) if response['validation']!='NOT_CALLED' else 0)
        result = dict(manifest=manifest, batch=batch, top=top, analysis_set=candidates, positions=contexts,
                      decision=decision, paper_simulation=simulation,
                      paper_trade_history=paper_portfolio.trades(self.db_path))
        if live_enabled:
            result['execution_budget'] = {k:paper_state.get(k) for k in (
                'available_cash_twd','broker_buying_power_twd','minimum_balance_reserve_twd',
                'strategy_cap_twd','strategy_remaining_twd',
                'ai_committed_capital_twd','pending_buy_reserve_twd',
                'broker_after_minimum_twd','broker_after_pending_twd',
                'daily_buy_limit_twd','daily_buy_reserved_twd',
                'daily_buy_remaining_twd','budget_status')}
            result['execution_budget'].update(
                min_buy_fee_reserve_twd=float(live_config.min_buy_fee_reserve_twd),
                buy_cash_buffer_rate=float(live_config.buy_cash_buffer_rate))
        if broker_health is not None:
            result['broker_health'] = broker_health
        save_json(directory/'dashboard_result.json', result)
        db_result = scan_store.persist(self.db_path, manifest, batch, raw, scanned, ranked, request, response, decision)
        save_json(directory/'sqlite_result.json', db_result)
        save_json(directory/'run.json', manifest)
        save_json(self.directory/'latest_dashboard.json', result)
        advice_path = directory/'advice_interface.json'
        write_advice_interface(result, advice_path)
        write_advice_interface(result, self.directory/'latest_advice.json')

        execution_report = self._execute_after_advice_written(
            manifest.get('trigger_type'), result, directory, advice_path,
        )
        if execution_report is not None:
            result['execution_report'] = execution_report
            manifest['real_order_sent'] = any(x.get('broker_order_sent') is True
                                              for x in execution_report.get('results', []))
            save_json(directory/'run.json', manifest)
            save_json(directory/'dashboard_result.json', result)
            save_json(self.directory/'latest_dashboard.json', result)
            # The latest handoff is a display artifact after this execution
            # attempt; a separate CLI must not replay it as a fresh order.
            latest_advice = self.directory/'latest_advice.json'
            recorded = json.loads(latest_advice.read_text(encoding='utf-8'))
            recorded['run_status'] = 'HANDLED_BY_DASHBOARD'
            recorded['run_error'] = 'Execution report: ' + str(execution_report.get('status'))
            save_json(latest_advice, recorded)
            # Re-read broker inventory after the executor has observed the
            # order, so the app shows confirmed holdings and unresolved work.
            try:
                from execution.health_monitor import sync_broker_state
                result['broker_snapshot'] = sync_broker_state(ROOT)
            except Exception as exc:
                result['broker_refresh_error'] = redact(f'{type(exc).__name__}: {exc}')
            save_json(directory/'dashboard_result.json', result)
            save_json(self.directory/'latest_dashboard.json', result)
        return result

    def _execute_after_advice_written(self, trigger, result, directory, advice_path):
        manifest = result.get('manifest') or {}
        if (trigger not in ('AUTO','MANUAL') or manifest.get('status') != 'COMPLETE'
                or manifest.get('ai_validation') != 'VALID'):
            return None

        from execution.broker_runtime import get_broker_runtime

        try:
            from execution.health_monitor import sync_broker_state
            fresh = sync_broker_state(ROOT)
            payload = json.loads(advice_path.read_text(encoding='utf-8'))
            budget = payload.get('execution_budget') or {}
            original = Decimal(str(budget.get('available_cash_twd') or 0))
            current = Decimal(str(fresh.get('current_buy_budget_twd') or 0))
            budget['available_cash_twd'] = float(min(original, current))
            budget['pre_execution_broker_checked_at'] = fresh.get('checked_at')
            budget['pre_execution_buy_block_reasons'] = fresh.get('buy_block_reasons')
            payload['execution_budget'] = budget
            save_json(advice_path, payload)
            if result.get('execution_budget') is not None:
                result['execution_budget'].update(budget)
            # The same COM-owning broker thread handles every AUTO cycle and
            # the broker reconciliation; no duplicate Capital login.
            report = get_broker_runtime(ROOT).execute(advice_path)
        except Exception as exc:
            report = {
                'status': 'ERROR',
                'advice_path': str(advice_path),
                'reason': redact(f'{type(exc).__name__}: {exc}'),
                'automatic_retry': False,
            }
            latest_execution = self.directory.parent/'execution'/'latest_live_execution.json'
            save_json(latest_execution, report)
        save_json(directory/'execution_report.json', report)
        return report

    def refresh(self, sync_broker=False):
        from execution.health_monitor import latest_refresh_snapshot
        broker_snapshot = latest_refresh_snapshot(ROOT)
        if sync_broker:
            # The broker's COM-owning thread also refreshes held-stock quotes.
            from execution.health_monitor import sync_broker_state
            def add_position_quotes(health, runtime):
                symbols = [row['symbol'] for row in health.get('inventory_comparison', [])
                           if int(row.get('broker_cash_qty') or 0) > 0]
                if symbols and health.get('inventory_checked'):
                    started = now().isoformat()
                    try:
                        raw, errors = runtime.fetch_quotes(
                            symbols, Audit(self.directory/'broker_refresh'),
                            self.stop.is_set, timeout=12)
                        quote_batch = multi_snapshot(
                            symbols, raw, errors, started, now().isoformat())
                        health['position_quotes'] = quote_batch['quotes']
                        health['position_quote_errors'] = errors
                        health['position_quotes_checked_at'] = quote_batch['request_completed_at']
                    except Exception as exc:
                        health['position_quotes'] = []
                        health['position_quote_errors'] = {
                            'batch':redact(f'{type(exc).__name__}: {exc}')}
            broker_snapshot = sync_broker_state(ROOT, enrich=add_position_quotes)
        path = self.directory/'latest_dashboard.json'
        result = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        positions = position_store.load(self.db_path)
        if result is not None:
            result['paper_simulation'] = paper_portfolio.snapshot(self.db_path)
            result['paper_trade_history'] = paper_portfolio.trades(self.db_path)
        quotes = {q['symbol']:q['last_price'] for q in result['batch']['quotes']} if result else {}
        return result, [position_store.context(p, quotes.get(p['symbol'])) for p in positions], broker_snapshot
