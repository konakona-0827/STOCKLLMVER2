"""Read-only Yahoo Japan daily observations; JPX calendar is independent of quotes.

The Yahoo chart endpoint is an unofficial, replaceable research source. Its OHLC
adjustment semantics, ordinary-share classification, events and tradability are
not certified by this adapter. No account, broker or order interface is present.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from .contracts import DataError, digest, strict_json_loads, timestamp

JST = timezone(timedelta(hours=9), "Asia/Tokyo")
BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"
CALENDAR_URL = "https://www.jpx.co.jp/english/corporate/about-jpx/calendar/index.html"
NATIONAL_HOLIDAYS_URL = "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv"
CALENDAR_VERSION = "jpx-cash-2025-2027-verified-20261028"
# Date lists checked against JPX (2026/2027) and Cabinet Office CSV (2025).
# Jan 2, Jan 3 and Dec 31 are additional JPX cash-market holidays.
_CLOSED = {
    2025: "01-01 01-02 01-03 01-13 02-11 02-23 02-24 03-20 04-29 05-03 05-04 05-05 05-06 07-21 08-11 09-15 09-23 10-13 11-03 11-23 11-24 12-31",
    2026: "01-01 01-02 01-03 01-12 02-11 02-23 03-20 04-29 05-03 05-04 05-05 05-06 07-20 08-11 09-21 09-22 09-23 10-12 11-03 11-23 12-31",
    2027: "01-01 01-02 01-03 01-11 02-11 02-23 03-21 03-22 04-29 05-03 05-04 05-05 07-19 08-11 09-20 09-23 10-11 11-03 11-23 12-31",
}
OBSERVATION_BLOCKERS = [
    "YAHOO_UNOFFICIAL_SOURCE", "CORPORATE_ACTIONS_UNVERIFIED", "TRADABILITY_UNVERIFIED",
    "COMMON_STOCK_CLASSIFICATION_UNVERIFIED", "TURNOVER_ESTIMATED_CLOSE_TIMES_VOLUME",
    "CURRENT_RETRIEVAL_NOT_HISTORICAL_PIT", "COMPANY_EVENTS_UNVERIFIED",
]


@dataclass(frozen=True)
class Document:
    url: str
    payload: object
    fetched_at: str
    sha256: str
    cache_file: str
    from_cache: bool = False

    def provenance(self):
        return {"url": self.url, "fetched_at": self.fetched_at, "sha256": self.sha256,
                "cache_file": self.cache_file, "from_cache": self.from_cache}


class YahooClient:
    """One bounded HTTP call per symbol; archive responses, never reuse a cache.

    A saved response is audit material, not a fallback on timeout/rate limiting.
    ``opener`` and ``now`` are injectable to exercise failure and cutoff cases.
    """

    def __init__(self, cache_dir, *, timeout=15, opener=None, now=None):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 30:
            raise DataError("YAHOO_TIMEOUT_MUST_BE_1_TO_30")
        self.cache_dir = Path(cache_dir)
        self.timeout = timeout
        self.opener = opener or urlopen
        self.now = now or (lambda: datetime.now(JST))

    def get(self, url, *, refresh=True):
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 SupportDeskJapan/0.1 (read-only research)",
                                        "Accept": "application/json"})
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise DataError("YAHOO_RESPONSE_TOO_LARGE")
            raw_text = raw.decode("utf-8")
            payload = strict_json_loads(raw_text)
        except HTTPError as exc:
            raise DataError(f"YAHOO_HTTP_{exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise DataError("YAHOO_NETWORK_UNAVAILABLE") from None
        except DataError:
            raise
        except (UnicodeError, ValueError):
            raise DataError("YAHOO_INVALID_JSON") from None
        fetched_at = self.now().isoformat()
        checksum = hashlib.sha256(raw).hexdigest()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache = self.cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:20] + "-" + uuid4().hex + ".json")
        envelope = {"url": url, "fetched_at": fetched_at, "sha256": checksum, "raw_json": raw_text}
        temporary = cache.with_suffix(".tmp")
        temporary.write_text(json.dumps(envelope, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temporary.replace(cache)
        return Document(url, payload, fetched_at, checksum, str(cache.resolve()))


def normalize_symbol(value):
    if not isinstance(value, str):
        raise DataError("JAPAN_SYMBOL_REQUIRED")
    symbol = value.strip().upper()
    if symbol.endswith(".T"):
        symbol = symbol[:-2]
    # JPX permits letters from 2024. Exclude ambiguous/unsupported suffixes.
    if not re.fullmatch(r"[1-9][0-9A-Z]{3}", symbol):
        raise DataError("JAPAN_TOKYO_SYMBOL_REQUIRED")
    return symbol + ".T"


def holidays_for_year(year):
    if year not in _CLOSED:
        raise DataError("JPX_CALENDAR_YEAR_UNAVAILABLE:" + str(year))
    return {str(year) + "-" + value for value in _CLOSED[year].split()}


def sessions_between(start, end):
    if start > end:
        raise DataError("JPX_CALENDAR_INVALID_RANGE")
    result = []
    for year in range(start.year, end.year + 1):
        holidays_for_year(year)  # Refuse unsupported years, including weekend-only ranges.
    current = start
    while current <= end:
        if current.weekday() < 5 and current.isoformat() not in holidays_for_year(current.year):
            result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def expected_completed_session(as_of):
    local = as_of.astimezone(JST)
    holidays_for_year(local.year)
    # 30-minute publication guard after the 15:30 close, rather than using an
    # in-progress or not-yet-published Yahoo daily candle as a completed bar.
    candidate = local.date() if local.time() >= time(16) else local.date() - timedelta(days=1)
    for _ in range(370):
        if candidate.weekday() < 5 and candidate.isoformat() not in holidays_for_year(candidate.year):
            return candidate.isoformat()
        candidate -= timedelta(days=1)
    raise DataError("JPX_EXPECTED_SESSION_NOT_FOUND")


def _number(value, *, zero=False):
    if type(value) not in (float, int) or not math.isfinite(value) or value < 0 or (not zero and value == 0):
        raise DataError("YAHOO_INVALID_NUMBER")
    return float(value)


def parse_chart(payload, symbol, document, *, expected_latest, sessions):
    """Validate identity and quote arrays before returning completed OHLC rows."""
    try:
        chart = payload["chart"]
        if chart.get("error") is not None:
            raise DataError("YAHOO_CHART_ERROR")
        results = chart["result"]
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            raise DataError("YAHOO_CHART_RESULT_REQUIRED")
        result = results[0]
        meta = result["meta"]
        if meta.get("symbol") != symbol:
            raise DataError("YAHOO_SYMBOL_MISMATCH")
        if (meta.get("currency") != "JPY" or meta.get("exchangeTimezoneName") != "Asia/Tokyo"
                or meta.get("exchangeName") not in {"JPX", "TYO", "TSE"}
                or meta.get("instrumentType") != "EQUITY"
                or meta.get("dataGranularity") != "1d"):
            raise DataError("YAHOO_JAPAN_EQUITY_METADATA_REQUIRED")
        if "gmtoffset" in meta and meta["gmtoffset"] != 32400:
            raise DataError("YAHOO_TIMEZONE_OFFSET_MISMATCH")
        stamps = result["timestamp"]
        quote_arrays = result["indicators"]["quote"]
        if not isinstance(stamps, list) or not stamps or len(stamps) > 400:
            raise DataError("YAHOO_INVALID_TIMESTAMP_ARRAY")
        if not isinstance(quote_arrays, list) or len(quote_arrays) != 1 or not isinstance(quote_arrays[0], dict):
            raise DataError("YAHOO_INVALID_QUOTE_ARRAY")
        quote = quote_arrays[0]
        for key in ("open", "high", "low", "close", "volume"):
            if not isinstance(quote.get(key), list) or len(quote[key]) != len(stamps):
                raise DataError("YAHOO_QUOTE_LENGTH_MISMATCH")
        if any(type(stamp) not in (int, float) or not math.isfinite(stamp) for stamp in stamps):
            raise DataError("YAHOO_INVALID_TIMESTAMP")
        if stamps != sorted(set(stamps)):
            raise DataError("YAHOO_DUPLICATE_OR_UNSORTED_TIMESTAMPS")
        allowed = set(sessions)
        bars = []
        for index, stamp in enumerate(stamps):
            day = datetime.fromtimestamp(stamp, JST).date().isoformat()
            if day > expected_latest:
                continue
            if day not in allowed:
                raise DataError("YAHOO_NON_JPX_OR_OUT_OF_RANGE_SESSION")
            prices = {key: _number(quote[key][index]) for key in ("open", "high", "low", "close")}
            if not prices["low"] <= min(prices["open"], prices["close"]) <= max(prices["open"], prices["close"]) <= prices["high"]:
                raise DataError("YAHOO_INVALID_OHLC")
            volume = _number(quote["volume"][index], zero=True)
            if not volume.is_integer():
                raise DataError("YAHOO_VOLUME_NOT_INTEGER_SHARES")
            bars.append({"session": day, "closed_at": day + "T15:30:00+09:00", "complete": True,
                **prices, "volume": int(volume), "turnover_jpy": prices["close"] * volume,
                "turnover_estimated": True, "turnover_method": "close_times_volume",
                "halted": volume == 0, "limit_locked": prices["high"] == prices["low"],
                "limit_lock_assessment": "FLAT_BAR_PROXY_ONLY_NOT_VERIFIED",
                "retrieved_at": document.fetched_at, "source_url": document.url,
                "source_sha256": document.sha256})
        dates = [bar["session"] for bar in bars]
        if not dates or dates != sorted(set(dates)):
            raise DataError("YAHOO_DUPLICATE_OR_EMPTY_SESSIONS")
        if dates != [day for day in sessions if day >= dates[0]] or dates[-1] != expected_latest:
            raise DataError("MISSING_OR_STALE_STOCK_SESSIONS")
        events = result.get("events", {})
        if not isinstance(events, dict):
            raise DataError("YAHOO_INVALID_EVENTS")
        visible_events = []
        for category in ("splits", "dividends", "capitalGains"):
            rows = events.get(category, {})
            if not isinstance(rows, dict):
                raise DataError("YAHOO_INVALID_EVENTS")
            for event in rows.values():
                if not isinstance(event, dict) or type(event.get("date")) not in (float, int):
                    raise DataError("YAHOO_INVALID_EVENT_DATE")
                event_day = datetime.fromtimestamp(_number(event["date"]), JST).date().isoformat()
                if dates[0] <= event_day <= expected_latest:
                    visible_events.append({"type": category, "session": event_day, "reported": event})
                    if category in {"splits", "capitalGains"}:
                        raise DataError("CORPORATE_ACTION_IN_WINDOW_REQUIRES_REVIEW")
        if any(bar["volume"] == 0 for bar in bars):
            raise DataError("HALT_OR_ZERO_VOLUME_REQUIRES_REVIEW")
        if len(bars) < 60:
            raise DataError("INSUFFICIENT_60_SESSION_HISTORY")
        return bars, {"name": str(meta.get("longName") or meta.get("shortName") or symbol),
            "yahoo_events": visible_events, "yahoo_metadata": {key: meta.get(key) for key in
                ("symbol", "exchangeName", "exchangeTimezoneName", "currency", "instrumentType", "dataGranularity")}}
    except (KeyError, TypeError, IndexError, AttributeError, OverflowError, OSError, ValueError) as exc:
        if isinstance(exc, DataError):
            raise
        raise DataError("YAHOO_MALFORMED_CHART") from None


def build_snapshot(symbols, cache_dir, *, months=8, timeout=15, client=None, progress=None):
    if type(months) is not int or not 4 <= months <= 12:
        raise DataError("JAPAN_MONTHS_MUST_BE_4_TO_12")
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 100:
        raise DataError("JAPAN_UNIVERSE_MUST_BE_1_TO_100")
    normalized = []
    for item in symbols:
        if isinstance(item, dict):
            if item.get("exchange", "TSE") != "TSE":
                raise DataError("JAPAN_TOKYO_EQUITY_ONLY")
            item = item.get("symbol")
        symbol = normalize_symbol(item)
        if symbol in normalized:
            raise DataError("JAPAN_DUPLICATE_UNIVERSE_SYMBOL")
        normalized.append(symbol)
    client = client or YahooClient(cache_dir, timeout=timeout)
    started = client.now().astimezone(JST)
    expected_latest = expected_completed_session(started)
    end_day = date.fromisoformat(expected_latest)
    start_day = end_day.replace(day=1)
    for _ in range(months - 1):
        start_day = (start_day - timedelta(days=1)).replace(day=1)
    sessions = sessions_between(start_day, end_day)
    errors, sources, securities = [], [], []
    for symbol in normalized:
        query = urlencode({"period1": int(datetime.combine(start_day, time(), JST).timestamp()),
            "period2": int(datetime.combine(started.date() + timedelta(days=1), time(), JST).timestamp()),
            "interval": "1d", "events": "div,splits,capitalGains", "includeAdjustedClose": "true"})
        url = BASE + symbol + "?" + query
        item_errors, bars, info, stock_sources = [], [], {}, []
        try:
            if progress:
                progress(url)
            document = client.get(url, refresh=True)
            stock_sources.append(document.provenance())
            sources.append(document.provenance())
            bars, info = parse_chart(document.payload, symbol, document,
                expected_latest=expected_latest, sessions=sessions)
        except DataError as exc:
            item_errors.append(str(exc))
            errors.append("SYMBOL_COLLECTION_FAILED:" + symbol)
        securities.append({"symbol": symbol, "name": info.get("name", symbol), "exchange": "TSE",
            "currency": "JPY", "instrument_type": "COMMON_STOCK", "interval": "1d",
            "source": "YAHOO_CHART_UNOFFICIAL_DAILY", "volume_unit": "shares",
            "price_basis": "raw_unverified", "actions_reviewed": False, "volume_comparable": False,
            "tradability_reviewed": False, "point_in_time": False, "observation_only": True,
            "instrument_classification": "YAHOO_EQUITY_NOT_INDEPENDENTLY_VERIFIED",
            "price_basis_detail": "YAHOO_OHLC_ADJUSTMENT_SEMANTICS_UNVERIFIED_NO_ADJCLOSE_MIXING",
            "turnover_basis": "estimated_close_times_volume",
            "turnover_estimated": True, "turnover_method": "close_times_volume", "lot_size": 100,
            "observation_blockers": list(OBSERVATION_BLOCKERS), "dossier": {}, **info,
            "collection_status": "ERROR" if item_errors else "OK", "collection_errors": item_errors,
            "source_documents": stock_sources, "collected_bar_count": len(bars), "bars": bars})
    assembled = client.now().astimezone(JST)
    if assembled.date() != started.date() or expected_completed_session(assembled) != expected_latest:
        raise DataError("JAPAN_COLLECTION_CROSSED_SESSION_CUTOFF_RETRY")
    if assembled < started:
        raise DataError("JAPAN_CLOCK_MOVED_BACKWARDS")
    for item in securities:
        item["adjustment_as_of"] = assembled.isoformat()
        for bar in item["bars"]:
            # Current retrieval time is honest availability; never synthesize a
            # historical publication timestamp for a quote downloaded today.
            bar["available_at"] = assembled.isoformat()
    actual = [item["bars"][-1]["session"] for item in securities if item["bars"]]
    holiday_dates = sorted({day for year in range(start_day.year, assembled.year + 1)
                            for day in holidays_for_year(year)})
    return {"schema_version": 1, "market": "JP", "timezone": "Asia/Tokyo", "currency": "JPY",
        "synthetic": False, "observation_only": True, "as_of": assembled.isoformat(),
        "universe_version": "japan-observation-" + digest(normalized)[:12],
        "calendar": {"source": "JPX_VERSIONED_CASH_CALENDAR", "version": CALENDAR_VERSION,
            "source_url": CALENDAR_URL, "supplementary_source_url": NATIONAL_HOLIDAYS_URL,
            "available_at": assembled.isoformat(), "coverage_through": assembled.date().isoformat(),
            "sessions": sessions, "timezone": "Asia/Tokyo", "session_close": "15:30",
            "publication_guard": "16:00", "annual_closed_dates": holiday_dates,
            "unscheduled_closures_handling": "MISSING_EXPECTED_QUOTE_BLOCKS_SYMBOL_NOT_INFERRED_HOLIDAY"},
        "market_context": {"source": "JPX_CALENDAR_ONLY_NO_MARKET_INDEX", "available_at": assembled.isoformat(),
            "stress": "UNKNOWN", "latest_session": max(actual) if actual else None},
        "securities": securities,
        "collection": {"status": "DEGRADED" if errors else "COMPLETE", "started_at": started.isoformat(),
            "finished_at": assembled.isoformat(), "expected_latest_session": expected_latest,
            "actual_latest_session": max(actual) if actual else None, "months": months,
            "requested_symbols": normalized, "successful_symbols": [item["symbol"] for item in securities
                if item["collection_status"] == "OK"], "errors": errors, "sources": sources,
            "limitations": list(OBSERVATION_BLOCKERS) + ["VERSIONED_CALENDAR_NO_UNSCHEDULED_CLOSURE_FEED",
                "NO_HISTORICAL_POINT_IN_TIME_CLAIM", "NO_MARKET_INDEX_STRESS_REVIEW"]}}
