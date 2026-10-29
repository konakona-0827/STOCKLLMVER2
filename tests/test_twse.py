from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

from support_scout.contracts import DataError, TAIPEI, digest
from support_scout.twse import (Document, REGISTRY_URL, TwseClient, build_snapshot,
                               expected_completed_session, month_starts, numeric, parse_holidays, parse_market,
                               parse_registry, parse_stock, roc_date)

NOW = datetime(2026, 10, 27, 18, 0, tzinfo=TAIPEI)


def document(payload, url="https://www.twse.com.tw/test"):
    return Document(url, payload, NOW.isoformat(), digest(payload), "fixture.json")


def month_sessions(month, *, missing_today=False):
    day = month
    output = []
    while day.month == month.month and day <= NOW.date():
        # An unscheduled closure must come from actual market rows, not a weekday calendar.
        if day.weekday() < 5 and day != date(2026, 10, 23) and not (missing_today and day == NOW.date()):
            output.append(day)
        day += timedelta(days=1)
    return output


def roc(day):
    return f"{day.year - 1911:03d}/{day.month:02d}/{day.day:02d}"


def market_payload(month, *, missing_today=False):
    return {"stat": "OK", "fields": ["日期", "成交金額", "發行量加權股價指數"],
        "data": [[roc(day), "500,000,000,000", "30,000.00"]
                 for day in month_sessions(month, missing_today=missing_today)]}


def stock_payload(month, symbol="2330", *, missing_last=False):
    days = month_sessions(month)
    if missing_last:
        days = days[:-1]
    return {"stat": "OK", "title": "115年成交資訊 " + symbol + " 台積電",
        "fields": ["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價", "收盤價"],
        "data": [[roc(day), "1,000,000", "123,456,789", "100", "103", "99", "102"] for day in days]}


class FixtureClient:
    now = staticmethod(lambda: NOW)

    def __init__(self, *, missing_stock=False, missing_market=False, fail_stock_month=False):
        self.missing_stock = missing_stock
        self.missing_market = missing_market
        self.fail_stock_month = fail_stock_month
        self.calls = []

    def get(self, url, *, refresh=False):
        self.calls.append((url, refresh))
        if url == REGISTRY_URL:
            payload = [{"公司代號": "2330", "公司簡稱": "台積電", "出表日期": "1151027"},
                       {"公司代號": "2317", "公司簡稱": "鴻海", "出表日期": "1151027"}]
        elif "holidaySchedule" in url:
            payload = {"stat": "ok", "fields": ["日期", "名稱"],
                       "data": [["2026-01-01", "中華民國開國紀念日"], ["2026-01-02", "國曆新年開始交易日"]]}
        else:
            values = parse_qs(urlparse(url).query)
            value = values["date"][0]
            month = date(int(value[:4]), int(value[4:6]), 1)
            if "FMTQIK" in url:
                payload = market_payload(month, missing_today=self.missing_market)
            else:
                if self.fail_stock_month and month.month == 9:
                    raise DataError("TWSE_NETWORK_UNAVAILABLE")
                payload = stock_payload(month, values["stockNo"][0],
                    missing_last=self.missing_stock and month.month == 10)
        return document(payload, url)


