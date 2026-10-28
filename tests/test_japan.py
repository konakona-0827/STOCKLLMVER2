from copy import deepcopy
from datetime import date, datetime, time, timedelta
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError, URLError

from support_scout.contracts import DataError, strict_json_loads
from support_scout.japan import (JST, Document, YahooClient, build_snapshot,
    expected_completed_session, holidays_for_year, normalize_symbol, parse_chart, sessions_between)


AS_OF = datetime(2026, 10, 28, 11, 0, tzinfo=JST)
SESSIONS = sessions_between(date(2026, 3, 1), date(2026, 10, 27))


def chart(symbol="6857.T", sessions=None):
    sessions = SESSIONS if sessions is None else sessions
    return {"chart": {"error": None, "result": [{
        "meta": {"symbol": symbol, "currency": "JPY", "exchangeTimezoneName": "Asia/Tokyo",
            "exchangeName": "JPX", "instrumentType": "EQUITY", "dataGranularity": "1d",
            "gmtoffset": 32400, "longName": "Advantest"},
        "timestamp": [int(datetime.combine(date.fromisoformat(day), time(9), JST).timestamp()) for day in sessions],
        "indicators": {"quote": [{"open": [1000] * len(sessions), "high": [1020] * len(sessions),
            "low": [980] * len(sessions), "close": [1010] * len(sessions), "volume": [2_000_000] * len(sessions)}],
            "adjclose": [{"adjclose": [900] * len(sessions)}]}, "events": {}}]}}


def document(payload):
    raw = json.dumps(payload).encode()
    return Document("https://example.test/chart", payload, AS_OF.isoformat(), hashlib.sha256(raw).hexdigest(), "fixture.json")


class FakeClient:
    def __init__(self, payloads=None, now=None):
        self.payloads = payloads or {"6857.T": chart()}
        self.now = now or (lambda: AS_OF)
        self.calls = []

    def get(self, url, *, refresh=True):
        self.calls.append((url, refresh))
        symbol = url.split("/")[-1].split("?")[0]
        value = self.payloads.get(symbol)
        if value is None:
            raise DataError("YAHOO_HTTP_429")
        return document(deepcopy(value))


class CalendarTests(unittest.TestCase):
    def test_symbols_normalize_and_duplicates_after_normalization(self):
        self.assertEqual(normalize_symbol("6857"), "6857.T")
        self.assertEqual(normalize_symbol(" 130a.t "), "130A.T")
        for value in (None, 6857, "AAPL", "6857.TW", "../6857", "0AAA"):
            with self.subTest(value=value), self.assertRaises(DataError):
                normalize_symbol(value)
        with self.assertRaisesRegex(DataError, "DUPLICATE"):
            build_snapshot(["6857", "6857.T"], ".", client=FakeClient())

    def test_publication_guard_does_not_accept_partial_today(self):
        for hour, minute in ((9, 0), (15, 29), (15, 30), (15, 59)):
            self.assertEqual(expected_completed_session(AS_OF.replace(hour=hour, minute=minute)), "2026-10-27")
        self.assertEqual(expected_completed_session(AS_OF.replace(hour=16)), "2026-10-28")

    def test_holiday_weekend_and_special_september_holiday(self):
        self.assertEqual(expected_completed_session(datetime(2026, 9, 23, 17, tzinfo=JST)), "2026-09-18")
        self.assertEqual(expected_completed_session(datetime(2026, 10, 12, 18, tzinfo=JST)), "2026-10-09")
        self.assertEqual(expected_completed_session(datetime(2026, 10, 25, 18, tzinfo=JST)), "2026-10-23")
        self.assertNotIn("2025-11-24", sessions_between(date(2025, 11, 21), date(2025, 11, 25)))

    def test_year_boundary_and_unknown_calendar_fail_closed(self):
        self.assertEqual(expected_completed_session(datetime(2026, 1, 5, 10, tzinfo=JST)), "2025-12-30")
        self.assertEqual(expected_completed_session(datetime(2027, 1, 4, 10, tzinfo=JST)), "2026-12-30")
        with self.assertRaisesRegex(DataError, "CALENDAR_YEAR_UNAVAILABLE"):
            holidays_for_year(2028)
        with self.assertRaisesRegex(DataError, "CALENDAR_YEAR_UNAVAILABLE"):
            expected_completed_session(datetime(2028, 1, 1, 17, tzinfo=JST))


