from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time

# Windows installations need no external tzdata for these fixed-offset timestamps.
from datetime import timedelta, timezone

TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")


class DataError(ValueError):
    pass


def strict_json_loads(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise DataError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def nonfinite(value):
        raise DataError("NON_FINITE_JSON_NUMBER")

    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise DataError("NON_FINITE_JSON_NUMBER")
        return result

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite, parse_float=finite_float)


def timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timezone required")
        return result
    except (AttributeError, TypeError, ValueError) as exc:
        raise DataError("INVALID_AWARE_TIMESTAMP") from exc


def number(value, field: str, *, zero: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (not zero and value == 0):
        raise DataError("INVALID_NUMBER:" + field)
    return float(value)


def digest(value) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Rules:
    version: str = "support-retest-v0.2"
    min_bars: int = 60
    atr_window: int = 14
    pivot_left: int = 2
    pivot_right: int = 2
    high_window: int = 20
    min_drawdown: float = 0.08
    zone_atr: float = 0.35
    zone_price: float = 0.008
    bounce_atr: float = 1.5
    min_retest_separation: int = 3
    episode_lifetime: int = 40
    confirmation_lifetime: int = 10
    reset_after_break: int = 10
    max_distance_atr: float = 2.5
    min_turnover_twd: float = 20_000_000
    event_buffer_days: int = 3

    def __post_init__(self):
        integer_fields = ("min_bars", "atr_window", "pivot_left", "pivot_right", "high_window",
                          "min_retest_separation", "episode_lifetime", "confirmation_lifetime",
                          "reset_after_break", "event_buffer_days")
        for name in integer_fields:
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("INVALID_RULE:" + name)
        for name in ("min_drawdown", "zone_atr", "zone_price", "bounce_atr", "max_distance_atr", "min_turnover_twd"):
            number(getattr(self, name), name)
        if self.min_drawdown >= 1 or self.zone_price >= 1 or self.min_bars < max(self.atr_window + 1, self.high_window + self.pivot_right + 1):
            raise ValueError("INCONSISTENT_RULES")


@dataclass(frozen=True)
class Bar:
    session: str
    closed_at: str
    available_at: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover_twd: float
    halted: bool = False
    limit_locked: bool = False


@dataclass(frozen=True)
class Series:
    symbol: str
    name: str
    exchange: str
    source: str
    price_basis: str
    bars: tuple[Bar, ...]
    metadata: dict


def calendar_at(bundle: dict, as_of: datetime) -> list[str]:
    calendar = bundle.get("calendar", {})
    if not isinstance(calendar, dict):
        raise DataError("INVALID_CALENDAR")
    if not calendar.get("source") or timestamp(calendar.get("available_at")) > as_of:
        raise DataError("CALENDAR_NOT_AVAILABLE")
    try:
        coverage = date.fromisoformat(calendar["coverage_through"])
        raw = calendar["sessions"]
        if not isinstance(raw, list) or not raw or any(not isinstance(s, str) for s in raw):
            raise ValueError()
        parsed = [date.fromisoformat(s) for s in raw]
        if parsed != sorted(set(parsed)) or coverage < as_of.astimezone(TAIPEI).date():
            raise ValueError()
    except (KeyError, TypeError, ValueError) as exc:
        raise DataError("INVALID_OR_INCOMPLETE_CALENDAR") from exc
    # Data must explicitly mark a session complete. A calendar date alone cannot certify a bar.
    return [s for s in raw if datetime.combine(date.fromisoformat(s), time(13, 30), TAIPEI) <= as_of]


def load_series(item: dict, as_of: datetime, calendar: list[str], *, allow_observation: bool = False) -> Series:
    if not isinstance(item, dict):
        raise DataError("INVALID_SERIES")
    symbol = item.get("symbol", "")
    if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{4,6}[A-Z]?", symbol):
        raise DataError("TAIWAN_SYMBOL_REQUIRED")
    if item.get("exchange") not in {"TWSE", "TPEX"} or item.get("currency") != "TWD" or item.get("instrument_type") != "COMMON_STOCK":
        raise DataError("TAIWAN_COMMON_STOCK_ONLY")
    if item.get("interval") != "1d" or not item.get("source") or item.get("volume_unit") != "shares":
        raise DataError("DAILY_SOURCE_AND_SHARE_UNITS_REQUIRED")
    observation = allow_observation and item.get("observation_only") is True
    bases = {"adjusted_ohlc", "raw_no_actions_in_window"} | ({"raw_unverified"} if observation else set())
    if item.get("price_basis") not in bases or (not observation and item.get("actions_reviewed") is not True):
        raise DataError("CORPORATE_ACTION_BASIS_UNVERIFIED")
    if not item.get("adjustment_as_of") or timestamp(item["adjustment_as_of"]) > as_of:
        raise DataError("ADJUSTMENT_NOT_AVAILABLE_AT_CUTOFF")
    if item.get("volume_comparable") is not True and not observation:
        raise DataError("VOLUME_NOT_COMPARABLE")
    if item.get("tradability_reviewed") is not True and not observation:
        raise DataError("TRADABILITY_UNKNOWN")
    raw_bars = item.get("bars")
    if not isinstance(raw_bars, list):
        raise DataError("BARS_REQUIRED")
    selected = []
    for row in raw_bars:
        if not isinstance(row, dict):
            raise DataError("INVALID_BAR")
        # Future data may be in a replay file. It is never used or hashed into the visible snapshot.
        if timestamp(row.get("closed_at")) > as_of or timestamp(row.get("available_at")) > as_of:
            continue
        if row.get("complete") is not True:
            raise DataError("INCOMPLETE_BAR")
        closed, available = timestamp(row["closed_at"]), timestamp(row["available_at"])
        try:
            session = date.fromisoformat(row["session"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DataError("INVALID_SESSION") from exc
        if (closed.astimezone(TAIPEI).date() != session or closed.astimezone(TAIPEI).time() < time(13, 30)
                or closed > available):
            raise DataError("INVALID_BAR_AVAILABILITY")
        prices = {key: number(row.get(key), key) for key in ("open", "high", "low", "close")}
        if not (prices["low"] <= min(prices["open"], prices["close"]) <= max(prices["open"], prices["close"]) <= prices["high"]):
            raise DataError("INVALID_OHLC")
        for flag in ("halted", "limit_locked"):
            if type(row.get(flag)) is not bool:
                raise DataError("TRADABILITY_FLAGS_REQUIRED")
        selected.append(Bar(session.isoformat(), row["closed_at"], row["available_at"], **prices,
                            volume=number(row.get("volume"), "volume", zero=True),
                            turnover_twd=number(row.get("turnover_twd"), "turnover_twd", zero=True),
                            halted=row["halted"], limit_locked=row["limit_locked"]))
    dates = [b.session for b in selected]
    if dates != sorted(set(dates)):
        raise DataError("UNSORTED_OR_DUPLICATE_SESSIONS")
    availability = [timestamp(b.available_at) for b in selected]
    if availability != sorted(availability):
        raise DataError("NON_MONOTONIC_AVAILABILITY_REQUIRES_NEW_SNAPSHOT")
    if not selected:
        raise DataError("NO_AVAILABLE_BARS")
    expected = [s for s in calendar if s >= dates[0]]
    if dates != expected:
        raise DataError("MISSING_OR_STALE_SESSIONS")
    if any(b.halted or b.volume == 0 or b.turnover_twd == 0 for b in selected):
        raise DataError("HALT_OR_ZERO_VOLUME_REQUIRES_NEW_SEGMENT")
    metadata = {key: item.get(key) for key in ("currency", "instrument_type", "interval", "volume_unit",
                "actions_reviewed", "adjustment_as_of", "volume_comparable", "tradability_reviewed", "point_in_time",
                "observation_only", "observation_blockers", "collection_status", "collection_errors")}
    return Series(symbol, str(item.get("name", symbol)), item["exchange"], item["source"],
                  item["price_basis"], tuple(selected), metadata)


def visible_series(series: Series) -> dict:
    result = asdict(series)
    result["bars"] = [asdict(bar) for bar in series.bars]
    return result