class ParserTests(unittest.TestCase):
    def test_expected_session_uses_clock_weekends_holidays_and_verified_previous_year(self):
        closed = {2026: {"2026-10-26", "2026-12-25"}, 2027: {"2027-01-01"}}
        for current, expected in (
            (datetime(2026, 10, 28, 10, tzinfo=TAIPEI), "2026-10-27"),
            (datetime(2026, 10, 28, 18, tzinfo=TAIPEI), "2026-10-28"),
            (datetime(2026, 10, 25, 18, tzinfo=TAIPEI), "2026-10-23"),
            (datetime(2026, 10, 27, 10, tzinfo=TAIPEI), "2026-10-23"),
            (datetime(2026, 12, 25, 18, tzinfo=TAIPEI), "2026-12-24"),
            (datetime(2027, 1, 4, 10, tzinfo=TAIPEI), "2026-12-31"),
        ):
            with self.subTest(current=current):
                self.assertEqual(expected_completed_session(current, lambda year: closed[year]), expected)

    def test_roc_dates_and_month_starts(self):
        self.assertEqual(roc_date("115/10/27"), date(2026, 10, 27))
        self.assertEqual(roc_date("2026-10-27"), date(2026, 10, 27))
        self.assertEqual(month_starts(date(2026, 2, 15), 4),
                         [date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1)])
        for invalid in (None, "115/02/30", "20261027"):
            with self.assertRaises(DataError):
                roc_date(invalid)

    def test_stock_actual_turnover_and_share_units_are_preserved(self):
        month = date(2026, 10, 1)
        payload = stock_payload(month)
        bars = parse_stock(payload, "2330", month, document(payload))
        self.assertEqual(bars[0]["volume"], 1_000_000)
        self.assertEqual(bars[0]["turnover_twd"], 123_456_789)
        self.assertNotEqual(bars[0]["close"] * bars[0]["volume"], bars[0]["turnover_twd"])
        self.assertEqual(bars[0]["retrieved_at"], NOW.isoformat())
        self.assertEqual(bars[0]["limit_lock_assessment"], "FLAT_BAR_PROXY_ONLY_NOT_VERIFIED")

    def test_stock_symbol_month_fields_and_ohlc_fail_closed(self):
        month = date(2026, 10, 1)
        original = stock_payload(month)
        for kind in ("symbol", "month", "fields", "field_type", "row_type", "ohlc", "duplicate", "nonfinite", "fractional_volume"):
            with self.subTest(kind=kind):
                payload = deepcopy(original)
                if kind == "symbol": payload["title"] = "2317 鴻海"
                if kind == "month": payload["data"][0][0] = "115/09/01"
                if kind == "fields": payload["fields"][1] = "成交張數"
                if kind == "field_type": payload["fields"][1] = {}
                if kind == "row_type": payload["data"][0] = {}
                if kind == "ohlc": payload["data"][0][5] = "120"
                if kind == "duplicate": payload["data"][1] = deepcopy(payload["data"][0])
                if kind == "nonfinite": payload["data"][0][6] = "NaN"
                if kind == "fractional_volume": payload["data"][0][1] = "1.5"
                with self.assertRaises(DataError):
                    parse_stock(payload, "2330", month, document(payload))

    def test_market_sessions_not_weekday_guess(self):
        month = date(2026, 10, 1)
        rows = parse_market(market_payload(month), month)
        self.assertNotIn("2026-10-23", [r["session"] for r in rows])
        self.assertEqual(rows[-1]["session"], "2026-10-27")

    def test_holiday_table_does_not_treat_first_trading_day_as_holiday(self):
        payload = {"stat": "ok", "fields": ["日期", "名稱"],
            "data": [["2026-01-01", "元旦"], ["2026-01-02", "國曆新年開始交易日"],
                     ["2026-02-11", "農曆春節前最後交易日"], ["2026-10-26", "光復節補假"]]}
        self.assertEqual(parse_holidays(payload, 2026), ["2026-01-01", "2026-10-26"])
        with self.assertRaisesRegex(DataError, "YEAR_MISMATCH"):
            parse_holidays(payload, 2027)

    def test_registry_needs_company_ids_dates_and_unique_entries(self):
        registry = [{"公司代號": "2330", "公司簡稱": "台積電", "出表日期": "1151027"}]
        self.assertEqual(parse_registry(registry)["2330"]["published_date"], "2026-10-27")
        with self.assertRaises(DataError):
            parse_registry(registry + registry)
        with self.assertRaises(DataError):
            parse_registry([{"公司代號": "2330"}])


