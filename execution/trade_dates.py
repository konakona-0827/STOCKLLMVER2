"""Read-only dated AI trade history for analysis prompts."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from config import TAIPEI


def _taipei_date(stamp):
    parsed = datetime.fromisoformat(stamp)
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(TAIPEI).date().isoformat()


def dated_ai_trades(db_path, symbols, *, recent_limit=24):
    """Return BUY/SELL dates; order and manual dates are labelled by source.

    Broker replay does not provide a verified fill date here, so a filled
    order contributes its submission date. Manual purchase records contribute
    their entry date, which may differ from the actual purchase date.
    """
    wanted = {str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}
    output = {symbol: {'timezone': 'Asia/Taipei', 'records': []}
              for symbol in wanted}
    if not wanted:
        return output
    path = Path(db_path).resolve()
    with closing(sqlite3.connect(f'file:{path.as_posix()}?mode=ro',
                                 uri=True, timeout=2)) as con:
        placeholders = ','.join('?' for _ in wanted)
        attempts = con.execute(
            f"""SELECT symbol,side,started_at,detail_json FROM execution_attempts
                WHERE symbol IN ({placeholders}) AND side IN ('BUY','SELL')""",
            tuple(sorted(wanted))).fetchall()
        has_adoptions = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='manual_ai_adoptions'").fetchone()
        adoptions = (con.execute(
            f"""SELECT symbol,qty,adopted_at FROM manual_ai_adoptions
                WHERE symbol IN ({placeholders})""",
            tuple(sorted(wanted))).fetchall() if has_adoptions else [])

    events = {symbol: [] for symbol in wanted}
    for symbol, side, started_at, raw in attempts:
        try:
            detail = json.loads(raw or '{}')
            quantity = int(detail.get('filled_quantity') or 0)
            date = _taipei_date(started_at)
        except (TypeError, ValueError, KeyError):
            continue
        if quantity > 0 and date:
            events[symbol].append({
                'side': side, 'date': date, 'quantity': quantity,
                'date_basis': 'FILLED_ORDER_SUBMISSION_DATE',
            })
    for symbol, quantity, adopted_at in adoptions:
        try:
            date = _taipei_date(adopted_at)
            quantity = int(quantity)
        except (TypeError, ValueError):
            continue
        if quantity > 0 and date:
            events[symbol].append({
                'side': 'BUY', 'date': date, 'quantity': quantity,
                'date_basis': 'MANUAL_PURCHASE_ENTRY_DATE',
            })
    for symbol, rows in events.items():
        rows.sort(key=lambda item: (item['date'], item['side'], item['date_basis']))
        output[symbol]['records'] = rows[-recent_limit:]
    return output
