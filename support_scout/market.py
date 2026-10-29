"""動態 TWSE 市場清單與單日行情；不預選研究標的或讀取固定觀察池。"""
from __future__ import annotations

from datetime import date, timedelta
import math
import re
from urllib.parse import urlencode

from .contracts import DataError, TAIPEI, digest, timestamp
from .twse import (HOLIDAY_BASE, REGISTRY_URL, TwseClient,
                   expected_completed_session, numeric, parse_holidays, parse_registry)

DAILY_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
DAILY_FIELDS = {"Date", "Code", "Name", "TradeVolume", "TradeValue", "OpeningPrice",
                "HighestPrice", "LowestPrice", "ClosingPrice", "Change", "Transaction"}


def _compact_date(value) -> date:
    """The OpenAPI feeds use a seven-digit ROC date, unlike monthly report dates."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{7}", value):
        raise DataError("MARKET_INVALID_SESSION_DATE")
    try:
        return date(int(value[:3]) + 1911, int(value[3:5]), int(value[5:]))
    except ValueError:
        raise DataError("MARKET_INVALID_SESSION_DATE") from None


def _signed_number(value) -> float:
    if type(value) not in (str, int, float):
        raise DataError("MARKET_INVALID_CHANGE")
    try:
        result = float(str(value).replace(",", "").strip())
    except ValueError:
        raise DataError("MARKET_INVALID_CHANGE") from None
    if not math.isfinite(result):
        raise DataError("MARKET_INVALID_CHANGE")
    return result


def _quantity(value) -> int:
    result = numeric(value, zero=True)
    if not result.is_integer():
        raise DataError("MARKET_NON_INTEGER_QUANTITY")
    return int(result)


def _security(row: dict, listing: dict, session: str) -> dict:
    prices = {name: numeric(row[field]) for name, field in (
        ("open", "OpeningPrice"), ("high", "HighestPrice"),
        ("low", "LowestPrice"), ("close", "ClosingPrice"))}
    if not prices["low"] <= min(prices["open"], prices["close"]) <= max(prices["open"], prices["close"]) <= prices["high"]:
        raise DataError("MARKET_INVALID_OHLC")
    volume = _quantity(row["TradeVolume"])
    turnover = numeric(row["TradeValue"], zero=True)
    transactions = _quantity(row["Transaction"])
    if not volume or not turnover or not transactions:
        raise DataError("MARKET_NO_VERIFIED_TRADING_ACTIVITY")
    change = _signed_number(row["Change"])
    previous_close = prices["close"] - change
    if not math.isfinite(previous_close) or previous_close <= 0:
        raise DataError("MARKET_INVALID_PREVIOUS_CLOSE")
    return {"symbol": row["Code"], "name": listing["name"],
            "industry": listing["industry"], "session": session,
            "close": prices["close"], "volume_shares": volume,
            "turnover_twd": turnover, "change_pct": change / previous_close * 100}


def build_market_snapshot(cache_dir, *, timeout=15, client=None) -> dict:
    """Join the current company registry with official daily data, without a top-N.

    Every eligible stock is available to a discovery agent's market tools. A
    missing or unusable quote stays visible in coverage, not as a made-up zero.
    A one-session snapshot cannot establish support or executable tradability.
    """
    client = client or TwseClient(cache_dir, timeout=timeout)
    started = timestamp(client.now().isoformat()).astimezone(TAIPEI)
    sources, errors = [], []

    def fetch(url, *, refresh=False):
        document = client.get(url, refresh=refresh)
        if (document.url != url or not isinstance(document.sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", document.sha256)):
            raise DataError("MARKET_INVALID_SOURCE_PROVENANCE")
        timestamp(document.fetched_at)
        sources.append(document.provenance())
        return document

    registry_doc = fetch(REGISTRY_URL)
    registry = parse_registry(registry_doc.payload)
    for row in registry_doc.payload:
        symbol = row["公司代號"]
        if not isinstance(symbol, str):
            raise DataError("MARKET_REGISTRY_METADATA_INVALID")
        if symbol not in registry:
            continue
        if (not isinstance(row["公司簡稱"], str) or not row["公司簡稱"].strip()
                or not isinstance(row.get("產業別"), str) or not row["產業別"].strip()):
            raise DataError("MARKET_REGISTRY_METADATA_INVALID")
        registry[symbol]["industry"] = row["產業別"].strip()
        age = started.date() - date.fromisoformat(registry[symbol]["published_date"])
        if age < timedelta(0):
            raise DataError("MARKET_REGISTRY_DATE_IN_FUTURE")
        if age > timedelta(days=10):
            errors.append("MARKET_REGISTRY_STALE:" + symbol)

    daily_doc = fetch(DAILY_URL, refresh=True)
    daily = daily_doc.payload
    if not isinstance(daily, list) or not daily:
        raise DataError("MARKET_DAILY_REPORT_UNAVAILABLE")
    if any(not isinstance(row, dict) or not DAILY_FIELDS <= set(row) for row in daily):
        raise DataError("MARKET_DAILY_FIELDS_CHANGED")
    sessions = {_compact_date(row["Date"]) for row in daily}
    if len(sessions) != 1:
        raise DataError("MARKET_MIXED_DAILY_SESSIONS")
    actual_day = next(iter(sessions))
    codes = [row["Code"] for row in daily]
    if any(not isinstance(code, str) or not code.strip() for code in codes):
        raise DataError("MARKET_INVALID_SECURITY_CODE")
    if len(codes) != len(set(codes)):
        raise DataError("MARKET_DUPLICATE_DAILY_SYMBOL")

    holiday_sets = {}

    def holidays_for_year(year):
        if year not in holiday_sets:
            url = HOLIDAY_BASE + "?" + urlencode({"response": "json", "queryYear": year - 1911})
            try:
                document = fetch(url, refresh=True)
                holiday_sets[year] = set(parse_holidays(document.payload, year))
            except DataError:
                raise DataError("MARKET_HOLIDAY_YEAR_UNAVAILABLE:" + str(year)) from None
        return holiday_sets[year]

    holidays_for_year(started.year)
    expected = expected_completed_session(started, holidays_for_year)
    if actual_day.isoformat() > expected:
        raise DataError("MARKET_SESSION_NOT_COMPLETED")
    if actual_day.weekday() >= 5 or actual_day.isoformat() in holidays_for_year(actual_day.year):
        raise DataError("MARKET_SESSION_ON_CLOSED_DAY")
    if actual_day.isoformat() != expected:
        errors.append("MARKET_LATEST_SESSION_MISSING_OR_UNSCHEDULED_CLOSURE")

    missing = sorted(set(registry) - set(codes))
    invalid, securities = [], []
    for row in daily:
        symbol = row["Code"]
        if symbol not in registry:
            continue
        try:
            securities.append(_security(row, registry[symbol], actual_day.isoformat()))
        except (DataError, KeyError, TypeError, ValueError) as exc:
            invalid.append(symbol)
            code = str(exc) if isinstance(exc, DataError) else type(exc).__name__
            errors.append("MARKET_INVALID_SECURITY:" + symbol + ":" + code)
    if not securities:
        raise DataError("MARKET_NO_VALID_COMMON_STOCK_QUOTES")
    if missing:
        errors.append("MARKET_COMMON_STOCK_QUOTES_MISSING")

    assembled = timestamp(client.now().isoformat()).astimezone(TAIPEI)
    if (assembled < started or assembled.date() != started.date()
            or expected_completed_session(assembled, holidays_for_year) != expected):
        raise DataError("MARKET_COLLECTION_CROSSED_CUTOFF_RETRY")
    if any(timestamp(source["fetched_at"]) > assembled for source in sources):
        raise DataError("MARKET_SOURCE_TIMESTAMP_IN_FUTURE")
    result = {"schema_version": 1, "market": "TWSE", "synthetic": False,
        "as_of": assembled.isoformat(), "session": actual_day.isoformat(),
        "expected_session": expected, "securities": sorted(securities, key=lambda row: row["symbol"]),
        "sources": sources,
        "coverage": {"registry_count": len(registry), "daily_count": len(daily),
            "eligible_count": len(securities), "missing_symbols": missing,
            "invalid_symbols": sorted(invalid),
            "excluded_non_registry_count": sum(symbol not in registry for symbol in codes)},
        "status": "DEGRADED" if errors else "COMPLETE", "errors": sorted(set(errors)),
        "limitations": ["TWSE_LISTED_COMMON_STOCKS_ONLY", "END_OF_DAY_NOT_INTRADAY",
            "RAW_PRICES_CORPORATE_ACTIONS_UNVERIFIED", "NO_SUPPORT_CONFIRMATION_FROM_ONE_SESSION",
            "NO_HISTORICAL_POINT_IN_TIME_CLAIM", "NO_COMPANY_EVENT_REVIEW"]}
    result["catalog_id"] = digest(result)
    return result
