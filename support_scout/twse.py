"""唯讀 TWSE 收盤觀察資料；保留原始來源，不宣稱歷史 PIT 或公司行動已核實。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import time as clock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from .contracts import DataError, TAIPEI, digest, strict_json_loads, timestamp

BASE = "https://www.twse.com.tw/exchangeReport/"
REGISTRY_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
HOLIDAY_BASE = "https://www.twse.com.tw/holidaySchedule/holidaySchedule"
OBSERVATION_BLOCKERS = ["CORPORATE_ACTIONS_UNVERIFIED", "TRADABILITY_UNVERIFIED",
                        "CURRENT_RETRIEVAL_NOT_HISTORICAL_PIT"]


@dataclass(frozen=True)
class Document:
    url: str
    payload: object
    fetched_at: str
    sha256: str
    cache_file: str
    from_cache: bool = False
    raw_archive_file: str | None = None

    def provenance(self):
        result = {"url": self.url, "fetched_at": self.fetched_at, "sha256": self.sha256,
                  "cache_file": self.cache_file, "from_cache": self.from_cache}
        if self.raw_archive_file is not None:
            result["raw_archive_file"] = self.raw_archive_file
        return result


class TwseClient:
    """每個來源有限 timeout；快取校驗失敗會重新下載，絕不悄悄沿用過時檔。"""

    def __init__(self, cache_dir, *, timeout=15, max_cache_age_hours=24, min_interval=0.2,
                 opener=None, now=None):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 30:
            raise DataError("TWSE_TIMEOUT_MUST_BE_1_TO_30")
        self.cache_dir = Path(cache_dir)
        self.timeout = timeout
        self.max_cache_age = timedelta(hours=max_cache_age_hours)
        self.min_interval = min_interval
        self.opener = opener or urlopen
        self.now = now or (lambda: datetime.now(TAIPEI))
        self.last_request = 0.0

    def _archive_raw(self, raw, checksum):
        archive = self.cache_dir / "raw" / (checksum + ".json")
        if archive.is_file():
            if hashlib.sha256(archive.read_bytes()).hexdigest() != checksum:
                raise DataError("TWSE_RAW_ARCHIVE_INTEGRITY_FAILED")
        else:
            archive.parent.mkdir(parents=True, exist_ok=True)
            temporary = archive.with_name(archive.name + "." + uuid4().hex + ".tmp")
            temporary.write_bytes(raw)
            temporary.replace(archive)
        return str(archive.resolve())

    def get(self, url, *, refresh=False):
        cache = self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        if cache.is_file() and not refresh:
            try:
                envelope = strict_json_loads(cache.read_text(encoding="utf-8"))
                raw = envelope["raw_json"].encode("utf-8")
                age = self.now() - timestamp(envelope["fetched_at"])
                if (envelope["url"] == url and hashlib.sha256(raw).hexdigest() == envelope["sha256"]
                        and timedelta(0) <= age <= self.max_cache_age):
                    return Document(url, strict_json_loads(raw), envelope["fetched_at"],
                                    envelope["sha256"], str(cache.resolve()), True,
                                    self._archive_raw(raw, envelope["sha256"]))
            except (OSError, DataError, KeyError, TypeError, ValueError):
                pass
        pause = self.min_interval - (clock.monotonic() - self.last_request)
        if pause > 0:
            clock.sleep(pause)
        self.last_request = clock.monotonic()
        request = Request(url, headers={"User-Agent": "SupportDesk/0.4 (read-only market research)",
                                        "Accept": "application/json"})
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise DataError("TWSE_RESPONSE_TOO_LARGE")
            payload = strict_json_loads(raw)
            raw_text = raw.decode("utf-8")
        except HTTPError as exc:
            raise DataError(f"TWSE_HTTP_{exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise DataError("TWSE_NETWORK_UNAVAILABLE") from None
        except (UnicodeError, ValueError) as exc:
            raise DataError("TWSE_INVALID_JSON") from None
        fetched_at = self.now().isoformat()
        checksum = hashlib.sha256(raw).hexdigest()
        envelope = {"url": url, "fetched_at": fetched_at, "sha256": checksum, "raw_json": raw_text}
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_name(cache.name + "." + uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(envelope, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temporary.replace(cache)
        return Document(url, payload, fetched_at, checksum, str(cache.resolve()),
                        raw_archive_file=self._archive_raw(raw, checksum))


def roc_date(value):
    if not isinstance(value, str):
        raise DataError("TWSE_INVALID_DATE")
    try:
        pieces = value.strip().replace("-", "/").split("/")
        if len(pieces) != 3:
            raise ValueError()
        year, month, day = map(int, pieces)
        return date(year + 1911 if year < 1911 else year, month, day)
    except (TypeError, ValueError):
        raise DataError("TWSE_INVALID_DATE") from None


def numeric(value, *, zero=False):
    if type(value) not in (str, float, int):
        raise DataError("TWSE_INVALID_NUMBER")
    try:
        result = float(str(value).replace(",", "").strip())
    except ValueError:
        raise DataError("TWSE_INVALID_NUMBER") from None
    if not math.isfinite(result) or result < 0 or (not zero and result == 0):
        raise DataError("TWSE_INVALID_NUMBER")
    return result


def month_starts(day, count):
    result = []
    current = day.replace(day=1)
    for _ in range(count):
        result.append(current)
        current = (current - timedelta(days=1)).replace(day=1)
    return list(reversed(result))


def _table(payload, required):
    if not isinstance(payload, dict) or str(payload.get("stat", "")).lower() != "ok":
        raise DataError("TWSE_REPORT_NOT_OK")
    fields, rows = payload.get("fields"), payload.get("data")
    if (not isinstance(fields, list) or any(not isinstance(field, str) for field in fields)
            or len(fields) != len(set(fields)) or not all(k in fields for k in required)):
        raise DataError("TWSE_FIELDS_CHANGED")
    if not isinstance(rows, list) or not rows:
        raise DataError("TWSE_EMPTY_REPORT")
    if any(not isinstance(row, list) for row in rows):
        raise DataError("TWSE_INVALID_REPORT_ROW")
    return {key: fields.index(key) for key in required}, rows


def parse_market(payload, month):
    columns, rows = _table(payload, ("日期", "成交金額", "發行量加權股價指數"))
    output = []
    for row in rows:
        try:
            session = roc_date(row[columns["日期"]])
            if (session.year, session.month) != (month.year, month.month):
                raise DataError("TWSE_REPORT_MONTH_MISMATCH")
            output.append({"session": session.isoformat(),
                "turnover_twd": numeric(row[columns["成交金額"]]),
                "index_close": numeric(row[columns["發行量加權股價指數"]])})
        except (IndexError, TypeError):
            raise DataError("TWSE_INVALID_MARKET_ROW") from None
    dates = [r["session"] for r in output]
    if dates != sorted(set(dates)):
        raise DataError("TWSE_DUPLICATE_OR_UNSORTED_MARKET_SESSIONS")
    return output


def parse_stock(payload, symbol, month, document):
    required = ("日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價", "收盤價")
    columns, rows = _table(payload, required)
    if not re.search(r"(?<![0-9])" + re.escape(symbol) + r"(?![0-9])", str(payload.get("title", ""))):
        raise DataError("TWSE_SYMBOL_MISMATCH")
    output = []
    for row in rows:
        try:
            session = roc_date(row[columns["日期"]])
            if (session.year, session.month) != (month.year, month.month):
                raise DataError("TWSE_REPORT_MONTH_MISMATCH")
            prices = {key: numeric(row[columns[field]]) for key, field in
                (("open", "開盤價"), ("high", "最高價"), ("low", "最低價"), ("close", "收盤價"))}
            if not prices["low"] <= min(prices["open"], prices["close"]) <= max(prices["open"], prices["close"]) <= prices["high"]:
                raise DataError("TWSE_INVALID_OHLC")
            volume = numeric(row[columns["成交股數"]], zero=True)
            if not volume.is_integer():
                raise DataError("TWSE_VOLUME_NOT_INTEGER_SHARES")
            output.append({"session": session.isoformat(),
                "closed_at": session.isoformat() + "T13:30:00+08:00", "complete": True,
                **prices, "volume": int(volume), "turnover_twd": numeric(row[columns["成交金額"]], zero=True),
                "halted": volume == 0,
                # Flat daily bars are conservatively excluded; this is not an exchange limit-lock feed.
                "limit_locked": prices["high"] == prices["low"],
                "limit_lock_assessment": "FLAT_BAR_PROXY_ONLY_NOT_VERIFIED",
                "retrieved_at": document.fetched_at, "source_url": document.url,
                "source_sha256": document.sha256})
        except (IndexError, TypeError):
            raise DataError("TWSE_INVALID_STOCK_ROW") from None
    dates = [b["session"] for b in output]
    if dates != sorted(set(dates)):
        raise DataError("TWSE_DUPLICATE_OR_UNSORTED_STOCK_SESSIONS")
    return output


def parse_holidays(payload, year):
    columns, rows = _table(payload, ("日期", "名稱"))
    closed = []
    for row in rows:
        try:
            day = roc_date(row[columns["日期"]])
            if day.year != year:
                raise DataError("TWSE_HOLIDAY_YEAR_MISMATCH")
            name = row[columns["名稱"]]
            if not isinstance(name, str):
                raise DataError("TWSE_INVALID_HOLIDAY_NAME")
            if not any(word in name for word in ("開始交易", "最後交易")):
                closed.append(day.isoformat())
        except (IndexError, TypeError):
            raise DataError("TWSE_INVALID_HOLIDAY_ROW") from None
    return sorted(set(closed))


def parse_registry(payload):
    if not isinstance(payload, list) or not payload:
        raise DataError("TWSE_COMPANY_REGISTRY_UNAVAILABLE")
    result = {}
    for row in payload:
        if not isinstance(row, dict) or not all(k in row for k in ("公司代號", "公司簡稱", "出表日期")):
            raise DataError("TWSE_COMPANY_REGISTRY_FIELDS_CHANGED")
        symbol = row["公司代號"]
        if isinstance(symbol, str) and re.fullmatch(r"[1-9][0-9]{3}", symbol):
            if symbol in result:
                raise DataError("TWSE_DUPLICATE_COMPANY_REGISTRY")
            published = str(row["出表日期"])
            try:
                if not re.fullmatch(r"[0-9]{7}", published):
                    raise ValueError()
                published_at = date(int(published[:3]) + 1911, int(published[3:5]), int(published[5:]))
            except ValueError:
                raise DataError("TWSE_INVALID_REGISTRY_DATE") from None
            result[symbol] = {"name": row["公司簡稱"], "published_date": published_at.isoformat()}
    if not result:
        raise DataError("TWSE_COMPANY_REGISTRY_EMPTY")
    return result


def expected_completed_session(as_of, holidays_for_year):
    """獨立推導應有的最近完成日；資料缺列不會被當成額外休市。"""
    local = as_of.astimezone(TAIPEI)
    candidate = local.date() if local.time() >= time(16, 0) else local.date() - timedelta(days=1)
    for _ in range(370):
        # The caller must supply verified holiday data for every crossed year.
        holidays = holidays_for_year(candidate.year)
        if candidate.weekday() < 5 and candidate.isoformat() not in holidays:
            return candidate.isoformat()
        candidate -= timedelta(days=1)
    raise DataError("TWSE_EXPECTED_SESSION_NOT_FOUND")


def build_snapshot(symbols, cache_dir, *, months=8, timeout=15, client=None, progress=None):
    """下載台灣上市普通股觀察快照；不將未核實公司行動／交易狀態說成已驗證。"""
    if type(months) is not int or not 4 <= months <= 12:
        raise DataError("TWSE_MONTHS_MUST_BE_4_TO_12")
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 100:
        raise DataError("TWSE_UNIVERSE_MUST_BE_1_TO_100")
    normalized = []
    for item in symbols:
        symbol = item.get("symbol") if isinstance(item, dict) else item
        exchange = item.get("exchange", "TWSE") if isinstance(item, dict) else "TWSE"
        if exchange != "TWSE" or not isinstance(symbol, str) or not re.fullmatch(r"[1-9][0-9]{3}", symbol):
            raise DataError("TWSE_COMMON_STOCK_ONLY_NO_ETF_OR_TPEX")
        if symbol in normalized:
            raise DataError("TWSE_DUPLICATE_UNIVERSE_SYMBOL")
        normalized.append(symbol)
    client = client or TwseClient(cache_dir, timeout=timeout)
    started = client.now().astimezone(TAIPEI)
    today = started.date()
    sources, errors, market_rows = [], [], []

    def fetch(url, refresh=False):
        if progress:
            progress(url)
        doc = client.get(url, refresh=refresh)
        sources.append(doc.provenance())
        return doc

    registry_doc = fetch(REGISTRY_URL)
    registry = parse_registry(registry_doc.payload)
    for symbol in normalized:
        if symbol not in registry:
            raise DataError("SYMBOL_NOT_IN_TWSE_COMPANY_REGISTRY:" + symbol)
        if not timedelta(0) <= today - date.fromisoformat(registry[symbol]["published_date"]) <= timedelta(days=10):
            raise DataError("TWSE_COMPANY_REGISTRY_STALE")
    holiday_documents, holidays_by_year = {}, {}

    def holidays_for_year(year):
        if year not in holidays_by_year:
            holiday_url = HOLIDAY_BASE + "?" + urlencode({"response": "json", "queryYear": year - 1911})
            try:
                doc = fetch(holiday_url, refresh=True)
                holidays_by_year[year] = set(parse_holidays(doc.payload, year))
                holiday_documents[year] = doc
            except DataError:
                raise DataError("TWSE_HOLIDAY_YEAR_UNAVAILABLE:" + str(year)) from None
        return holidays_by_year[year]

    holidays_for_year(today.year)
    holiday_doc = holiday_documents[today.year]
    expected_latest = expected_completed_session(started, holidays_for_year)
    # A new month's report can legitimately be empty before its first completed session.
    months_to_get = month_starts(date.fromisoformat(expected_latest), months)
    holidays = sorted({day for closed in holidays_by_year.values() for day in closed})
    for month in months_to_get:
        url = BASE + "FMTQIK?" + urlencode({"response": "json", "date": month.strftime("%Y%m01")})
        doc = fetch(url, refresh=month == months_to_get[-1])
        market_rows.extend(parse_market(doc.payload, month))
    # Conservative publication guard: current session is used only after 16:00 Taipei.
    include_today = started.time() >= time(16, 0)
    market_rows = [r for r in market_rows if r["session"] < today.isoformat()
                   or (include_today and r["session"] == today.isoformat())]
    sessions = [r["session"] for r in market_rows]
    if not sessions or sessions != sorted(set(sessions)):
        raise DataError("TWSE_INVALID_MARKET_SESSION_COVERAGE")
    if sessions[-1] != expected_latest:
        errors.append("MARKET_LATEST_SESSION_MISSING_OR_UNSCHEDULED_CLOSURE")

    securities = []
    for symbol in normalized:
        item_errors, bars, stock_sources = [], [], []
        for month in months_to_get:
            url = BASE + "STOCK_DAY?" + urlencode({"response": "json", "date": month.strftime("%Y%m01"), "stockNo": symbol})
            try:
                doc = fetch(url, refresh=month == months_to_get[-1])
                stock_sources.append(doc.provenance())
                bars.extend(parse_stock(doc.payload, symbol, month, doc))
            except DataError as exc:
                item_errors.append(month.isoformat() + ":" + str(exc))
        bars = [b for b in bars if b["session"] <= sessions[-1]]
        bar_dates = [b["session"] for b in bars]
        expected = [s for s in sessions if not bar_dates or s >= bar_dates[0]]
        if not bars or bar_dates != expected or (bars and bars[-1]["session"] != expected_latest):
            item_errors.append("MISSING_OR_STALE_STOCK_SESSIONS")
        if any(b["halted"] or b["volume"] == 0 or b["turnover_twd"] == 0 for b in bars):
            item_errors.append("HALT_OR_ZERO_VOLUME_REQUIRES_REVIEW")
        if len(bars) < 60:
            item_errors.append("INSUFFICIENT_60_SESSION_HISTORY")
        if item_errors:
            errors.append("SYMBOL_COLLECTION_FAILED:" + symbol)
        securities.append({"symbol": symbol, "name": registry[symbol]["name"], "exchange": "TWSE",
            "currency": "TWD", "instrument_type": "COMMON_STOCK", "interval": "1d",
            "source": "TWSE_STOCK_DAY_OFFICIAL_MONTHLY", "volume_unit": "shares",
            "price_basis": "raw_unverified", "actions_reviewed": False, "volume_comparable": False,
            "tradability_reviewed": False, "point_in_time": False, "observation_only": True,
            "observation_blockers": list(OBSERVATION_BLOCKERS), "dossier": {},
            "collection_status": "ERROR" if item_errors else "OK", "collection_errors": item_errors,
            "source_documents": stock_sources, "collected_bar_count": len(bars),
            "bars": [] if item_errors else bars})
    assembled = client.now().astimezone(TAIPEI)
    if assembled.date() != today:
        raise DataError("TWSE_COLLECTION_CROSSED_MARKET_DATE_RETRY")
    # This assembled snapshot is available now. Preserve source retrieval times separately.
    for item in securities:
        item["adjustment_as_of"] = assembled.isoformat()
        for bar in item["bars"]:
            bar["available_at"] = assembled.isoformat()
    latest_market = market_rows[-1]
    status = "DEGRADED" if errors else "COMPLETE"
    return {"schema_version": 1, "synthetic": False, "observation_only": True,
        "as_of": assembled.isoformat(), "universe_version": "twse-observation-" + digest(normalized)[:12],
        "calendar": {"source": "TWSE_FMTQIK_ACTUAL_MARKET_SESSIONS", "available_at": assembled.isoformat(),
            "coverage_through": assembled.date().isoformat(), "sessions": sessions,
            "annual_holiday_source": holiday_doc.provenance(), "annual_closed_dates": holidays,
            "annual_holiday_sources": [doc.provenance() for doc in holiday_documents.values()],
            "unscheduled_closures_handling": "ACTUAL_INDEX_ROWS_NO_FABRICATED_SESSIONS"},
        "market_context": {"source": "TWSE_FMTQIK", "available_at": assembled.isoformat(),
            "stress": "UNKNOWN", "latest_session": latest_market["session"],
            "index_close": latest_market["index_close"], "turnover_twd": latest_market["turnover_twd"]},
        "securities": securities,
        "collection": {"status": status, "started_at": started.isoformat(), "finished_at": assembled.isoformat(),
            "expected_latest_session": expected_latest, "actual_latest_session": sessions[-1],
            "months": months, "requested_symbols": normalized,
            "successful_symbols": [s["symbol"] for s in securities if s["collection_status"] == "OK"],
            "errors": errors, "sources": sources,
            "limitations": ["RAW_PRICES_CORPORATE_ACTIONS_UNVERIFIED", "LIMIT_LOCK_FLAT_BAR_PROXY_ONLY",
                            "NO_HISTORICAL_POINT_IN_TIME_CLAIM", "NO_COMPANY_EVENT_REVIEW"]}}
