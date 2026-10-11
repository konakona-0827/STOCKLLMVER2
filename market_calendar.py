"""TWSE market closures that affect intraday quote and order decisions.

Source: https://www.twse.com.tw/zh/trading/holiday.html (115 / 2026 calendar).
Live quote freshness is checked separately, including for dates not listed here.
"""
from datetime import date


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
    date(2026, 10, 26): '光復節補假',
    date(2026, 12, 25): '行憲紀念日',
}


def twse_closed_reason(day):
    if day.weekday() >= 5:
        return '週末'
    return _CLOSED_2026.get(day)