class ChartTests(unittest.TestCase):
    def parse(self, payload):
        return parse_chart(payload, "6857.T", document(payload), expected_latest=SESSIONS[-1], sessions=SESSIONS)

    def test_completed_ohlc_not_adjclose_and_turnover_is_estimate(self):
        bars, info = self.parse(chart())
        self.assertEqual(bars[-1]["close"], 1010)
        self.assertEqual(bars[-1]["turnover_jpy"], 2_020_000_000)
        self.assertTrue(bars[-1]["turnover_estimated"])
        self.assertNotIn("turnover_twd", bars[-1])
        self.assertEqual(bars[-1]["closed_at"], "2026-10-27T15:30:00+09:00")
        self.assertEqual(info["name"], "Advantest")

    def test_partial_or_future_today_candle_is_excluded_before_validation(self):
        payload = chart(sessions=SESSIONS + ["2026-10-28"])
        quote = payload["chart"]["result"][0]["indicators"]["quote"][0]
        for value in quote.values():
            value[-1] = None
        bars, _ = self.parse(payload)
        self.assertEqual(len(bars), len(SESSIONS))
        self.assertEqual(bars[-1]["session"], "2026-10-27")

    def test_symbol_currency_timezone_exchange_interval_instrument_checked(self):
        for field, value in (("symbol", "7203.T"), ("currency", "USD"),
            ("exchangeTimezoneName", "Asia/Taipei"), ("exchangeName", "NYQ"),
            ("instrumentType", "ETF"), ("dataGranularity", "1h"), ("gmtoffset", 28800)):
            payload = chart()
            payload["chart"]["result"][0]["meta"][field] = value
            with self.subTest(field=field), self.assertRaises(DataError):
                self.parse(payload)

    def test_missing_tail_interior_or_duplicate_cannot_be_filled(self):
        for sessions in (SESSIONS[:-1], SESSIONS[:70] + SESSIONS[71:], SESSIONS[:70] + [SESSIONS[69]] + SESSIONS[70:]):
            with self.subTest(count=len(sessions)), self.assertRaises(DataError):
                self.parse(chart(sessions=sessions))

    def test_holiday_bar_is_rejected_instead_of_altering_calendar(self):
        sessions = sorted(SESSIONS + ["2026-10-12"])
        with self.assertRaisesRegex(DataError, "NON_JPX"):
            self.parse(chart(sessions=sessions))

    def test_bad_arrays_nan_null_boolean_prices_and_zero_volume_rejected(self):
        payload = chart()
        payload["chart"]["result"][0]["indicators"]["quote"][0]["close"].pop()
        with self.assertRaisesRegex(DataError, "LENGTH"):
            self.parse(payload)
        for key, value in (("close", None), ("high", float("nan")), ("open", True), ("volume", 0), ("volume", 1.5)):
            payload = chart()
            payload["chart"]["result"][0]["indicators"]["quote"][0][key][-1] = value
            with self.subTest(key=key, value=value), self.assertRaises(DataError):
                self.parse(payload)

    def test_split_in_window_blocks_instead_of_manufacturing_support(self):
        payload = chart()
        payload["chart"]["result"][0]["events"]["splits"] = {
            "x": {"date": int(datetime(2026, 8, 3, 9, tzinfo=JST).timestamp()), "numerator": 4, "denominator": 1}}
        with self.assertRaisesRegex(DataError, "CORPORATE_ACTION"):
            self.parse(payload)

    def test_future_action_does_not_enter_current_snapshot(self):
        payload = chart()
        payload["chart"]["result"][0]["events"]["splits"] = {
            "x": {"date": int(datetime(2026, 10, 29, 9, tzinfo=JST).timestamp()), "numerator": 4, "denominator": 1}}
        _, info = self.parse(payload)
        self.assertEqual(info["yahoo_events"], [])

    def test_malformed_chart_is_controlled_error(self):
        for payload in ({}, [], {"chart": None}, {"chart": {"error": {"code": "Not Found"}}},
            {"chart": {"error": None, "result": None}}):
            with self.subTest(payload=payload), self.assertRaises(DataError):
                self.parse(payload)


