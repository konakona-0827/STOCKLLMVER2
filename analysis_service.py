"""Background SKCOM -> ranking/position union -> OpenAI -> immutable research run."""
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from contextlib import closing
from pathlib import Path
import threading
import uuid
from config import ROOT, TAIPEI, load_env, now, redact, trading_mode
from main import Audit, save_json, single_instance
from universe import load_universe, load_rules
from capital_multi_quote import multi_snapshot
from quant_scanner import scan_and_rank, scan_quote
from multi_scan import candidate_payload
import position_store
import position_analyzer
import scan_store
import daily_history
from advice_interface import write_advice_interface
import paper_portfolio
import strategy_catalog
import strategy_evidence
from scheduler import is_twse_oddlot_market_window
from market_calendar import twse_closed_reason


def should_call_openai(trigger, candidates, stopped, wall):
    if stopped or not candidates:
        return False
    has_live_candidate = any(
        c.get('quote_status') == 'LIVE' and not c.get('is_trial')
        for c in candidates
    )
    if trigger == 'MANUAL':
        return (twse_closed_reason(datetime.fromtimestamp(wall, TAIPEI).date()) is None
                and has_live_candidate)
    return is_twse_oddlot_market_window(wall) and has_live_candidate


def live_buy_budget(broker_health, daily_reserved, strategy_cap, daily_limit):
    """Return a fail-closed current BUY budget and its auditable components."""
    from execution.health_monitor import current_buy_sources_verified
    sources_verified = current_buy_sources_verified(broker_health)
    strategy_cap = Decimal(str(strategy_cap))
    minimum = Decimal(str(broker_health.get('minimum_balance_twd') or 0))
    balance = broker_health.get('broker_balance_twd')
    account_balance = broker_health.get('broker_one_account_balance_twd')
    if account_balance is None:
        account_balance = balance
    if account_balance is None or Decimal(str(account_balance)) < strategy_cap:
        sources_verified = False
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


def build_union(top, ranked, quotes, positions, db_path, at, scanned=None, etf_symbols=(), paper_positions=(), live_managed=False, ai_settings=None):
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
        if live_managed and not (ai_settings or {}).get(s, {}).get('enabled', True):
            continue
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
            evidence = strategy_evidence.assess(q, c['market_history'])
            c['strategy_evidence'] = evidence
            c['eligible_strategies'] = evidence['eligible_strategies']
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
    with closing(sqlite3.connect('file:' + str(path.resolve()) + '?mode=ro', uri=True)) as db:
        return [dict(symbol=symbol, qty=qty, cost_cents=None)
                for symbol, qty in db.execute('SELECT symbol,qty FROM ai_positions WHERE qty>0')]


def apply_live_sell_limits(candidates, broker_health):
    """Bound AI SELL advice by the broker's verified cash sellable quantity."""
    from execution.account_guard import ai_sellable_qty

    rows = {row['symbol']: row for row in (broker_health or {}).get('inventory_comparison') or []}
    inventory_checked = (broker_health or {}).get('inventory_checked') is True
    for candidate in candidates:
        held = max(0, int((candidate.get('paper_position') or {}).get('qty') or 0))
        row = rows.get(candidate['symbol']) if inventory_checked else None
        broker_cash = row.get('broker_cash_qty') if row else None
        broker_sellable = row.get('broker_sellable_qty') if row else None
        candidate['max_sell_qty'] = (ai_sellable_qty(broker_cash, broker_sellable, held)
                                     if broker_cash is not None and broker_sellable is not None
                                     else 0)
        candidate['sellable_context'] = dict(
            inventory_checked=inventory_checked and row is not None,
            broker_cash_qty=broker_cash, broker_sellable_qty=broker_sellable,
            ai_managed_qty=held,
            non_ai_cash_qty=max(0, int(broker_cash)-held) if broker_cash is not None else None,
            ai_sellable_limit=candidate['max_sell_qty'])


def apply_live_cost_basis(candidates, costs):
    """Expose reconstructed acquisition price without claiming broker fee precision."""
    for candidate in candidates:
        candidate['position'].pop('ai_average_cost', None)
        held = int((candidate.get('paper_position') or {}).get('qty') or 0)
        basis = costs.get(candidate['symbol'])
        if (not basis or held <= 0 or int(basis['quantity']) != held
                or basis.get('average_unit_cost_twd') is None):
            candidate['ai_cost_basis'] = {'available': False}
            continue
        unit_cost = Decimal(str(basis['average_unit_cost_twd']))
        if not unit_cost.is_finite() or unit_cost <= 0:
            candidate['ai_cost_basis'] = {'available': False}
            continue
        candidate['ai_cost_basis'] = {
            'available': True,
            'average_unit_cost_twd': str(unit_cost),
            'quantity': held,
            'estimated': bool(basis['estimated']),
            'excludes_fees_and_tax': True,
            'source': 'RECONSTRUCTED_FORMAL_AI_LEDGER',
        }
        candidate['position']['ai_average_cost'] = float(unit_cost)