class SnapshotTests(unittest.TestCase):
    def test_month_start_before_open_does_not_request_empty_new_month(self):
        class FirstMorningClient(FixtureClient):
            now = staticmethod(lambda: datetime(2026, 11, 1, 10, tzinfo=TAIPEI))
        client = FirstMorningClient()
        result = build_snapshot(["2330"], "unused", months=4, client=client)
        self.assertEqual(result["collection"]["expected_latest_session"], "2026-10-30")
        self.assertTrue(any("date=20261001" in url for url, _ in client.calls))
        self.assertFalse(any("date=20261101" in url for url, _ in client.calls))
        # The fixture deliberately ends on 10/27; report that gap, not an empty-November parser failure.
        self.assertEqual(result["collection"]["status"], "DEGRADED")

    def test_preopen_missing_yesterday_cannot_be_complete(self):
        class MorningClient(FixtureClient):
            now = staticmethod(lambda: datetime(2026, 10, 28, 10, tzinfo=TAIPEI))
        result = build_snapshot(["2330"], "unused", months=4, client=MorningClient(missing_market=True))
        self.assertEqual(result["collection"]["expected_latest_session"], "2026-10-27")
        self.assertEqual(result["collection"]["actual_latest_session"], "2026-10-26")
        self.assertEqual(result["collection"]["status"], "DEGRADED")

    def test_cross_year_requires_previous_year_official_holiday_source(self):
        class NewYearClient(FixtureClient):
            now = staticmethod(lambda: datetime(2027, 1, 1, 10, tzinfo=TAIPEI))

            def get(self, url, *, refresh=False):
                if url == REGISTRY_URL:
                    return document([{"公司代號": "2330", "公司簡稱": "台積電", "出表日期": "1160101"}], url)
                if "holidaySchedule" in url:
                    year = parse_qs(urlparse(url).query)["queryYear"][0]
                    self.calls.append((url, refresh))
                    if year == "116":
                        return document({"stat": "ok", "fields": ["日期", "名稱"],
                                         "data": [["2027-01-01", "元旦"]]}, url)
                    raise DataError("TWSE_NETWORK_UNAVAILABLE")
                self.fail("Market data should not be fetched without a verified expected date")

        client = NewYearClient()
        with self.assertRaisesRegex(DataError, "TWSE_HOLIDAY_YEAR_UNAVAILABLE:2026"):
            build_snapshot(["2330"], "unused", months=4, client=client)
        self.assertTrue(any("queryYear=115" in url for url, _ in client.calls))

    def test_real_contract_observation_only_and_sources_preserved(self):
        client = FixtureClient()
        result = build_snapshot(["2330"], "unused", months=4, client=client)
        self.assertEqual(result["collection"]["status"], "COMPLETE")
        self.assertEqual(result["collection"]["expected_latest_session"], "2026-10-27")
        self.assertTrue(result["observation_only"])
        self.assertFalse(result["synthetic"])
        item = result["securities"][0]
        self.assertFalse(item["actions_reviewed"])
        self.assertFalse(item["tradability_reviewed"])
        self.assertFalse(item["point_in_time"])
        self.assertEqual(item["price_basis"], "raw_unverified")
        self.assertGreaterEqual(len(item["bars"]), 60)
        self.assertEqual(item["collection_status"], "OK")
        self.assertTrue(all(b["available_at"] == result["as_of"] for b in item["bars"]))
        self.assertNotIn("2026-10-23", result["calendar"]["sessions"])
        self.assertEqual(len(result["collection"]["sources"]), 10)
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_missing_stock_session_is_explicit_and_not_filled(self):
        result = build_snapshot(["2330"], "unused", months=4, client=FixtureClient(missing_stock=True))
        self.assertEqual(result["collection"]["status"], "DEGRADED")
        self.assertEqual(result["securities"][0]["bars"], [])
        self.assertIn("MISSING_OR_STALE_STOCK_SESSIONS", result["securities"][0]["collection_errors"])

    def test_missing_market_latest_never_presents_previous_session_as_current(self):
        result = build_snapshot(["2330"], "unused", months=4, client=FixtureClient(missing_market=True))
        self.assertEqual(result["collection"]["expected_latest_session"], "2026-10-27")
        self.assertEqual(result["collection"]["actual_latest_session"], "2026-10-26")
        self.assertEqual(result["collection"]["status"], "DEGRADED")

    def test_month_failure_does_not_silently_drop_rows(self):
        result = build_snapshot(["2330"], "unused", months=4, client=FixtureClient(fail_stock_month=True))
        self.assertEqual(result["securities"][0]["collection_status"], "ERROR")
        self.assertEqual(result["securities"][0]["bars"], [])
        self.assertTrue(any("2026-09-01:TWSE_NETWORK_UNAVAILABLE" in e for e in result["securities"][0]["collection_errors"]))

    def test_non_twse_etf_duplicates_and_unknown_symbol_rejected(self):
        for symbols in (["0050"], ["2330.TW"], [{"symbol": "6488", "exchange": "TPEX"}], ["2330", "2330"], ["9999"]):
            with self.subTest(symbols=symbols):
                with self.assertRaises(DataError):
                    build_snapshot(symbols, "unused", months=4, client=FixtureClient())