class SnapshotTests(unittest.TestCase):
    def test_bundle_explicitly_marks_unknowns_and_actual_retrieval_time(self):
        client = FakeClient()
        bundle = build_snapshot(["6857"], ".", client=client)
        self.assertEqual(bundle["market"], "JP")
        self.assertEqual(bundle["collection"]["status"], "COMPLETE")
        self.assertEqual(bundle["collection"]["expected_latest_session"], "2026-10-27")
        self.assertEqual(bundle["calendar"]["sessions"], SESSIONS)
        self.assertTrue(client.calls[0][1])
        item = bundle["securities"][0]
        self.assertEqual(item["symbol"], "6857.T")
        self.assertFalse(item["point_in_time"])
        self.assertFalse(item["tradability_reviewed"])
        self.assertFalse(item["actions_reviewed"])
        self.assertTrue(item["observation_only"])
        self.assertTrue(item["observation_blockers"])
        self.assertEqual(item["turnover_basis"], "estimated_close_times_volume")
        self.assertEqual({bar["available_at"] for bar in item["bars"]}, {AS_OF.isoformat()})
        self.assertEqual(item["adjustment_as_of"], AS_OF.isoformat())

    def test_one_bad_symbol_remains_visible_and_preserves_other_success(self):
        client = FakeClient()
        bundle = build_snapshot(["6857", "7203"], ".", client=client)
        self.assertEqual(bundle["collection"]["status"], "DEGRADED")
        self.assertEqual(len(bundle["securities"]), 2)
        self.assertEqual(len(bundle["securities"][0]["bars"]), len(SESSIONS))
        self.assertEqual(bundle["securities"][1]["bars"], [])
        self.assertEqual(bundle["securities"][1]["collection_errors"], ["YAHOO_HTTP_429"])
        self.assertEqual(bundle["collection"]["successful_symbols"], ["6857.T"])
        self.assertEqual(len(client.calls), 2)

    def test_all_bad_keeps_null_actual_latest_not_calendar_tail(self):
        bundle = build_snapshot(["7203"], ".", client=FakeClient())
        self.assertIsNone(bundle["collection"]["actual_latest_session"])
        self.assertEqual(bundle["collection"]["expected_latest_session"], "2026-10-27")

    def test_crossing_1600_cutoff_during_collection_requires_retry(self):
        moments = iter([AS_OF.replace(hour=15, minute=59), AS_OF.replace(hour=16, minute=0)])
        with self.assertRaisesRegex(DataError, "CROSSED_SESSION"):
            build_snapshot(["6857"], ".", client=FakeClient(now=lambda: next(moments)))


class ClientTests(unittest.TestCase):
    def test_archive_has_exact_raw_hash_and_never_serves_cache(self):
        raw = json.dumps(chart(), ensure_ascii=False).encode()
        calls = []
        def opener(request, *, timeout):
            calls.append((request.full_url, timeout))
            if len(calls) == 1:
                return io.BytesIO(raw)
            raise URLError("no network; cached response must not be reused")
        with tempfile.TemporaryDirectory() as tmp:
            client = YahooClient(tmp, opener=opener, now=lambda: AS_OF)
            doc = client.get("https://example.test/6857.T")
            envelope = strict_json_loads(Path(doc.cache_file).read_text(encoding="utf-8"))
            self.assertEqual(envelope["raw_json"].encode(), raw)
            self.assertEqual(doc.sha256, hashlib.sha256(raw).hexdigest())
            self.assertEqual(envelope["sha256"], doc.sha256)
            self.assertFalse(doc.from_cache)
            with self.assertRaisesRegex(DataError, "NETWORK_UNAVAILABLE"):
                client.get("https://example.test/6857.T", refresh=False)
            self.assertEqual(len(calls), 2)

    def test_http_rate_limit_is_bounded_and_controlled(self):
        calls = []
        def opener(request, *, timeout):
            calls.append(request.full_url)
            raise HTTPError(request.full_url, 429, "Rate limited", {}, None)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(DataError, "YAHOO_HTTP_429"):
                YahooClient(tmp, opener=opener).get("https://example.test/chart")
        self.assertEqual(len(calls), 1)

    def test_invalid_json_duplicate_and_response_size_rejected(self):
        for raw, expected in ((b"not-json", "INVALID_JSON"), (b'{"chart":1,"chart":2}', "DUPLICATE_JSON_KEY"),
                              (b"x" * 8_000_001, "TOO_LARGE")):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                client = YahooClient(tmp, opener=lambda request, timeout: io.BytesIO(raw))
                with self.assertRaisesRegex(DataError, expected):
                    client.get("https://example.test/chart")
                self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
