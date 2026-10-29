from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import unittest
from urllib.parse import parse_qs, urlparse

from support_scout.contracts import DataError, TAIPEI, digest
from support_scout.market import DAILY_URL, build_market_snapshot
from support_scout.twse import Document, REGISTRY_URL

NOW = datetime(2026, 10, 28, 10, tzinfo=TAIPEI)


def quote(symbol, *, day="1151027"):
    return {"Date": day, "Code": symbol, "Name": "行情名稱 " + symbol,
        "TradeVolume": "1,000,000", "TradeValue": "102,000,000",
        "OpeningPrice": "100", "HighestPrice": "104", "LowestPrice": "99",
        "ClosingPrice": "102", "Change": "-2", "Transaction": "500"}


class MarketClient:
    def __init__(self, *, now=NOW):
        self.current = now
        self.calls = []
        self.registry = [
            {"公司代號": "2330", "公司簡稱": "台積電", "產業別": "24", "出表日期": "1151027"},
            {"公司代號": "6415", "公司簡稱": "矽力-KY", "產業別": "24", "出表日期": "1151027"}]
        self.daily = [quote("2330"), quote("6415"), quote("0050"), quote("00400A"), quote("9999")]
        self.closed = {2026: ["2026-01-01", "2026-10-26"], 2027: ["2027-01-01"]}

    def now(self):
        return self.current

    def get(self, url, *, refresh=False):
        self.calls.append((url, refresh))
        if url == REGISTRY_URL:
            payload = self.registry
        elif url == DAILY_URL:
            payload = self.daily
        elif "holidaySchedule" in url:
            year = int(parse_qs(urlparse(url).query)["queryYear"][0]) + 1911
            if year not in self.closed:
                raise DataError("TWSE_NETWORK_UNAVAILABLE")
            payload = {"stat": "OK", "fields": ["日期", "名稱"],
                       "data": [[day, "假日"] for day in self.closed[year]]}
        else:
            raise AssertionError("Unexpected source: " + url)
        return Document(url, deepcopy(payload), self.current.isoformat(), digest(payload), "fixture.json")


