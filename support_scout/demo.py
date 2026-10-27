"""Explicitly synthetic fixtures. Codes 9001..9006 here are scenario IDs, not stock recommendations."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta


def demo_bundle() -> dict:
    start = date(2026, 6, 1)
    sessions = []
    day = start
    while len(sessions) < 72:
        if day.weekday() < 5:  # Fixture-only calendar; never used for actual Taiwan sessions.
            sessions.append(day.isoformat())
        day += timedelta(days=1)
    cutoff = sessions[-1] + "T16:00:00+08:00"
    bars = []
    for i, session in enumerate(sessions):
        close = 120 + (i % 3) * 0.2
        op, high, low, volume = close - 0.2, close + 1, close - 1, 1_000_000
        # Unique trough at session 62; two right sessions reveal it at session 64.
        special = {
            59: (116, 117, 113, 114, 1_400_000),
            60: (113, 114, 109, 110, 1_400_000),
            61: (109, 110, 104, 105, 1_500_000),
            62: (103, 104, 100, 102, 1_800_000),
            63: (102, 105, 101, 104, 1_000_000),
            64: (104, 107, 103, 106, 1_000_000),
            65: (106, 112, 105, 111, 1_500_000),
            66: (110, 111, 107, 108, 1_000_000),
            67: (104, 105, 100.5, 103, 600_000),
            68: (104, 107, 103, 106, 1_100_000),
            69: (106, 108, 104, 107, 1_000_000),
            70: (107, 108, 104.5, 106, 900_000),
            71: (106, 108, 104, 107, 900_000),
        }
        if i in special:
            op, high, low, close, volume = special[i]
        bars.append({"session": session, "closed_at": session + "T13:33:00+08:00",
                     "available_at": session + "T15:00:00+08:00", "complete": True,
                     "open": op, "high": high, "low": low, "close": close, "volume": volume,
                     "turnover_twd": close * volume, "halted": False, "limit_locked": False})
    template = {"symbol": "9001", "name": "合成：回測後站回", "exchange": "TWSE", "currency": "TWD",
                "instrument_type": "COMMON_STOCK", "interval": "1d", "source": "SYNTHETIC_FIXTURE_NOT_MARKET_DATA",
                "volume_unit": "shares", "volume_comparable": True, "tradability_reviewed": True,
                "price_basis": "raw_no_actions_in_window", "actions_reviewed": True,
                "adjustment_as_of": sessions[0] + "T08:00:00+08:00", "point_in_time": True,
                "bars": bars, "dossier": {"source": "SYNTHETIC_DOSSIER", "available_at": cutoff,
                    "thesis_status": "INTACT", "events_reviewed_through": (day + timedelta(days=7)).isoformat() + "T23:00:00+08:00", "events": []}}
    securities = [template]
    for code, name in (("9002", "反彈但未回測"), ("9003", "站回後失效"), ("9004", "型態成立但遠離支撐"),
                       ("9005", "事件資訊未知"), ("9006", "公司行動未核對")):
        item = deepcopy(template)
        item.update(symbol=code, name="合成：" + name)
        if code == "9002":
            for row in item["bars"][67:]:
                row.update(open=110, high=113, low=108, close=111)
        if code == "9003":
            item["bars"][-1].update(open=98, high=99, low=95, close=96)
        if code == "9004":
            item["bars"][-1].update(open=115, high=119, low=114, close=118)
        if code == "9005":
            item["dossier"] = {}
        if code == "9006":
            item["actions_reviewed"] = False
        securities.append(item)
    return {"schema_version": 1, "as_of": cutoff, "universe_version": "synthetic-scenarios-v1", "synthetic": True,
            "calendar": {"source": "SYNTHETIC_WEEKDAYS_ONLY_NOT_TWSE_CALENDAR", "available_at": sessions[0] + "T08:00:00+08:00",
                         "coverage_through": sessions[-1], "sessions": sessions},
            "market_context": {"source": "SYNTHETIC_MARKET", "available_at": cutoff, "stress": "ELEVATED"},
            "securities": securities}
