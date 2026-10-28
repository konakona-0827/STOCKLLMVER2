from copy import deepcopy
from datetime import datetime
from io import BytesIO
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError, URLError
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from support_scout.contracts import DataError, digest
from support_scout.japan import JST
from support_scout.universe import (JpxUniverseClient, UniverseDocument, SOURCE_URL,
    load_universe, select_cohort)

NOW = datetime(2026, 10, 28, 11, tzinfo=JST)
HEADERS = ["日付", "コード", "銘柄名", "市場・商品区分", "33業種コード", "33業種区分",
           "17業種コード", "17業種区分", "規模コード", "規模区分"]
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
DOCREL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def row(code="6857", segment="プライム（内国株式）", size="TOPIX Core30", day="20260930"):
    return [day, code, "会社" + code, segment, "3650", "電気機器", "9", "電機・精密", "1", size]


def xlsx(rows=None, *, headers=None, shared=False, formula=False, external=False):
    rows = [row()] if rows is None else rows
    headers = HEADERS if headers is None else headers
    all_rows = [headers] + rows
    worksheet = ET.Element("{" + MAIN + "}worksheet")
    data = ET.SubElement(worksheet, "{" + MAIN + "}sheetData")
    strings = []
    for number, values in enumerate(all_rows, 1):
        record = ET.SubElement(data, "{" + MAIN + "}row", r=str(number))
        for index, value in enumerate(values):
            cell = ET.SubElement(record, "{" + MAIN + "}c", r=chr(65 + index) + str(number), t="s" if shared else "inlineStr")
            if formula and number == 2 and index == 1:
                ET.SubElement(cell, "{" + MAIN + "}f").text = "1+1"
            if shared:
                ET.SubElement(cell, "{" + MAIN + "}v").text = str(len(strings))
                strings.append(str(value))
            else:
                inline = ET.SubElement(cell, "{" + MAIN + "}is")
                ET.SubElement(inline, "{" + MAIN + "}t").text = str(value)
    workbook = ET.Element("{" + MAIN + "}workbook")
    sheets = ET.SubElement(workbook, "{" + MAIN + "}sheets")
    ET.SubElement(sheets, "{" + MAIN + "}sheet", {"name": "Sheet1", "sheetId": "1", "{" + DOCREL + "}id": "rId1"})
    relationships = ET.Element("{" + REL + "}Relationships")
    attrs = {"Id": "rId1", "Target": "worksheets/sheet1.xml", "Type": DOCREL + "/worksheet"}
    if external:
        attrs["TargetMode"] = "External"
    ET.SubElement(relationships, "{" + REL + "}Relationship", attrs)
    buf = BytesIO()
    with ZipFile(buf, "w", ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", ET.tostring(workbook, encoding="utf-8"))
        archive.writestr("xl/_rels/workbook.xml.rels", ET.tostring(relationships, encoding="utf-8"))
        archive.writestr("xl/worksheets/sheet1.xml", ET.tostring(worksheet, encoding="utf-8"))
        if shared:
            root = ET.Element("{" + MAIN + "}sst")
            for value in strings:
                item = ET.SubElement(root, "{" + MAIN + "}si")
                ET.SubElement(item, "{" + MAIN + "}t").text = value
            archive.writestr("xl/sharedStrings.xml", ET.tostring(root, encoding="utf-8"))
    return buf.getvalue()


class FakeClient:
    def __init__(self, raw=None, at=NOW):
        self.raw = raw if raw is not None else xlsx()
        self.at = at
        self.calls = []
    def now(self):
        return self.at
    def get(self, url):
        self.calls.append(url)
        return UniverseDocument(url, self.raw, self.at.isoformat())


class UniverseTests(unittest.TestCase):
    def load(self, raw=None, *, now=NOW):
        with tempfile.TemporaryDirectory() as tmp:
            return load_universe(tmp, client=FakeClient(raw, at=now))

    def test_official_fields_raw_hash_and_source_date_separate_from_retrieval(self):
        raw = xlsx(shared=True)
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(raw)
            universe = load_universe(tmp, client=client)
            self.assertEqual(client.calls, [SOURCE_URL])
            self.assertEqual(universe["as_of"], "2026-09-30")
            self.assertEqual(universe["fetched_at"], NOW.isoformat())
            self.assertNotEqual(universe["as_of"], universe["fetched_at"][:10])
            self.assertEqual(universe["source"]["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(universe["sha256"], universe["source"]["sha256"])
            self.assertEqual(Path(universe["source"]["raw_file"]).read_bytes(), raw)
            self.assertFalse(universe["source"]["from_cache"])
            self.assertFalse(universe["point_in_time"])
            self.assertEqual(universe["items"][0]["symbol"], "6857.T")
            self.assertEqual(universe["symbols"], ["6857.T"])
            self.assertEqual(universe["items"][0]["sector"], "電気機器")
            self.assertEqual(universe["items"][0]["size_category"], "TOPIX Core30")
            self.assertEqual(universe["freshness"]["expected_month"], "2026-09")

    def test_only_domestic_prime_standard_growth_common_codes_included(self):
        rows = [row(), row("130A", "グロース（内国株式）", "-"),
            row("1301", "スタンダード（内国株式）", "TOPIX Small 1"),
            row("1305", "ETF・ETN", "-"), row("8951", "REIT・ベンチャーファンド・カントリーファンド・インフラファンド", "-"),
            row("25935", "プライム（内国株式）", "-"), row("1302", "プライム（外国株式）", "-"),
            row("1303", "PRO Market", "-")]
        universe = self.load(xlsx(rows))
        self.assertEqual([item["symbol"] for item in universe["items"]], ["6857.T", "1301.T", "130A.T"])
        self.assertEqual(universe["counts"]["included_rows"], 3)
        self.assertEqual(universe["counts"]["excluded_rows"], 5)
        self.assertEqual(universe["counts"]["excluded_by_reason"]["DOMESTIC_PREFERENCE_OR_CLASS_SHARE"], 1)

    def test_size_priority_then_code_stable_and_all_others_remain(self):
        universe = self.load(xlsx([row("7203", size="TOPIX Large70"), row("6857"),
            row("6501"), row("1301", size="TOPIX Mid400"), row("130A", size="-"),
            row("1302", size="TOPIX Small 1")]))
        self.assertEqual([item["code"] for item in universe["items"]], ["6501", "6857", "7203", "1301", "1302", "130A"])

    def test_repeated_download_has_same_universe_id_but_unique_audit_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = load_universe(tmp, client=FakeClient())
            second = load_universe(tmp, client=FakeClient())
            self.assertEqual(first["universe_id"], second["universe_id"])
            self.assertNotEqual(first["source"]["raw_file"], second["source"]["raw_file"])
            self.assertEqual(len(list(Path(tmp).glob("*.xlsx"))), 2)

    def test_changed_rows_change_universe_id_and_order_digest(self):
        first = self.load()
        second = self.load(xlsx([row(), row("7203")]))
        self.assertNotEqual(first["universe_id"], second["universe_id"])
        self.assertNotEqual(first["ordered_items_digest"], second["ordered_items_digest"])

    def test_third_jpx_session_0900_publication_boundary(self):
        old = xlsx([row(day="20260831")])
        fresh = xlsx([row(day="20260930")])
        before = datetime(2026, 10, 5, 8, 59, tzinfo=JST)
        due = before.replace(hour=9, minute=0)
        self.assertEqual(self.load(old, now=before)["freshness"]["expected_month"], "2026-08")
        with self.assertRaisesRegex(DataError, "STALE_LIST"):
            self.load(old, now=due)
        self.assertEqual(self.load(fresh, now=due)["freshness"]["expected_month"], "2026-09")

    def test_november_culture_holiday_does_not_count_as_third_business_day(self):
        old = xlsx([row(day="20260930")])
        # Nov 2 = first, Nov 3 closed, Nov 4 = second, Nov 5 = third.
        before = datetime(2026, 11, 5, 8, 59, tzinfo=JST)
        self.assertEqual(self.load(old, now=before)["freshness"]["publication_due_at"], "2026-11-05T09:00:00+09:00")
        with self.assertRaisesRegex(DataError, "STALE_LIST"):
            self.load(old, now=before.replace(hour=9))

    def test_month_end_on_weekend_allows_last_jpx_session_date(self):
        registry = self.load(xlsx([row(day="20260529")]), now=datetime(2026, 6, 10, 12, tzinfo=JST))
        self.assertEqual(registry["as_of"], "2026-05-29")
        self.assertEqual(registry["freshness"]["expected_month"], "2026-05")

    def test_stale_malformed_and_future_dates_rejected_not_relabelled_today(self):
        for day, code in (("20260831", "STALE_LIST"), ("20260929", "NOT_MONTH_END"),
            ("20261031", "FUTURE_LIST_DATE"), ("20260931", "INVALID_LIST_DATE"),
            ("2026-09-30", "INVALID_LIST_DATE")):
            with self.subTest(day=day), self.assertRaisesRegex(DataError, code):
                self.load(xlsx([row(day=day)]))

    def test_stale_download_raw_and_rejection_reason_are_preserved(self):
        raw = xlsx([row(day="20260831")])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(DataError, "STALE_LIST"):
                load_universe(tmp, client=FakeClient(raw))
            self.assertEqual(next(Path(tmp).glob("*.xlsx")).read_bytes(), raw)
            metadata = json.loads(next(Path(tmp).glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "REJECTED")
            self.assertIn("STALE_LIST", metadata["error"])

    def test_mixed_dates_duplicate_any_code_bad_headers_empty_pool_rejected(self):
        cases = [(xlsx([row(), row("7203", day="20260831")]), "MIXED_DATES"),
                 (xlsx([row(), row()]), "DUPLICATE_SYMBOL"),
                 (xlsx([row(), row("6857", segment="ETF・ETN")]), "DUPLICATE_SYMBOL"),
                 (xlsx(headers=HEADERS[:-1] + ["unknown"]), "HEADERS_CHANGED"),
                 (xlsx([row("1305", segment="ETF・ETN")]), "EMPTY_COMMON_STOCKS")]
        for raw, expected in cases:
            with self.subTest(expected=expected), self.assertRaisesRegex(DataError, expected):
                self.load(raw)

    def test_formula_external_sheet_invalid_archive_and_code_rejected(self):
        for raw, expected in ((xlsx(formula=True), "FORMULA_REJECTED"),
            (xlsx(external=True), "LOCAL_WORKSHEET"), (b"<html>broken</html>", "MALFORMED_XLSX"),
            (xlsx([row("../../secret")]), "INVALID_SECURITY_CODE")):
            with self.subTest(expected=expected), self.assertRaisesRegex(DataError, expected):
                self.load(raw)

    def test_uncovered_calendar_year_is_explicit_failure(self):
        with self.assertRaisesRegex(DataError, "CALENDAR_YEAR_UNAVAILABLE"):
            self.load(xlsx([row(day="20271231")]), now=datetime(2028, 1, 10, 12, tzinfo=JST))

    def test_retrieval_future_or_naive_clock_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(DataError, "RETRIEVAL_IN_FUTURE"):
                load_universe(tmp, client=FakeClient(), now=NOW.replace(hour=10))
            with self.assertRaisesRegex(DataError, "INVALID_AWARE_TIMESTAMP"):
                load_universe(tmp, client=FakeClient(), now=NOW.replace(tzinfo=None))

    def test_rotation_visits_all_rows_without_repeating_first_hundred(self):
        universe = self.load(xlsx([row(str(code), size="-") for code in range(1001, 1246)]))
        seen, cursor, batches = [], 0, []
        while True:
            symbols, cursor = select_cohort(universe, limit=100, cursor=cursor)
            batches.append(len(symbols))
            seen.extend(symbols)
            if cursor == 0:
                break
        self.assertEqual(batches, [100, 100, 45])
        self.assertEqual(seen, [item["symbol"] for item in universe["items"]])
        self.assertEqual(len(set(seen)), 245)
        self.assertEqual(select_cohort(universe, limit=1)[0], ["1001.T"])

    def test_cohort_caps_types_out_of_range_and_tamper_rejected(self):
        universe = self.load()
        for kwargs in ({"limit": 101}, {"limit": 0}, {"limit": True}, {"cursor": -1}, {"cursor": True}, {"cursor": 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(DataError):
                select_cohort(universe, **kwargs)
        universe["items"][0]["name"] = "tampered"
        with self.assertRaisesRegex(DataError, "ITEMS_CHANGED"):
            select_cohort(universe)


class ClientTests(unittest.TestCase):
    def test_real_transport_injected_one_call_timeout_and_no_cache_fallback(self):
        raw = xlsx()
        calls = []
        def opener(request, *, timeout):
            calls.append((request.full_url, timeout))
            if len(calls) == 1:
                return BytesIO(raw)
            raise URLError("offline")
        client = JpxUniverseClient(timeout=7, opener=opener, now=lambda: NOW)
        with tempfile.TemporaryDirectory() as tmp:
            result = load_universe(tmp, client=client)
            self.assertEqual(result["source"]["sha256"], hashlib.sha256(raw).hexdigest())
            with self.assertRaisesRegex(DataError, "NETWORK_UNAVAILABLE"):
                load_universe(tmp, client=client)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0], (SOURCE_URL, 7))

    def test_rate_limit_too_large_and_bad_timeout_are_controlled(self):
        def opener(request, *, timeout):
            raise HTTPError(request.full_url, 429, "rate limited", {}, None)
        with self.assertRaisesRegex(DataError, "HTTP_429"):
            JpxUniverseClient(opener=opener).get(SOURCE_URL)
        with self.assertRaisesRegex(DataError, "TOO_LARGE"):
            JpxUniverseClient(opener=lambda request, timeout: BytesIO(b"x" * 8_000_001)).get(SOURCE_URL)
        for timeout in (0, 31, True, float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(DataError):
                JpxUniverseClient(timeout=timeout)


if __name__ == "__main__":
    unittest.main()