class MarketSnapshotTests(unittest.TestCase):
    def test_dynamic_registry_covers_stock_outside_original_pool_without_preselection(self):
        client = MarketClient()
        result = build_market_snapshot("unused", client=client)
        self.assertEqual([row["symbol"] for row in result["securities"]], ["2330", "6415"])
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["coverage"]["eligible_count"], 2)
        self.assertEqual(result["coverage"]["excluded_non_registry_count"], 3)
        self.assertEqual(result["securities"][1]["industry"], "24")
        self.assertEqual(result["securities"][1]["name"], "矽力-KY")
        self.assertEqual(result["securities"][1]["volume_shares"], 1_000_000)
        self.assertAlmostEqual(result["securities"][1]["change_pct"], -2 / 104 * 100)
        self.assertIn((DAILY_URL, True), client.calls)

    def test_sources_and_entire_catalog_are_bound_to_replayable_digest(self):
        result = build_market_snapshot("unused", client=MarketClient())
        self.assertEqual(result, json.loads(json.dumps(result)))
        self.assertTrue(all(len(source["sha256"]) == 64 for source in result["sources"]))
        self.assertEqual(result["catalog_id"], digest({k: v for k, v in result.items() if k != "catalog_id"}))
        changed = MarketClient()
        changed.daily[1].update(ClosingPrice="103", Change="-1")
        self.assertNotEqual(result["catalog_id"], build_market_snapshot("unused", client=changed)["catalog_id"])

    def test_missing_stock_is_reported_instead_of_filled_with_zero(self):
        client = MarketClient()
        client.daily = [row for row in client.daily if row["Code"] != "6415"]
        result = build_market_snapshot("unused", client=client)
        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["coverage"]["missing_symbols"], ["6415"])
        self.assertEqual([row["symbol"] for row in result["securities"]], ["2330"])

    def test_stale_daily_data_does_not_imply_an_unscheduled_holiday(self):
        client = MarketClient()
        for row in client.daily:
            row["Date"] = "1151023"
        result = build_market_snapshot("unused", client=client)
        self.assertEqual(result["expected_session"], "2026-10-27")
        self.assertEqual(result["session"], "2026-10-23")
        self.assertEqual(result["status"], "DEGRADED")
        self.assertIn("MARKET_LATEST_SESSION_MISSING_OR_UNSCHEDULED_CLOSURE", result["errors"])

    def test_stale_registry_is_explicit_and_future_registry_is_rejected(self):
        client = MarketClient()
        client.registry[0]["出表日期"] = "1151001"
        result = build_market_snapshot("unused", client=client)
        self.assertEqual(result["status"], "DEGRADED")
        self.assertIn("MARKET_REGISTRY_STALE:2330", result["errors"])
        client.registry[0]["出表日期"] = "1151029"
        with self.assertRaisesRegex(DataError, "REGISTRY_DATE_IN_FUTURE"):
            build_market_snapshot("unused", client=client)

    def test_before_publication_cutoff_rejects_current_day_or_future_quotes(self):
        for day in ("1151028", "1151029"):
            with self.subTest(day=day):
                client = MarketClient()
                for row in client.daily:
                    row["Date"] = day
                with self.assertRaisesRegex(DataError, "SESSION_NOT_COMPLETED"):
                    build_market_snapshot("unused", client=client)

    def test_after_cutoff_requires_current_day(self):
        client = MarketClient(now=NOW.replace(hour=18))
        result = build_market_snapshot("unused", client=client)
        self.assertEqual(result["expected_session"], "2026-10-28")
        self.assertEqual(result["status"], "DEGRADED")

    def test_mixed_sessions_duplicate_quotes_and_malformed_feed_fail_closed(self):
        for kind in ("mixed", "duplicate", "fields", "object", "date", "code", "empty"):
            with self.subTest(kind=kind):
                client = MarketClient()
                if kind == "mixed": client.daily[0]["Date"] = "1151023"
                if kind == "duplicate": client.daily.append(deepcopy(client.daily[0]))
                if kind == "fields": client.daily[0].pop("ClosingPrice")
                if kind == "object": client.daily[0] = []
                if kind == "date": client.daily[0]["Date"] = "1150230"
                if kind == "code": client.daily[0]["Code"] = []
                if kind == "empty": client.daily = []
                with self.assertRaises(DataError):
                    build_market_snapshot("unused", client=client)

    def test_unusable_security_is_excluded_and_never_assigned_fabricated_numbers(self):
        cases = [{"LowestPrice": "110"}, {"ClosingPrice": "NaN"}, {"TradeVolume": "1.5"},
                 {"TradeVolume": False}, {"TradeValue": "Infinity"}, {"TradeVolume": "0"},
                 {"Transaction": "0"}, {"Change": "--"}, {"Change": "102"}]
        for updates in cases:
            with self.subTest(updates=updates):
                client = MarketClient()
                client.daily[1].update(updates)
                result = build_market_snapshot("unused", client=client)
                self.assertEqual(result["coverage"]["invalid_symbols"], ["6415"])
                self.assertEqual(result["status"], "DEGRADED")
                self.assertEqual([row["symbol"] for row in result["securities"]], ["2330"])

    def test_no_valid_quotes_cannot_publish_a_catalog(self):
        client = MarketClient()
        for row in client.daily:
            row["ClosingPrice"] = "--"
        with self.assertRaisesRegex(DataError, "NO_VALID_COMMON_STOCK_QUOTES"):
            build_market_snapshot("unused", client=client)

    def test_registry_duplicates_or_missing_company_metadata_are_rejected(self):
        for kind in ("duplicate", "industry", "name"):
            with self.subTest(kind=kind):
                client = MarketClient()
                if kind == "duplicate": client.registry.append(deepcopy(client.registry[0]))
                if kind == "industry": client.registry[0].pop("產業別")
                if kind == "name": client.registry[0]["公司簡稱"] = []
                with self.assertRaises(DataError):
                    build_market_snapshot("unused", client=client)

    def test_weekends_and_official_closed_days_cannot_be_daily_sessions(self):
        for day in ("1151024", "1151026"):
            with self.subTest(day=day):
                client = MarketClient()
                for row in client.daily:
                    row["Date"] = day
                with self.assertRaisesRegex(DataError, "SESSION_ON_CLOSED_DAY"):
                    build_market_snapshot("unused", client=client)

    def test_cross_year_checks_the_previous_year_holiday_source(self):
        client = MarketClient(now=datetime(2027, 1, 1, 10, tzinfo=TAIPEI))
        for row in client.registry:
            row["出表日期"] = "1160101"
        for row in client.daily:
            row["Date"] = "1151231"
        result = build_market_snapshot("unused", client=client)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["expected_session"], "2026-12-31")
        self.assertTrue(any("queryYear=115" in url for url, _ in client.calls))
        self.assertTrue(any("queryYear=116" in url for url, _ in client.calls))

    def test_unverified_cross_year_calendar_fails_instead_of_guessing(self):
        client = MarketClient(now=datetime(2027, 1, 1, 10, tzinfo=TAIPEI))
        for row in client.registry:
            row["出表日期"] = "1160101"
        client.closed.pop(2026)
        with self.assertRaisesRegex(DataError, "HOLIDAY_YEAR_UNAVAILABLE:2026"):
            build_market_snapshot("unused", client=client)

    def test_source_identity_and_future_timestamp_are_rejected(self):
        class WrongSource(MarketClient):
            def get(self, url, *, refresh=False):
                document = super().get(url, refresh=refresh)
                return Document("https://example.com/unverified", document.payload,
                                document.fetched_at, document.sha256, document.cache_file)
        with self.assertRaisesRegex(DataError, "INVALID_SOURCE_PROVENANCE"):
            build_market_snapshot("unused", client=WrongSource())
        class FutureSource(MarketClient):
            def get(self, url, *, refresh=False):
                document = super().get(url, refresh=refresh)
                return Document(document.url, document.payload,
                    self.current.replace(year=2027).isoformat(), document.sha256, document.cache_file)
        with self.assertRaisesRegex(DataError, "SOURCE_TIMESTAMP_IN_FUTURE"):
            build_market_snapshot("unused", client=FutureSource())

    def test_collection_crossing_the_publication_cutoff_requires_a_fresh_run(self):
        class CrossingCutoffClient(MarketClient):
            def __init__(self):
                super().__init__(now=NOW.replace(hour=15, minute=59))
                self.clock_reads = 0

            def now(self):
                self.clock_reads += 1
                return self.current if self.clock_reads == 1 else self.current.replace(hour=16, minute=0)
        with self.assertRaisesRegex(DataError, "COLLECTION_CROSSED_CUTOFF_RETRY"):
            build_market_snapshot("unused", client=CrossingCutoffClient())


if __name__ == "__main__":
    unittest.main()