def apply_live_position_context(candidates, broker_health, execution_db, audit):
    """Use the same verified sell limits, dates and costs after every account read."""
    from execution.trade_dates import dated_ai_trades
    from execution.trade_pnl import remaining_acquisition_costs

    apply_live_sell_limits(candidates, broker_health)
    dates = dated_ai_trades(execution_db, (c['symbol'] for c in candidates))
    for candidate in candidates:
        candidate['ai_trade_dates'] = dates[candidate['symbol']]
    from trade_plan_context import live_plans
    plans = live_plans(execution_db, {
        c['symbol']: int((c.get('paper_position') or {}).get('qty') or 0)
        for c in candidates}, analysis_path=Path(execution_db).parent.parent/'analysis'/'market_history.sqlite3')
    for candidate in candidates:
        candidate['buy_plans'] = plans.get(candidate['symbol'], {'available': False, 'lots': []})
    try:
        costs = remaining_acquisition_costs(
            execution_db, (broker_health or {}).get('broker_order_reconciliation'))
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, ArithmeticError) as exc:
        costs = {}
        audit.event('AI_COST_BASIS_UNAVAILABLE', {'reason': redact(exc)})
    apply_live_cost_basis(candidates, costs)


def apply_buy_limits(candidates, account, live_config=None):
    """Keep initial and Agent-refreshed quotes on the existing budget policy."""
    for candidate in candidates:
        candidate['pricing_policy'] = ('PLANNED_LIMIT' if getattr(live_config, 'planned_limits_enabled', False) else 'AT_QUOTE')
        candidate['max_buy_qty'] = 0
        if (candidate['last_price'] is None or candidate['quote_status'] != 'LIVE'
                or candidate.get('is_trial')):
            continue
        price = ((candidate.get('ask') or candidate['last_price'])
                 if live_config is not None else candidate['last_price'])
        candidate['budget_price'] = price
        quantity = paper_portfolio.max_affordable_qty(account['available_cash_cents'], price)
        if live_config is not None:
            from paper_portfolio import price_cents
            while quantity:
                gross = Decimal(price_cents(price) * quantity) / 100
                reserve = max(live_config.min_buy_fee_reserve_twd,
                              gross * live_config.buy_cash_buffer_rate)
                if (gross + reserve <= Decimal(str(account['available_cash_twd']))
                        and (live_config.max_order_twd is None
                             or gross <= live_config.max_order_twd)):
                    break
                quantity -= 1
        candidate['max_buy_qty'] = quantity


def broker_snapshot_after_execution(report, root):
    from execution.health_monitor import latest_refresh_snapshot, sync_broker_state

    if report.get('status') in ('NO_TRADE_ACTION', 'BLOCKED_BUDGET'):
        snapshot = latest_refresh_snapshot(root)
        if snapshot is not None:
            return snapshot
    return sync_broker_state(root)


