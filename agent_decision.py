"""Replace only the LLM decision step, using the current account and execution ledger."""
from contextlib import closing
import json
import sqlite3

from config import ROOT, now, redact
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


def refreshed_market_context(current, previous, scan_rules):
    """Recheck the quote without dropping the verified daily evidence."""
    daily = {key: previous[key] for key in
             ('daily_bars', 'daily_trend', 'daily_history_status') if key in previous}
    evidence = strategy_evidence.assess({**current, **daily},
                                        previous.get('market_history', []))
    metrics = scan_quote(current, scan_rules, now())
    return {**current, **daily,
            'asset_type': previous.get('asset_type', 'UNKNOWN'),
            'strategy_evidence': evidence,
            'eligible_strategies': evidence['eligible_strategies'],
            'metrics': metrics.get('metrics', {}),
            'components': metrics.get('components', {})}


def analyze_existing(service, directory, manifest, request, candidates, account, rules, live_enabled, scan_rules):
    from analysis_service import (apply_live_cost_basis, apply_live_sell_limits,
                                  live_ai_positions, live_buy_budget)
    from execution.broker_runtime import get_broker_runtime
    execution_db = ROOT/'data'/'execution'/'live_execution.sqlite3'

    def portfolio():
        if not live_enabled:
            return paper_portfolio.snapshot(service.db_path)
        from execution.health_monitor import sync_broker_state
        from execution.execution_store import ExecutionStore
        health = sync_broker_state(ROOT)
        daily = ExecutionStore(execution_db, must_exist=True).daily_reserved_buy_twd()
        cap = paper_portfolio.account(service.db_path)['capital_limit_cents']/100
        budget = live_buy_budget(health, daily, cap, rules.max_daily_buy_twd)
        positions = live_ai_positions(execution_db)
        by_symbol = {p['symbol']: p for p in positions}
        for candidate in candidates:
            symbol = candidate['symbol']
            holding = by_symbol.get(symbol, dict(symbol=symbol, qty=0, cost_cents=None))
            candidate['paper_position'] = {**candidate.get('paper_position', {}), **holding}
            candidate['position']['ai_managed_qty'] = holding['qty']
        apply_live_sell_limits(candidates, health)
        from execution.trade_dates import dated_ai_trades
        from execution.trade_pnl import remaining_acquisition_costs
        trade_dates = dated_ai_trades(execution_db, (c['symbol'] for c in candidates))
        for candidate in candidates:
            candidate['ai_trade_dates'] = trade_dates[candidate['symbol']]
        try:
            costs = remaining_acquisition_costs(
                execution_db, health.get('broker_order_reconciliation'))
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, ArithmeticError) as exc:
            costs = {}
            Audit(directory).event('AI_COST_BASIS_UNAVAILABLE', {'reason': redact(exc)})
        apply_live_cost_basis(candidates, costs)
        return {**account, 'available_cash_cents':int(budget['available']*100),
                'available_cash_twd':float(budget['available']), 'capital_limit_twd':cap,
                'positions':positions, 'budget_status':budget['status'],
                'broker_checked_at':health.get('checked_at'), 'source':'CURRENT_LIVE_EXECUTION_ACCOUNT',
                'minimum_balance_reserve_twd':float(budget['minimum_reserve']),
                'daily_buy_remaining_twd':float(budget['daily_remaining']),
                'pending_buy_reserve_twd':float(budget['pending_reserve']),
                'ai_committed_capital_twd':float(budget['ai_committed']),
                'strategy_remaining_twd':float(budget['strategy_remaining'])}

    def quote(symbol, previous):
        started = now().isoformat()
        raw, errors = get_broker_runtime(ROOT).fetch_quotes([symbol], Audit(directory), service.stop.is_set, timeout=12)
        current = multi_snapshot([symbol], raw, errors, started, now().isoformat())['quotes'][0]
        return refreshed_market_context(current, previous, scan_rules)

    context = trading_agent.TradingTools(candidates, rules, manifest['run_id'], directory/'agent_tools.json',
                quote, portfolio,
                (lambda:execution_history(execution_db)) if live_enabled else
                (lambda:paper_portfolio.trades(service.db_path, limit=30)),
                service.stop.is_set, bool(manifest.get('read_only')), json.loads(request['input']))
    decision, response = trading_agent.analyze(context, request['instructions'],model=request['model'])
    response['api']['execution_mode'] = 'LIVE' if live_enabled else 'PAPER'
    response['api']['state_root'] = str(ROOT)
    return decision, response