class HttpCacheTests(unittest.TestCase):
    def test_refresh_preserves_content_addressed_original_responses(self):
        payloads = [b'{"value":1}', b'{"value":2}']
        def opener(*args, **kwargs):
            return BytesIO(payloads.pop(0))
        with tempfile.TemporaryDirectory() as directory:
            client = TwseClient(directory, min_interval=0, opener=opener)
            first = client.get("https://www.twse.com.tw/test")
            second = client.get("https://www.twse.com.tw/test", refresh=True)
            self.assertNotEqual(first.raw_archive_file, second.raw_archive_file)
            self.assertEqual(Path(first.raw_archive_file).read_bytes(), b'{"value":1}')
            self.assertEqual(Path(second.raw_archive_file).read_bytes(), b'{"value":2}')
            self.assertEqual(first.provenance()["raw_archive_file"], first.raw_archive_file)
            cached = client.get("https://www.twse.com.tw/test")
            self.assertTrue(cached.from_cache)
            self.assertEqual(cached.raw_archive_file, second.raw_archive_file)

    def test_http_payload_archived_checksum_checked_and_refetched_after_tamper(self):
        calls = []
        payload = {"stat": "OK", "message": "測試來源"}
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        def opener(request, timeout):
            calls.append(request.full_url)
            self.assertEqual(timeout, 5)
            return BytesIO(raw)
        with tempfile.TemporaryDirectory() as directory:
            client = TwseClient(directory, timeout=5, min_interval=0, opener=opener, now=lambda: NOW)
            first = client.get("https://www.twse.com.tw/test")
            second = client.get("https://www.twse.com.tw/test")
            self.assertFalse(first.from_cache)
            self.assertTrue(second.from_cache)
            self.assertEqual(first.sha256, second.sha256)
            self.assertEqual(len(calls), 1)
            cache = Path(first.cache_file)
            envelope = json.loads(cache.read_text(encoding="utf-8"))
            envelope["raw_json"] = '{"tampered":true}'
            cache.write_text(json.dumps(envelope), encoding="utf-8")
            third = client.get("https://www.twse.com.tw/test")
            self.assertFalse(third.from_cache)
            self.assertEqual(len(calls), 2)

    def test_invalid_http_json_is_controlled_error(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TwseClient(directory, min_interval=0, opener=lambda *args, **kwargs: BytesIO(b"not-json"))
            with self.assertRaisesRegex(DataError, "TWSE_INVALID_JSON"):
                client.get("https://www.twse.com.tw/test")


if __name__ == "__main__":
    unittest.main()
