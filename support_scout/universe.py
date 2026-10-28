"""Versioned JPX monthly domestic common-share universe, with bounded rotation."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from io import BytesIO
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from .contracts import DataError, digest, timestamp
from .japan import JST, CALENDAR_VERSION, normalize_symbol, sessions_between

PAGE_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
SOURCE_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"
PROTOCOL = "jpx-domestic-common-universe-v1"
ORDERING = "TOPIX_CORE30_LARGE70_MID400_THEN_OTHER_CODE_ASC_V1"
_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS = {"m": _MAIN}
_HEADERS = ("日付", "コード", "銘柄名", "市場・商品区分", "33業種コード", "33業種区分",
            "17業種コード", "17業種区分", "規模コード", "規模区分")
_SEGMENTS = {"プライム（内国株式）": "PRIME", "スタンダード（内国株式）": "STANDARD",
             "グロース（内国株式）": "GROWTH"}
_SIZE_RANK = {"TOPIX Core30": 0, "TOPIX Large70": 1, "TOPIX Mid400": 2}


@dataclass(frozen=True)
class UniverseDocument:
    url: str
    raw: bytes
    fetched_at: str


class JpxUniverseClient:
    """Download once with a byte bound and timeout; never substitute cached lists."""
    def __init__(self, *, timeout=15, opener=None, now=None):
        if type(timeout) not in (float, int) or not math.isfinite(timeout) or not 1 <= timeout <= 30:
            raise DataError("JPX_UNIVERSE_TIMEOUT_MUST_BE_1_TO_30")
        self.timeout = timeout
        self.opener = opener or urlopen
        self.now = now or (lambda: datetime.now(JST))

    def get(self, url):
        if url != SOURCE_URL:
            raise DataError("JPX_UNIVERSE_SOURCE_URL_REQUIRED")
        request = Request(url, headers={"User-Agent": "SupportDeskJapan/0.2 (read-only research)",
            "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"})
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read(8_000_001)
        except HTTPError as exc:
            raise DataError("JPX_UNIVERSE_HTTP_" + str(exc.code)) from None
        except (TimeoutError, URLError, OSError):
            raise DataError("JPX_UNIVERSE_NETWORK_UNAVAILABLE") from None
        if len(raw) > 8_000_000:
            raise DataError("JPX_UNIVERSE_RESPONSE_TOO_LARGE")
        return UniverseDocument(url, raw, self.now().isoformat())


def _xml(raw):
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise DataError("JPX_UNIVERSE_XML_DECLARATION_REJECTED")
    return ET.fromstring(raw)


def _cell_text(cell, shared):
    if cell.find("m:f", _NS) is not None:
        raise DataError("JPX_UNIVERSE_FORMULA_REJECTED")
    kind = cell.get("t")
    value = cell.find("m:v", _NS)
    text = value.text if value is not None and value.text is not None else ""
    if kind == "s":
        if not re.fullmatch(r"[0-9]+", text) or int(text) >= len(shared):
            raise DataError("JPX_UNIVERSE_INVALID_SHARED_STRING")
        return shared[int(text)]
    if kind == "inlineStr":
        inline = cell.find("m:is", _NS)
        if inline is None:
            raise DataError("JPX_UNIVERSE_INVALID_INLINE_STRING")
        return "".join(element.text or "" for element in inline.iter("{" + _MAIN + "}t"))
    if kind not in (None, "n", "str"):
        raise DataError("JPX_UNIVERSE_UNSUPPORTED_CELL_TYPE")
    return text


def _column(ref):
    matched = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]*)", ref)
    if not matched:
        raise DataError("JPX_UNIVERSE_INVALID_CELL_REFERENCE")
    col = 0
    for char in matched[1]:
        col = col * 26 + ord(char) - ord("A") + 1
    return col - 1, int(matched[2])


def _read_rows(raw):
    """Read one official-shaped worksheet without extracting or executing files."""
    try:
        if type(raw) is not bytes or not raw or len(raw) > 8_000_000:
            raise DataError("JPX_UNIVERSE_INVALID_XLSX_BYTES")
        with ZipFile(BytesIO(raw)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (not entries or len(entries) > 200 or len(names) != len(set(names))
                    or sum(entry.file_size for entry in entries) > 64_000_000
                    or any(entry.file_size > 32_000_000 or entry.flag_bits & 1 for entry in entries)):
                raise DataError("JPX_UNIVERSE_XLSX_BOUNDS_REJECTED")
            required = {"xl/workbook.xml", "xl/_rels/workbook.xml.rels"}
            if not required <= set(names):
                raise DataError("JPX_UNIVERSE_WORKBOOK_REQUIRED")
            workbook = _xml(archive.read("xl/workbook.xml"))
            sheets = workbook.findall("m:sheets/m:sheet", _NS)
            if len(sheets) != 1:
                raise DataError("JPX_UNIVERSE_SINGLE_SHEET_REQUIRED")
            sheet_id = sheets[0].get("{" + _DOC_REL + "}id")
            relationships = _xml(archive.read("xl/_rels/workbook.xml.rels"))
            matching = [row for row in relationships.findall("{" + _REL + "}Relationship") if row.get("Id") == sheet_id]
            if len(matching) != 1 or matching[0].get("TargetMode") == "External":
                raise DataError("JPX_UNIVERSE_LOCAL_WORKSHEET_REQUIRED")
            target = matching[0].get("Target", "")
            path = target.lstrip("/") if target.startswith("/") else "xl/" + target
            if ".." in PurePosixPath(path).parts or not path.startswith("xl/worksheets/") or path not in names:
                raise DataError("JPX_UNIVERSE_INVALID_WORKSHEET_TARGET")
            shared = []
            if "xl/sharedStrings.xml" in names:
                root = _xml(archive.read("xl/sharedStrings.xml"))
                for entry in root.findall("m:si", _NS):
                    # rPh is a pronunciation guide, not the displayed cell text.
                    fragments = entry.findall("m:t", _NS) + entry.findall("m:r/m:t", _NS)
                    shared.append("".join(fragment.text or "" for fragment in fragments))
                if len(shared) > 100_000:
                    raise DataError("JPX_UNIVERSE_TOO_MANY_SHARED_STRINGS")
            rows = _xml(archive.read(path)).findall("m:sheetData/m:row", _NS)
            if not 2 <= len(rows) <= 20_001:
                raise DataError("JPX_UNIVERSE_ROW_COUNT_REJECTED")
            result = []
            previous_row = 0
            for row in rows:
                row_index = row.get("r", "")
                if not row_index.isdigit() or int(row_index) <= previous_row:
                    raise DataError("JPX_UNIVERSE_INVALID_ROW_ORDER")
                previous_row = int(row_index)
                cells = {}
                for cell in row.findall("m:c", _NS):
                    column, cell_row = _column(cell.get("r", ""))
                    if column in cells or cell_row != previous_row or column > 100:
                        raise DataError("JPX_UNIVERSE_INVALID_OR_DUPLICATE_CELL")
                    cells[column] = _cell_text(cell, shared).strip()
                if not cells:
                    continue
                result.append([cells.get(index, "") for index in range(max(cells) + 1)])
            return result
    except (BadZipFile, ET.ParseError, KeyError, ValueError, TypeError, OSError, RuntimeError) as exc:
        if isinstance(exc, DataError):
            raise
        raise DataError("JPX_UNIVERSE_MALFORMED_XLSX") from None


def _parse(raw):
    rows = _read_rows(raw)
    headers = rows[0]
    if len(headers) != len(set(headers)) or set(headers) != set(_HEADERS):
        raise DataError("JPX_UNIVERSE_HEADERS_CHANGED")
    index = {name: headers.index(name) for name in _HEADERS}
    items, dates, seen, excluded = [], set(), set(), Counter()
    for row in rows[1:]:
        if len(row) != len(headers) or any(not value for value in row):
            raise DataError("JPX_UNIVERSE_INCOMPLETE_ROW")
        get = lambda name: row[index[name]]
        value = get("日付")
        if not re.fullmatch(r"[0-9]{8}", value):
            raise DataError("JPX_UNIVERSE_INVALID_LIST_DATE")
        try:
            list_date = datetime.strptime(value, "%Y%m%d").date()
        except ValueError:
            raise DataError("JPX_UNIVERSE_INVALID_LIST_DATE") from None
        dates.add(list_date)
        code = get("コード")
        if not re.fullmatch(r"[1-9][0-9A-Z]{3,4}", code):
            raise DataError("JPX_UNIVERSE_INVALID_SECURITY_CODE")
        if code in seen:
            raise DataError("JPX_UNIVERSE_DUPLICATE_SYMBOL:" + code)
        seen.add(code)
        segment = get("市場・商品区分")
        if segment not in _SEGMENTS:
            excluded[segment] += 1
            continue
        # Five-character preference/class-share codes are not ordinary shares.
        if len(code) != 4:
            excluded["DOMESTIC_PREFERENCE_OR_CLASS_SHARE"] += 1
            continue
        items.append({"symbol": normalize_symbol(code), "code": code, "name": get("銘柄名"),
            "exchange": "TSE", "currency": "JPY", "instrument_type": "COMMON_STOCK",
            "market_segment": _SEGMENTS[segment], "market_segment_source": segment,
            "sector": get("33業種区分"), "sector_code": get("33業種コード"),
            "sector17": get("17業種区分"), "sector17_code": get("17業種コード"),
            "size_category": get("規模区分"), "size_code": get("規模コード"),
            "classification_basis": "JPX_DOMESTIC_SEGMENT_FOUR_CHARACTER_CODE",
            "registry_as_of": list_date.isoformat()})
    if len(dates) != 1 or not items:
        raise DataError("JPX_UNIVERSE_MIXED_DATES_OR_EMPTY_COMMON_STOCKS")
    items.sort(key=lambda item: (_SIZE_RANK.get(item["size_category"], 3), item["code"]))
    return dates.pop(), items, {"total_rows": len(rows) - 1, "included_rows": len(items),
                               "excluded_rows": sum(excluded.values()), "excluded_by_reason": dict(sorted(excluded.items()))}


def _month_end(day):
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def _freshness(list_date, as_of):
    local = as_of.astimezone(JST)
    first = local.date().replace(day=1)
    month_sessions = sessions_between(first, _month_end(first))
    if len(month_sessions) < 3:
        raise DataError("JPX_UNIVERSE_PUBLICATION_CALENDAR_UNAVAILABLE")
    release_at = datetime.combine(date.fromisoformat(month_sessions[2]), time(9), JST)
    previous_month_end = first - timedelta(days=1)
    expected_end = previous_month_end if local >= release_at else previous_month_end.replace(day=1) - timedelta(days=1)
    last_session = sessions_between(list_date.replace(day=1), _month_end(list_date))[-1]
    if list_date != _month_end(list_date) and list_date.isoformat() != last_session:
        raise DataError("JPX_UNIVERSE_LIST_DATE_NOT_MONTH_END")
    if list_date > previous_month_end:
        raise DataError("JPX_UNIVERSE_FUTURE_LIST_DATE")
    if (list_date.year, list_date.month) < (expected_end.year, expected_end.month):
        raise DataError("JPX_UNIVERSE_STALE_LIST:" + list_date.isoformat())
    return {"status": "CURRENT_MONTHLY_LIST", "expected_month": expected_end.strftime("%Y-%m"),
            "expected_month_end": expected_end.isoformat(), "publication_due_at": release_at.isoformat(),
            "checked_at": local.isoformat(), "calendar_version": CALENDAR_VERSION,
            "policy": "PREVIOUS_MONTH_FROM_THIRD_JPX_SESSION_0900_JST"}


def load_universe(cache_dir, *, timeout=15, client=None, now=None):
    """Download and validate the official monthly pool; never use a fixed fallback.

    ``now`` may be an aware datetime or zero-argument clock. A test client has
    ``get(SOURCE_URL) -> UniverseDocument``; the real client makes one HTTP call.
    """
    clock = now if callable(now) else (lambda: now) if now is not None else None
    client = client or JpxUniverseClient(timeout=timeout, now=clock)
    clock = clock or getattr(client, "now", None) or (lambda: datetime.now(JST))
    document = client.get(SOURCE_URL)
    if not isinstance(document, UniverseDocument) or document.url != SOURCE_URL:
        raise DataError("JPX_UNIVERSE_DOCUMENT_CONTRACT_REJECTED")
    assembled = timestamp(clock().isoformat()).astimezone(JST)
    fetched_at = timestamp(document.fetched_at)
    if fetched_at > assembled:
        raise DataError("JPX_UNIVERSE_RETRIEVAL_IN_FUTURE")
    if type(document.raw) is not bytes or not document.raw or len(document.raw) > 8_000_000:
        raise DataError("JPX_UNIVERSE_INVALID_XLSX_BYTES")
    checksum = hashlib.sha256(document.raw).hexdigest()
    folder = Path(cache_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stem = assembled.strftime("%Y%m%dT%H%M%S") + "-" + checksum[:16] + "-" + uuid4().hex[:8]
    raw_path = folder / (stem + ".xlsx")
    temporary = folder / (stem + ".tmp")
    temporary.write_bytes(document.raw)
    temporary.replace(raw_path)
    source = {"url": SOURCE_URL, "page_url": PAGE_URL,
        "fetched_at": document.fetched_at, "sha256": checksum, "raw_file": str(raw_path.resolve()),
        "from_cache": False, "availability": "CURRENT_RETRIEVAL_NOT_HISTORICAL_POINT_IN_TIME"}
    metadata_path = folder / (stem + ".json")
    try:
        list_date, items, counts = _parse(document.raw)
        freshness = _freshness(list_date, assembled)
    except DataError as exc:
        metadata_path.write_text(json.dumps({"status": "REJECTED", "source": source, "error": str(exc)},
            ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        raise
    source["as_of"] = list_date.isoformat()
    identity = {"protocol": PROTOCOL, "source_sha256": checksum, "as_of": list_date.isoformat(),
                "ordering": ORDERING, "ordered_items_digest": digest(items)}
    result = {"protocol": PROTOCOL, "universe_id": "jpx-domestic-" + digest(identity)[:24],
        "market": "JP", "as_of": list_date.isoformat(), "sha256": checksum, "fetched_at": document.fetched_at,
        "available_at": document.fetched_at, "assembled_at": assembled.isoformat(),
        "point_in_time": False, "source": source, "freshness": freshness,
        "ordering": ORDERING, "ordered_items_digest": digest(items), "items": items,
        "symbols": [item["symbol"] for item in items], "counts": counts,
        "limitations": ["MONTHLY_MEMBERSHIP_NOT_LIVE_LISTING_STATUS", "NO_CURRENT_HALT_OR_EVENT_VERIFICATION",
            "SIZE_CATEGORY_IS_SCAN_PRIORITY_NOT_LIQUIDITY_GUARANTEE", "NO_HISTORICAL_POINT_IN_TIME_CLAIM"]}
    metadata_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return result


def select_cohort(universe, *, limit=100, cursor=0):
    """Rotate through the saved order without starving the rest of the market.

    At the end of a pass return the remaining rows and reset cursor to zero;
    do not pad a short final cohort with duplicates from the next pass.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DataError("JPX_UNIVERSE_COHORT_LIMIT_MUST_BE_1_TO_100")
    if type(cursor) is not int or cursor < 0:
        raise DataError("JPX_UNIVERSE_INVALID_CURSOR")
    if not isinstance(universe, dict) or universe.get("protocol") != PROTOCOL or universe.get("ordering") != ORDERING:
        raise DataError("JPX_UNIVERSE_CONTRACT_REQUIRED")
    items = universe.get("items")
    if not isinstance(items, list) or not items:
        raise DataError("JPX_UNIVERSE_ITEMS_REQUIRED")
    try:
        symbols = [normalize_symbol(item["symbol"]) for item in items]
    except (KeyError, TypeError):
        raise DataError("JPX_UNIVERSE_INVALID_ITEM") from None
    if (len(symbols) != len(set(symbols)) or symbols != universe.get("symbols")
            or digest(items) != universe.get("ordered_items_digest")):
        raise DataError("JPX_UNIVERSE_ITEMS_CHANGED")
    if cursor >= len(items):
        raise DataError("JPX_UNIVERSE_CURSOR_OUT_OF_RANGE")
    end = min(cursor + limit, len(items))
    return symbols[cursor:end], end if end < len(items) else 0
