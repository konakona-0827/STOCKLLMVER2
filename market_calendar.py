"""TWSE market closures that affect intraday quote and order decisions.

Source: https://www.twse.com.tw/zh/trading/holiday.html (115 / 2026 calendar).
Live quote freshness is checked separately, including for dates not listed here.
"""
from datetime import date, timedelta


_CLOSED_2026 = {
    date(2026, 1, 1): '元旦',
    date(2026, 2, 12): '春節前市場無交易',
    date(2026, 2, 13): '春節前市場無交易',
    **{date(2026, 2, day): '春節' for day in range(16, 21)},
    date(2026, 2, 27): '和平紀念日補假',
    date(2026, 4, 3): '兒童節補假',
    date(2026, 4, 6): '清明節補假',
    date(2026, 5, 1): '勞動節',
    date(2026, 6, 19): '端午節',
    date(2026, 9, 25): '中秋節',
    date(2026, 9, 28): '教師節',
    date(2026, 10, 9): '國慶日補假',
    # Emergency closure announced on Oct 22, absent from the annual schedule.
    # https://www.twfhcsec.com.tw/News/NewsContent?ID=1502&List=%E6%9C%80%E6%96%B0%E6%B6%88%E6%81%AF
    date(2026, 10, 23): '薔琵颱風臨時休市',
    date(2026, 10, 26): '光復節補假',
    date(2026, 12, 25): '行憲紀念日',
}

_EXCEPTION_SOURCES = {
    date(2026, 10, 23): 'https://www.twfhcsec.com.tw/News/NewsContent?ID=1502&List=%E6%9C%80%E6%96%B0%E6%B6%88%E6%81%AF',
}


def twse_closed_reason(day):
    if day.weekday() >= 5:
        return '週末'
    return _CLOSED_2026.get(day)


def previous_trading_day(day: date) -> date:
    """Skip weekends and documented exchange holidays before a date."""
    candidate = day - timedelta(days=1)
    while twse_closed_reason(candidate):
        candidate -= timedelta(days=1)
    return candidate


def trading_day_context(day: date) -> dict:
    previous = previous_trading_day(day)
    skipped = []
    candidate = previous + timedelta(days=1)
    while candidate < day:
        row = {'date': candidate.isoformat(), 'reason': twse_closed_reason(candidate)}
        if candidate in _EXCEPTION_SOURCES:
            row['source_url'] = _EXCEPTION_SOURCES[candidate]
        skipped.append(row)
        candidate += timedelta(days=1)
    return {'as_of_date': day.isoformat(),
            'previous_trading_date': previous.isoformat(),
            'skipped_closed_dates': skipped}
