"""Read-only estimated realized price difference for formal AI SELL orders."""
from contextlib import closing
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import json
import sqlite3


def _positive_decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and result > 0 else None


def _priced_gross(detail, check, filled, *, allow_limit):
    for source in (check or {}, detail):
        fills = source.get('broker_replay_fill_details') or []
        if not fills:
            continue
        try:
            quantities = [int(row['quantity']) for row in fills]
            prices = [_positive_decimal(row['price_twd']) for row in fills]
        except (KeyError, TypeError, ValueError):
            continue
        if sum(quantities) == filled and all(qty > 0 for qty in quantities) and all(prices):
            return sum((price * qty for price, qty in zip(prices, quantities)), Decimal('0')), False
    if allow_limit:
        limit = _positive_decimal(detail.get('limit_price'))
        if limit is not None:
            return limit * filled, True
    return None, False


def _reconstruct_price_difference(db_path, order_checks):
    path = Path(db_path).resolve()
    with closing(sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True, timeout=2)) as con:
        attempts = con.execute(
            """SELECT decision_id,symbol,side,started_at,detail_json
               FROM execution_attempts ORDER BY started_at,rowid""").fetchall()
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        adoptions = (con.execute(
            """SELECT symbol,qty,unit_cost_twd,adopted_at
               FROM manual_ai_adoptions ORDER BY adopted_at,id""").fetchall()
            if 'manual_ai_adoptions' in tables else [])
        adjustments = (con.execute(
            """SELECT symbol,qty,committed_cost_twd,adjusted_at
               FROM ai_position_adjustments ORDER BY adjusted_at,id""").fetchall()
            if 'ai_position_adjustments' in tables else [])

    events = []
    for symbol, qty, cost, at in adjustments:
        events.append((datetime.fromisoformat(at), 0, 'adjust', symbol, qty, cost))
    for symbol, qty, unit_cost, at in adoptions:
        events.append((datetime.fromisoformat(at), 1, 'adopt', symbol, qty, unit_cost))
    for decision_id, symbol, side, at, raw in attempts:
        events.append((datetime.fromisoformat(at), 2, 'trade', symbol, side,
                       decision_id, json.loads(raw or '{}')))
    events.sort(key=lambda row: (row[0], row[1]))

    checks = {row['decision_id']: row for row in order_checks or []}
    holdings = {}  # symbol -> (shares, acquisition cost, estimated)
    realized = {}
    for _at, _priority, kind, symbol, *values in events:
        held, cost, estimated = holdings.get(symbol, (0, Decimal('0'), False))
        if kind == 'adjust':
            qty, amount = values
            parsed = _positive_decimal(amount) if int(qty) > 0 else Decimal('0')
            holdings[symbol] = (int(qty), parsed, int(qty) > 0)
        elif kind == 'adopt':
            qty, unit_cost = values
            unit = _positive_decimal(unit_cost)
            new_cost = cost + unit * int(qty) if cost is not None and unit is not None else None
            holdings[symbol] = (held + int(qty), new_cost, True)
        else:
            side, decision_id, detail = values
            filled = int(detail.get('filled_quantity') or 0)
            if filled <= 0:
                continue
            gross, price_estimated = _priced_gross(
                detail, checks.get(decision_id), filled, allow_limit=side == 'BUY')
            if side == 'BUY':
                new_cost = cost + gross if cost is not None and gross is not None else None
                holdings[symbol] = (held + filled, new_cost, estimated or price_estimated)
            elif side == 'SELL':
                if filled > held:
                    holdings[symbol] = (0, None, True)
                    continue
                released = cost * filled / held if cost is not None and held else None
                if gross is not None and released is not None:
                    realized[decision_id] = (gross - released, estimated)
                remaining = held - filled
                holdings[symbol] = (remaining,
                                    cost - released if remaining and released is not None else Decimal('0') if not remaining else None,
                                    estimated if remaining else False)
    return realized, holdings


def realized_price_difference(db_path, order_checks):
    """Return decision_id -> (TWD amount, estimated acquisition cost).

    This excludes commissions and transaction tax. Missing SELL fill prices or
    an unknown cost basis produce no value; no trade/account data is changed.
    """
    realized, _holdings = _reconstruct_price_difference(db_path, order_checks)
    return realized


def remaining_acquisition_costs(db_path, order_checks):
    """Return remaining AI shares' average purchase price, excluding fees/tax."""
    _realized, holdings = _reconstruct_price_difference(db_path, order_checks)
    return {
        symbol: dict(quantity=quantity,
                     average_unit_cost_twd=str(cost / quantity) if cost is not None else None,
                     estimated=estimated)
        for symbol, (quantity, cost, estimated) in holdings.items()
        if quantity > 0
    }