class AnalysisService:
    def __init__(self, directory=ROOT/'data'/'analysis'):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db_path = self.directory/'market_history.sqlite3'
        self.stop = threading.Event()

    def run(self, trigger, phase=lambda value: None, read_only=False):
        if trigger not in ('AUTO', 'MANUAL'):
            raise ValueError('INVALID_TRIGGER')
        load_env()
        mode = trading_mode()
        if read_only and mode != 'agent':
            raise ValueError('READ_ONLY_PROBE_REQUIRES_AGENT_MODE')
        strategy = strategy_catalog.live_selection()
        run_id = now().strftime('%Y%m%dT%H%M%S')+'-gui-'+uuid.uuid4().hex[:8]
        directory = self.directory/'runs'/run_id
        directory.mkdir(parents=True)
        manifest = dict(run_id=run_id, trigger_type=trigger, started_at=now().isoformat(),
                        trading_mode=mode, state_root=str(ROOT), read_only=read_only,
                        run_directory=str(directory.resolve()), real_order_sent=False,
                        strategy={k: strategy[k] for k in ('name', 'path', 'version', 'sha256')})
        (directory/'strategy.md').write_text(strategy['prompt'], encoding='utf-8')
        save_json(directory/'run.json', manifest)
        def tracked_phase(value):
            manifest['last_phase']=value
            phase(value)
        try:
            with single_instance(self.directory/'runtime.lock'):
                return self._run(directory, manifest, tracked_phase, strategy)
        except Exception as exc:
            manifest.update(finished_at=now().isoformat(), status='ERROR', error=redact(f'{type(exc).__name__}: {exc}'))
            manifest['opportunity_diagnostics']=dict(outcome='PIPELINE_EXECUTION_FAILED',
                scope='OBSERVED_RESEARCH_POOL_ONLY_NOT_WHOLE_MARKET',last_phase=manifest.get('last_phase'),
                error=manifest['error'],counts={})
            save_json(directory/'opportunity_diagnostics.json',manifest['opportunity_diagnostics'])
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

    def _run(self, directory, manifest, phase, strategy):
        universe = load_universe(ROOT/'config'/'universe_tw.json')
        rules = load_rules(ROOT/'config'/'scan_rules.json')
        # The GUI contract is Top 10, independent of a one-shot CLI override.
        rules['candidate_top_n'] = 10
        paper_state = paper_portfolio.snapshot(self.db_path)
        if paper_state is None:
            raise ValueError('請先在主畫面設定資金總上限，再開始分析')
        from execution.auto_advice_executor import LiveExecutionConfig
        live_config = LiveExecutionConfig.load(ROOT/'config'/'execution_live.json')
        live_enabled = (self.directory.resolve() == (ROOT/'data'/'analysis').resolve()
                        and live_config.enabled)
        from opportunity_engine import policy as opportunity_policy, select as select_opportunities
        search_policy = opportunity_policy()
        opportunity_active = (live_enabled and manifest.get('trading_mode') == 'agent'
                              and strategy['name'] in ('strategy_support_scout_v1','strategy_opportunity_v1',
                                                       'strategy_opportunity_v2') and search_policy['enabled'])
        broker_health = None
        if live_enabled:
            from execution.health_monitor import sync_broker_state
            broker_health, _ = sync_broker_state(ROOT, return_report=True)
        positions = position_store.load(self.db_path)
        paper_positions = (live_ai_positions(ROOT/'data'/'execution'/'live_execution.sqlite3')
                           if live_enabled else paper_portfolio.positions(self.db_path))
        discovery = None
        if opportunity_active and not manifest.get('read_only'):
            phase('MARKET_DISCOVERY')
            from execution.execution_store import ExecutionStore
            from market_discovery import discover
            try:
                reserved = ExecutionStore(ROOT/'data/execution/live_execution.sqlite3',must_exist=True).daily_reserved_buy_twd()
                scout_budget = live_buy_budget(broker_health,reserved,paper_state['capital_limit_twd'],live_config.max_daily_buy_twd)
                scout_account = dict(paper_state,available_cash_twd=float(scout_budget['available']),
                                     budget_status=scout_budget['status'])
                excluded = [symbol for symbol,setting in (broker_health.get('ai_position_settings') or {}).items()
                            if setting.get('enabled') is False]
                discovery = discover(ROOT, directory, scout_account, search_policy,
                                     stopped=self.stop.is_set, excluded=excluded,
                                     include_weekly_recovery=True)
            except (OSError,sqlite3.Error,ValueError,TypeError) as exc:
                discovery = dict(status='UNAVAILABLE',symbols=[],watchlist=[],model_called=False,error=redact(exc))
            # Persist the fixed seed list and the independent scout's additions.
            universe = {**universe,'fixed_symbols':list(universe['symbols']),
                        'discovery_symbols':list(discovery['symbols']),
                        'symbols':list(dict.fromkeys(universe['symbols']+discovery['symbols']))}
            save_json(directory/'market_discovery.json',discovery)
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
                           'source':'CONFIGURED_CAP_REMAINING_WITH_BROKER_FUNDS_AND_RESERVE_CHECKS',
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
        scanned, ranked, _ = scan_and_rank([q for q in batch['quotes'] if q['symbol'] in universe['symbols']], rules, at)
        quote_map = {q['symbol']: q for q in batch['quotes']}
        top = [r for r in ranked if strategy_evidence.assess(
            quote_map[r['symbol']], scan_store.recent_history(self.db_path, r['symbol'], started)
        )['eligible_strategies']][:rules['candidate_top_n']]
        # Position-only stocks outside the universe still receive available
        # metrics, but cannot displace the universe's Top 10 ranking.
        scanned += [scan_quote(q, rules, at) for q in batch['quotes'] if q['symbol'] not in universe['symbols']]
        save_json(directory/'quant_scan.json', dict(rule_config=rules, quotes=scanned))
        save_json(directory/'candidate_ranking.json', dict(ranking=ranked, top_candidates=top))
        candidates = build_union(ranked if opportunity_active else top, ranked, batch['quotes'], positions, self.db_path, started, scanned,
                                 universe.get('etf_symbols', []), paper_positions, live_managed=live_enabled,
                                 ai_settings=(broker_health or {}).get('ai_position_settings'))
        phase('DAILY_HISTORY')
        daily_history.enrich_candidates(candidates, quote_map, at.date(),
                                        self.directory/'daily_history')
        if live_enabled:
            apply_live_position_context(candidates, broker_health,
                                       ROOT/'data'/'execution'/'live_execution.sqlite3', audit)
        else:
            paper_context = paper_portfolio.trade_context(
                self.db_path,
                {c['symbol']: int((c.get('paper_position') or {}).get('qty') or 0)
                 for c in candidates})
            for candidate in candidates:
                context = paper_context.get(candidate['symbol'], {})
                candidate['ai_trade_dates'] = context.get('ai_trade_dates',
                    {'timezone': 'Asia/Taipei', 'records': []})
                candidate['buy_plans'] = context.get('buy_plans',
                    {'available': False, 'lots': []})
        apply_buy_limits(candidates, paper_state, live_config if live_enabled else None)
        opportunity_audit = None
        opportunity_trigger = None
        if opportunity_active:
            from execution.execution_store import ExecutionStore
            try:
                pending_symbols=ExecutionStore(ROOT/'data/execution/live_execution.sqlite3',must_exist=True).pending_reconciliation_symbols()
            except (OSError,sqlite3.Error,ValueError):
                pending_symbols=set(universe['symbols'])
                audit.event('OPPORTUNITY_PENDING_ORDER_STATE_UNAVAILABLE',{})
            for c in candidates:
                c['opportunity_buy_eligible'] = c['symbol'] in universe['symbols']
                c['pending_execution'] = c['symbol'] in pending_symbols
                scout = next((w for w in (discovery or {}).get('watchlist',[]) if w['symbol']==c['symbol']),None)
                if scout:c['market_scout'] = scout
            save_json(directory/'opportunity_pool_before_selection.json', candidates)
            opportunity_pool=candidates
            candidates, opportunity_audit = select_opportunities(candidates, paper_state, live_config, search_policy)
            save_json(directory/'opportunity_pool_after_selection.json', opportunity_pool)
            if discovery is not None:
                from market_discovery import record_entry_feedback
                record_entry_feedback(ROOT,opportunity_pool,at)
            # Keep the entire researched pool for explaining rejected/omitted
            # symbols; only actual entry candidates and holdings reach the LLM.
            from opportunity_gate import evaluate
            opportunity_trigger = evaluate(manifest['trigger_type'], candidates, paper_state,
                self.directory/'opportunity_review_state.json', search_policy, at=at)
            save_json(directory/'opportunity_search.json', dict(search=opportunity_audit, trigger=opportunity_trigger))
        if discovery is not None:
            from market_discovery import watch_status
            discovery['watch_status'] = watch_status(discovery.get('watchlist',[]),batch['quotes'])
            save_json(directory/'market_discovery.json',discovery)
        save_json(directory/'analysis_set.json', candidates)
        agent_mode = manifest.get('trading_mode') == 'agent'
        if agent_mode:
            import trading_agent
        request = position_analyzer.build_request(candidates, paper_state, strategy_prompt=strategy['prompt'],
                            model=trading_agent.agent_model() if agent_mode else None)
        expansion_count = 0

        def expand_candidates(known_symbols, decisions):
            nonlocal expansion_count
            from market_discovery import discover, record_entry_feedback, merge_discovery
            from execution.broker_runtime import get_broker_runtime
            expansion_count += 1
            destination = directory/'search_expansions'/f'{expansion_count:03d}'
            destination.mkdir(parents=True, exist_ok=True)
            phase('SEARCHING_ALTERNATIVES')
            record_entry_feedback(ROOT, opportunity_pool, now(), decisions)
            disabled = {symbol for symbol, setting in (broker_health or {}).get('ai_position_settings', {}).items()
                        if setting.get('enabled') is False}
            replacement = discover(ROOT, destination, paper_state, search_policy,
                stopped=self.stop.is_set, excluded=disabled | set(symbols) | set(known_symbols), reconsider=True)
            merge_discovery(discovery, replacement)
            save_json(directory/'market_discovery.json', discovery)
            fresh_symbols = [s for s in replacement.get('symbols', []) if s not in symbols and s not in known_symbols]
            result = dict(status=replacement['status'], observed_symbols=fresh_symbols, candidates=[])
            if not fresh_symbols or self.stop.is_set():
                return result
            # Every newly discovered name follows the same broker, liquidity,
            # complete-history, ownership, budget and plan checks as the seed pool.
            symbols.extend(fresh_symbols)
            universe['symbols'].extend(fresh_symbols)
            universe['discovery_symbols'] = list(dict.fromkeys(universe.get('discovery_symbols', []) + fresh_symbols))
            started_extra = now().isoformat()
            extra_raw, extra_errors = get_broker_runtime(ROOT).fetch_quotes(
                fresh_symbols, Audit(destination), self.stop.is_set, timeout=12)
            extra = multi_snapshot(fresh_symbols, extra_raw, extra_errors, started_extra, now().isoformat())
            save_json(destination/'multi_market_snapshot.json', extra)
            errors.update(extra_errors)
            extra_scanned, extra_ranked, _ = scan_and_rank(extra['quotes'], rules, now())
            additions = build_union(extra_ranked, extra_ranked, extra['quotes'],
                [p for p in positions if p['symbol'] in fresh_symbols], self.db_path,
                started_extra, extra_scanned, universe.get('etf_symbols', []),
                [p for p in paper_positions if p['symbol'] in fresh_symbols],
                live_managed=live_enabled, ai_settings=(broker_health or {}).get('ai_position_settings'))
            extra_map = {q['symbol']:q for q in extra['quotes']}
            daily_history.enrich_candidates(additions, extra_map, now().date(), self.directory/'daily_history')
            if live_enabled:
                apply_live_position_context(additions, broker_health, ROOT/'data/execution/live_execution.sqlite3', Audit(destination))
            apply_buy_limits(additions, paper_state, live_config if live_enabled else None)
            for candidate in additions:
                candidate['opportunity_buy_eligible'] = True
                candidate['pending_execution'] = candidate['symbol'] in pending_symbols
                candidate['market_scout'] = next(w for w in replacement['watchlist'] if w['symbol'] == candidate['symbol'])
            save_json(destination/'opportunity_pool_before_selection.json', additions)
            batch['quotes'].extend(extra['quotes'])
            batch['symbols_received'] += extra['symbols_received']
            batch['symbols_unavailable'] += extra['symbols_unavailable']
            quote_map.update(extra_map)
            ranked.extend(extra_ranked)
            save_json(directory/'universe_snapshot.json', universe)
            result['candidates'] = additions
            return result

        if agent_mode:
            manifest['runtime'] = trading_agent.execution_metadata(request['model'])
        save_json(directory/'openai_request.json', request)
        phase('WAITING_OPENAI')
        budget_ready = not live_enabled or paper_state.get('budget_status') == 'READY'
        search_empty_pool = (agent_mode and opportunity_active and discovery is not None and not candidates
            and batch['symbols_received'] > 0 and not manifest.get('read_only') and not self.stop.is_set())
        if (manifest.get('read_only') or budget_ready) and (
                (manifest.get('read_only') and not self.stop.is_set()) or
                should_call_openai(trigger=manifest['trigger_type'], candidates=candidates,
                                   stopped=self.stop.is_set(), wall=now().timestamp()) or
                search_empty_pool) and (
                    not opportunity_active or opportunity_trigger['call_model'] or
                    search_empty_pool):
            if agent_mode:
                from agent_decision import analyze_existing
                decision, response = analyze_existing(self, directory, manifest, request, candidates,
                                                       paper_state, live_config, live_enabled, rules,
                                                       broker_health=broker_health,
                                                       opportunity_pool=opportunity_pool if opportunity_active else None,
                                                       expand_candidates=expand_candidates if discovery is not None else None)
                budget_ready = not live_enabled or paper_state.get('budget_status') == 'READY'
                paper_positions = paper_state.get('positions', paper_positions)
                save_json(directory/'paper_account_snapshot.json', paper_state)
                # On-demand Agent quotes become the authoritative handoff data.
                fresh_by_symbol = {c['symbol']:c for c in candidates}
                batch['quotes'] = [{**q, **fresh_by_symbol.get(q['symbol'], {})} for q in batch['quotes']]
                batch['symbols_unavailable'] = sum(q['quote_status']=='UNAVAILABLE' for q in batch['quotes'])
                batch['symbols_received'] = len(batch['quotes'])-batch['symbols_unavailable']
                save_json(directory/'analysis_set.json', candidates)
                save_json(directory/'multi_market_snapshot.json', batch)
                if opportunity_active:
                    # The final audit includes later research and replacements,
                    # rather than continuing to display the opening shortlist.
                    opportunity_audit['research_symbols'] = [c['symbol'] for c in candidates]
                    opportunity_audit['watch_research_symbols'] = [c['symbol'] for c in candidates if c.get('research_only')]
                    opportunity_audit['qualified_symbols'] = [c['symbol'] for c in opportunity_pool if c.get('trade_setups')]
                    opportunity_audit['pool_count'] = len(opportunity_pool)
                    opportunity_audit['candidate_search'] = response.get('candidate_search')
                    save_json(directory/'opportunity_pool_after_selection.json', opportunity_pool)
                    save_json(directory/'opportunity_search.json', dict(search=opportunity_audit, trigger=opportunity_trigger))
            else:
                decision, response = position_analyzer.analyze(candidates, request)
        else:
            market_closed = twse_closed_reason(now().date())
            has_live_candidate = any(
                c.get('quote_status') == 'LIVE' and not c.get('is_trial')
                for c in candidates
            )
            reason = ('交易額度已用盡或資金檢查尚未通過；本輪暫停所有 BUY／SELL，額度恢復後重新評估。'
                      if not budget_ready else
                      f'證交所今日休市（{market_closed}）；目前只有歷史行情，'
                      '可買股數為 0。本輪不呼叫 AI，也不送出委託。'
                      if market_closed else
                      '沒有可驗證的即時行情；可買股數為 0。本輪不呼叫 AI，也不送出委託。'
                      if not has_live_candidate else
                      'AUTO 等待盤中零股交易時段；本輪不呼叫 AI，也不送出委託。'
                      if manifest['trigger_type'] == 'AUTO' and not self.stop.is_set()
                      else '沒有可分析集合，或已要求停止。')
            if budget_ready and opportunity_active and not opportunity_trigger['call_model']:
                if batch['quotes'] and all(q.get('is_trial') for q in batch['quotes']):
                    reason = '本輪收到的行情全是零股試撮，沒有可用於交易評估的即時成交行情。'
                else:
                    reason = ('已完成研究池的機會篩選；目前沒有符合成本與風險的進場計畫，也沒有需重評的持倉變化，本輪未呼叫交易決策模型。')
                if (discovery or {}).get('model_called'):
                    reason = '本輪已執行獨立尋標子代理；' + reason
            manifest['display_notice'] = reason
            decision = dict(market_view=reason, decisions=[])
            response = dict(validation='NOT_CALLED', api={}, raw_response='')
        if discovery is not None:
            from market_discovery import merge_usage
            merge_usage(response,directory,discovery)
        phase('PROCESSING_RESULT')
        if response['validation'] == 'VALID' and not self.stop.is_set() and not live_enabled and not manifest.get('read_only'):
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
        # Per-symbol quote failures do not invalidate other symbols' verified
        # advice. Complete connection/data failures still fail the whole cycle.
        failures = []
        if response['validation'] == 'ERROR':
            failures.append(str(response['api'].get('error')))
        if not batch['symbols_received']:
            failures.append('NO_VALID_MARKET_DATA')
        manifest.update(finished_at=now().isoformat(), request_started_at=started, request_completed_at=completed,
                        universe_count=len(universe['symbols']), quote_symbols_count=len(symbols),
                        symbols_received=batch['symbols_received'], ai_validation=response['validation'],
                        llm_model=request.get('model'),
                        llm_request_started_at=response.get('request_started_at'),
                        llm_response_received_at=response.get('response_received_at'),
                        status='ERROR' if failures else ('COMPLETE' if budget_ready else 'BLOCKED_BUDGET'),
                        error='; '.join(failures), quote_warnings=errors,
                        research_status=response['validation'],
                        candidates_sent=len(candidates) if response['validation']!='NOT_CALLED' else 0)
        if discovery is not None:
            # Optional scouting can fail while the seed universe still yields valid advice.
            manifest['discovery_status']=discovery['status']
            expansion_errors = [r['error'] for r in (response.get('candidate_search') or {}).get('expansions', [])
                                if r.get('error')]
            manifest['research_warning']=discovery.get('error') or '；'.join(expansion_errors) or None
        result = dict(manifest=manifest, batch=batch, top=top, analysis_set=candidates, positions=contexts,
                      decision=decision, paper_simulation=simulation,
                      pretrade_review=response.get('pretrade_review'),
                      daily_participation=response.get('daily_participation'),
                      support_research=response.get('support_research'),
                       entry_research=response.get('entry_research'),
                      web_search=response.get('api', {}).get('web_search'),
                      paper_trade_history=paper_portfolio.trades(self.db_path))
        if opportunity_active:
            result['opportunity_search'] = dict(search=opportunity_audit, trigger=opportunity_trigger)
        if discovery is not None:
            result['market_discovery'] = discovery
        if opportunity_active:
            from opportunity_diagnostics import summarize
            result['opportunity_diagnostics']=summarize(batch,ranked,opportunity_pool,candidates,
                opportunity_audit,discovery,response,decision,paper_state)
            manifest['opportunity_diagnostics']=result['opportunity_diagnostics']
            save_json(directory/'opportunity_diagnostics.json',result['opportunity_diagnostics'])
        if live_enabled:
            result['execution_budget'] = {k:paper_state.get(k) for k in (
                'available_cash_twd','minimum_balance_reserve_twd',
                'strategy_cap_twd','strategy_remaining_twd',
                'ai_committed_capital_twd','pending_buy_reserve_twd',
                'daily_buy_limit_twd','daily_buy_reserved_twd',
                'daily_buy_remaining_twd','budget_status')}
            result['execution_budget'].update(
                min_buy_fee_reserve_twd=float(live_config.min_buy_fee_reserve_twd),
                minimum_commission_twd=float(live_config.economic_minimum_commission_twd),
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
        ) if live_enabled and not manifest.get('read_only') else None
        if manifest.get('read_only'):
            result['execution_report'] = dict(status='READ_ONLY',trading_mode='agent',broker_order_sent=False)
            save_json(directory/'dashboard_result.json',result)
            save_json(self.directory/'latest_dashboard.json',result)
        if execution_report is not None:
            if execution_report.get('status') == 'BLOCKED_BUDGET':
                manifest['status'] = 'BLOCKED_BUDGET'
                manifest['display_notice'] = (execution_report.get('reason') or execution_report.get('block_reason')
                                              or '交易額度不足；本輪所有交易暫停。')
                result['execution_budget']['budget_status'] = 'BLOCKED'
                paused = json.loads(advice_path.read_text(encoding='utf-8'))
                paused.update(run_status='BLOCKED_BUDGET',display_notice=manifest['display_notice'],
                              execution_budget=result['execution_budget'])
                save_json(advice_path,paused)
            execution_report['trading_mode'] = manifest.get('trading_mode','llm')
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
            # An empty advice has sent no order. Reuse its immediately preceding
            # broker check: an immediate second inventory query may return 1019.
            # After any real execution attempt, a fresh read is still required.
            try:
                result['broker_snapshot'] = broker_snapshot_after_execution(
                    execution_report, ROOT)
            except Exception as exc:
                result['broker_refresh_error'] = redact(f'{type(exc).__name__}: {exc}')
            save_json(directory/'dashboard_result.json', result)
            save_json(self.directory/'latest_dashboard.json', result)
        if opportunity_active and execution_report is not None:
            result['opportunity_diagnostics'].update(execution_status=execution_report.get('status'),
                broker_orders_sent=sum(r.get('broker_order_sent') is True for r in execution_report.get('results',[])))
            save_json(directory/'opportunity_diagnostics.json',result['opportunity_diagnostics'])
            save_json(directory/'run.json',manifest)
            save_json(directory/'dashboard_result.json',result)
            save_json(self.directory/'latest_dashboard.json',result)
        if opportunity_active and not manifest.get('read_only') and not self.stop.is_set():
            from opportunity_gate import record
            # Research completion is distinct from a handled execution cycle.
            # A new cycle must re-read broker state and obtain fresh advice;
            # this never replays an earlier order or bypasses pending-order guards.
            if response['validation'] in ('VALID', 'ERROR') or not budget_ready:
                items = (execution_report or {}).get('results', [])
                handled = (manifest['status'] == 'COMPLETE' and response['validation'] == 'VALID'
                           and execution_report is not None
                           and execution_report.get('status') in ('COMPLETE','NO_TRADE_ACTION')
                           and not any(x.get('status') in ('ERROR','FAILED','UNCONFIRMED','SKIPPED_CHECK_FAILED')
                                       for x in items))
                record(self.directory/'opportunity_review_state.json', candidates, paper_state,
                       at=now(), cycle_complete=handled, run_id=manifest['run_id'],
                       execution_status=(execution_report or {}).get('status') or manifest['status'])
        manifest['execution_status'] = (execution_report or {}).get('status') or 'NOT_DISPATCHED'
        scan_store.update_run_manifest(self.db_path,manifest)
        save_json(directory/'run.json', manifest)
        save_json(directory/'dashboard_result.json', result)
        save_json(self.directory/'latest_dashboard.json', result)
        return result

    def _execute_after_advice_written(self, trigger, result, directory, advice_path):
        manifest = result.get('manifest') or {}
        if manifest.get('read_only'):
            return None
        if manifest.get('status') == 'BLOCKED_BUDGET':
            report = dict(status='BLOCKED_BUDGET', reason=manifest.get('display_notice') or
                          '交易額度不足；本輪所有交易暫停。', results=[],
                          broker_order_sent=False, execution_attempted=False, automatic_retry=False)
            save_json(directory/'execution_report.json', report)
            return report
        if (trigger not in ('AUTO','MANUAL') or manifest.get('status') != 'COMPLETE'
                or manifest.get('ai_validation') != 'VALID'):
            return None

        from execution.broker_runtime import get_broker_runtime

        execution_attempted = False
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
            if original <= 0 or current <= 0 or fresh.get('budget_status') in ('BLOCKED', 'UNVERIFIED'):
                report = dict(status='BLOCKED_BUDGET', reason='送單前額度不足或尚未核實；本輪所有交易暫停。',
                              results=[], broker_order_sent=False, execution_attempted=False,
                              automatic_retry=False)
                save_json(directory/'execution_report.json', report)
                return report
            # The same COM-owning broker thread handles every AUTO cycle and
            # the broker reconciliation; no duplicate Capital login.
            execution_attempted = True
            report = get_broker_runtime(ROOT).execute(advice_path)
        except Exception as exc:
            report = {
                'status': 'ERROR',
                'advice_path': str(advice_path),
                'reason': redact(f'{type(exc).__name__}: {exc}'),
                'automatic_retry': False,
                'execution_attempted': execution_attempted,
                'run_id': manifest.get('run_id'),
                'execution_origin': 'ANALYSIS_SERVICE',
            }
            latest_execution = self.directory.parent/'execution'/'latest_live_execution.json'
            save_json(latest_execution, report)
        if 'trading_mode' in manifest:
            report['trading_mode'] = manifest['trading_mode']
        save_json(directory/'execution_report.json', report)
        return report

    def refresh_broker_state(self, refresh_quotes=True):
        """Shared manual/automatic refresh; quotes use the existing COM worker."""
        from execution.health_monitor import sync_broker_state

        def add_position_quotes(health, runtime):
            if not refresh_quotes:
                return
            # Clear prior evidence on failures or an empty/unverified inventory.
            health['position_quotes'] = []
            health['position_quote_errors'] = {}
            health['position_quotes_checked_at'] = now().isoformat()
            symbols = list(dict.fromkeys(
                row['symbol'] for row in health.get('inventory_comparison', [])
                if int(row.get('broker_cash_qty') or 0) > 0))
            if not symbols or not health.get('inventory_checked'):
                return
            started = now().isoformat()
            try:
                raw, errors = runtime.fetch_quotes(
                    symbols, Audit(self.directory/'broker_refresh'),
                    self.stop.is_set, timeout=12)
                quote_batch = multi_snapshot(
                    symbols, raw, errors, started, now().isoformat())
                health['position_quotes'] = quote_batch['quotes']
                health['position_quote_errors'] = dict(errors)
                for quote in quote_batch['quotes']:
                    if quote['quote_status'] != 'LIVE' or quote.get('is_trial'):
                        health['position_quote_errors'].setdefault(
                            quote['symbol'], '；'.join(quote.get('warnings') or [])
                            or '尚無可驗證的即時行情')
                health['position_quotes_checked_at'] = quote_batch['request_completed_at']
            except Exception as exc:
                health['position_quote_errors'] = {
                    'batch': redact(f'{type(exc).__name__}: {exc}')}

        return sync_broker_state(ROOT, enrich=add_position_quotes)

    def refresh(self, sync_broker=False):
        from execution.health_monitor import latest_refresh_snapshot
        broker_snapshot = (self.refresh_broker_state() if sync_broker
                           else latest_refresh_snapshot(ROOT))
        path = self.directory/'latest_dashboard.json'
        try:
            result = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            result = scan_store.historical_dashboard(self.db_path)
        positions = position_store.load(self.db_path)
        if result is not None:
            result['paper_simulation'] = paper_portfolio.snapshot(self.db_path)
            result['paper_trade_history'] = paper_portfolio.trades(self.db_path)
        quotes = {q['symbol']:q['last_price'] for q in result['batch']['quotes']} if result else {}
        return result, [position_store.context(p, quotes.get(p['symbol'])) for p in positions], broker_snapshot
