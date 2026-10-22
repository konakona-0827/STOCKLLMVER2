"""Replace only the LLM decision step, using the current account and execution ledger."""
from contextlib import closing
import json
import sqlite3

from config import ROOT, now
from main import Audit
from capital_multi_quote import multi_snapshot
from quant_scanner import scan_quote
import paper_portfolio
import strategy_evidence
import trading_agent


def execution_history(path, limit=30):
    """Read existing formal attempts without creating or resetting any account."""
    with closing(sqlite3.connect('file:' + str(path.resolve()) + '?mode=ro', uri=True)) as db:
        rows = db.execute('''SELECT run_id,symbol,side,started_at,status,detail_json
                             FROM execution_attempts ORDER BY started_at DESC LIMIT ?''', (limit,)).fetchall()
    results = []
    for run_id, symbol, side, at, status, raw in rows:
        detail = json.loads(raw or '{}')
        results.append(dict(run_id=run_id, symbol=symbol, side=side, at=at, status=status,
                            trading_mode=detail.get('trading_mode','llm'),
                            requested_qty=detail.get('quantity'), filled_qty=detail.get('filled_quantity'),
                            limit_price=detail.get('limit_price'), reason=detail.get('reason')))
    return results


def analyze_existing(service, directory, manifest, request, candidates, account, rules, live_enabled, scan_rules,
                     broker_health=None):
    from analysis_service import apply_live_position_context, live_ai_positions, live_buy_budget
    from execution.broker_runtime import get_broker_runtime
    execution_db = ROOT/'data'/'execution'/'live_execution.sqlite3'

    def portfolio():
        if not live_enabled:
            fresh = paper_portfolio.snapshot(service.db_path)
            account.update(fresh)
            return dict(account)
        from execution.health_monitor import sync_broker_state
        from execution.execution_store import ExecutionStore
        health = sync_broker_state(ROOT)
        daily = ExecutionStore(execution_db, must_exist=True).daily_reserved_buy_twd()
        from decimal import Decimal
        cap = Decimal(paper_portfolio.account(service.db_path)['capital_limit_cents'])/100
        budget = live_buy_budget(health, daily, cap, rules.max_daily_buy_twd)
        positions = live_ai_positions(execution_db)
        held = {position['symbol']: position for position in positions}
        for candidate in candidates:
            candidate['paper_position'] = held.get(candidate['symbol'], dict(qty=0, cost_cents=None))
            candidate['position']['ai_managed_qty'] = candidate['paper_position']['qty']
        apply_live_position_context(candidates, health, execution_db, Audit(directory))
        by_symbol = {candidate['symbol']: candidate for candidate in candidates}
        for position in positions:
            candidate = by_symbol.get(position['symbol'])
            if candidate:
                position.update({key: candidate[key] for key in
                                 ('max_sell_qty', 'ai_trade_dates', 'ai_cost_basis')})
        account.update(available_cash_cents=int(budget['available']*100),
                available_cash_twd=float(budget['available']), capital_limit_twd=float(cap),
                positions=positions, budget_status=budget['status'],
                broker_checked_at=health.get('checked_at'),
                source='CONFIGURED_CAP_REMAINING_WITH_BROKER_FUNDS_AND_RESERVE_CHECKS',
                strategy_cap_twd=float(budget['strategy_cap']),
                daily_buy_limit_twd=float(budget['daily_limit']),
                daily_buy_reserved_twd=float(budget['daily_reserved']),
                minimum_balance_reserve_twd=float(budget['minimum_reserve']),
                daily_buy_remaining_twd=float(budget['daily_remaining']),
                pending_buy_reserve_twd=float(budget['pending_reserve']),
                ai_committed_capital_twd=float(budget['ai_committed']),
                strategy_remaining_twd=float(budget['strategy_remaining']))
        if broker_health is not None and broker_health is not health:
            broker_health.clear()
            broker_health.update(health)
        return dict(account)

    def quote(symbol, previous):
        started = now().isoformat()
        raw, errors = get_broker_runtime(ROOT).fetch_quotes([symbol], Audit(directory), service.stop.is_set, timeout=12)
        current = multi_snapshot([symbol], raw, errors, started, now().isoformat())['quotes'][0]
        evidence = strategy_evidence.assess(
            {**current, 'daily_bars': previous.get('daily_bars', [])},
            previous.get('market_history', []))
        metrics = scan_quote(current, scan_rules, now())
        return {**current, 'asset_type':previous.get('asset_type', 'UNKNOWN'),
                'strategy_evidence':evidence, 'eligible_strategies':evidence['eligible_strategies'],
                'metrics':metrics.get('metrics', {}), 'components':metrics.get('components', {})}

    context = trading_agent.TradingTools(candidates, rules, manifest['run_id'], directory/'agent_tools.json',
                quote, portfolio,
                (lambda:execution_history(execution_db)) if live_enabled else
                (lambda:paper_portfolio.trades(service.db_path, limit=30)),
                service.stop.is_set, bool(manifest.get('read_only')), json.loads(request['input']),
                live_enabled=live_enabled, search_request=request)
    decision, response = trading_agent.analyze(context, request['instructions'],model=request['model'])
    response['api']['execution_mode'] = 'LIVE' if live_enabled else 'PAPER'
    response['api']['state_root'] = str(ROOT)
    return decision, response
