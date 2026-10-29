from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import unittest

from support_scout.contracts import DataError, digest
from support_scout.demo import demo_bundle
from support_scout.discovery_tools import DiscoveryTools, HistoryRepository
from support_scout.pipeline import run


def history(symbol="3552"):
    value = demo_bundle()
    value["securities"] = [value["securities"][0]]
    value["securities"][0].update(symbol=symbol, name="動態測試" + symbol)
    value["collection"] = {"status": "COMPLETE", "sources": [{"url": "fixture:" + symbol}], "errors": []}
    return value


def market(symbols=("3552", "4422", "6789")):
    value = history()
    return {"catalog_id": "dynamic-market-test", "as_of": value["as_of"],
        "session": value["calendar"]["sessions"][-1], "synthetic": True,
        "securities": [{"symbol": symbol, "name": "測試" + symbol,
            "industry": "半導體" if index % 2 == 0 else "傳產", "session": value["calendar"]["sessions"][-1],
            "close": 100 + index, "volume_shares": 1_000_000, "turnover_twd": (index + 1) * 10_000_000,
            "change_pct": (index - 1) * 2} for index, symbol in enumerate(symbols)]}


class HistoryRepositoryTests(unittest.TestCase):
    def test_new_symbol_outside_old_pool_has_real_support_evidence_and_is_cached(self):
        calls = []
        original = history()
        def loader(symbol):
            calls.append(symbol)
            return original
        repository = HistoryRepository(market(), loader)
        first = repository.inspect("3552")
        self.assertEqual(first["symbol"], "3552")
        self.assertTrue(first["technical"]["technical_candidate"])
        self.assertEqual(first["history_evidence_id"], "history:3552:" + digest(original)[:20])
        self.assertEqual(first["total_bar_count"], 72)
        self.assertFalse(first["bars_truncated"])
        first["recent_bars"][0]["close"] = 1
        original["securities"][0]["bars"][0]["close"] = 2
        again = repository.inspect("3552")
        self.assertNotEqual(again["recent_bars"][0]["close"], 1)
        self.assertNotEqual(again["recent_bars"][0]["close"], 2)
        self.assertEqual(calls, ["3552"])

    def test_concurrent_scouts_fetch_once_without_sharing_their_tool_results(self):
        calls = []
        def loader(symbol):
            calls.append(symbol)
            return history(symbol)
        repository = HistoryRepository(market(), loader)
        scouts = [DiscoveryTools(repository), DiscoveryTools(repository)]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda tools: tools.execute("inspect_support", {"symbol": "3552"}), scouts))
        self.assertEqual(calls, ["3552"])
        results[0]["technical"]["state"] = "FAKE"
        self.assertNotEqual(scouts[1].inspected["3552"]["technical"]["state"], "FAKE")
        self.assertEqual(len(scouts[0].audit), 1)
        self.assertEqual(len(scouts[1].audit), 1)

    def test_wrong_symbol_future_synthetic_and_market_session_are_rejected(self):
        cases = []
        wrong = history("4422")
        cases.append((wrong, "SYMBOL_MISMATCH"))
        future = history()
        future["as_of"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        cases.append((future, "CUTOFF_IN_FUTURE"))
        mixed = history()
        mixed["synthetic"] = False
        cases.append((mixed, "SYNTHETIC_MISMATCH"))
        stale = history()
        stale["calendar"]["sessions"].pop()
        cases.append((stale, "SESSION_MISMATCH"))
        for bundle, expected in cases:
            with self.subTest(expected=expected):
                repository = HistoryRepository(market(), lambda symbol: bundle)
                with self.assertRaisesRegex(DataError, expected):
                    repository.inspect("3552")
                self.assertEqual(repository.bundles, {})

    def test_inspection_returns_at_most_one_hundred_visible_bars_with_explicit_coverage(self):
        bundle = history()
        earlier = []
        cursor = date.fromisoformat(bundle["calendar"]["sessions"][0]) - timedelta(days=1)
        while len(earlier) < 40:
            if cursor.weekday() < 5:
                earlier.append(cursor.isoformat())
            cursor -= timedelta(days=1)
        earlier.reverse()
        prefix = []
        for session in earlier:
            row = deepcopy(bundle["securities"][0]["bars"][0])
            row.update(session=session, closed_at=session + "T13:33:00+08:00",
                       available_at=session + "T15:00:00+08:00")
            prefix.append(row)
        bundle["calendar"]["sessions"] = earlier + bundle["calendar"]["sessions"]
        bundle["securities"][0]["bars"] = prefix + bundle["securities"][0]["bars"]
        result = HistoryRepository(market(), lambda symbol: bundle).inspect("3552")
        self.assertEqual(result["total_bar_count"], 112)
        self.assertEqual(len(result["recent_bars"]), 100)
        self.assertTrue(result["bars_truncated"])
        self.assertEqual(result["recent_bars"][0]["session"], bundle["securities"][0]["bars"][-100]["session"])

    def test_invalid_prices_and_loader_failures_are_controlled_and_cached(self):
        invalid = history()
        invalid["securities"][0]["bars"][-1]["high"] = 1
        with self.assertRaisesRegex(DataError, "DATA_REJECTED"):
            HistoryRepository(market(), lambda symbol: invalid).inspect("3552")
        calls = []
        def failing(symbol):
            calls.append(symbol)
            raise RuntimeError("private sk-secret-do-not-persist")
        repository = HistoryRepository(market(), failing)
        for _ in range(2):
            with self.assertRaisesRegex(DataError, "SOURCE_UNAVAILABLE"):
                repository.inspect("3552")
        self.assertEqual(calls, ["3552"])
        with self.assertRaisesRegex(DataError, "NOT_IN_MARKET"):
            repository.inspect("2330")

    def test_combine_preserves_latest_cutoff_original_availability_and_sources(self):
        calls = []
        bundles = {symbol: history(symbol) for symbol in ("3552", "4422")}
        bundles["4422"]["as_of"] = "2026-09-08T17:00:00+08:00"
        def loader(symbol):
            calls.append(symbol)
            return bundles[symbol]
        repository = HistoryRepository(market(), loader)
        repository.inspect("3552")
        repository.inspect("4422")
        combined = repository.combine(["3552", "4422"])
        self.assertEqual(combined["as_of"], bundles["4422"]["as_of"])
        self.assertEqual(combined["securities"][0]["bars"], bundles["3552"]["securities"][0]["bars"])
        self.assertEqual(len(combined["collection"]["sources"]), 2)
        self.assertEqual(len(combined["discovery_source_snapshots"]), 2)
        self.assertTrue(combined["universe_version"].startswith("agent-discovery-"))
        self.assertTrue(all(s["data_status"] == "VALID" for s in run(combined)["securities"]))
        self.assertEqual(calls, ["3552", "4422"])
        combined["securities"][0]["bars"][0]["close"] = 0
        self.assertNotEqual(repository.combine(["3552"])["securities"][0]["bars"][0]["close"], 0)

    def test_combine_requires_inspection_nonempty_unique_and_identical_calendar(self):
        bundles = {symbol: history(symbol) for symbol in ("3552", "4422")}
        bundles["4422"]["calendar"]["sessions"].pop(0)
        bundles["4422"]["securities"][0]["bars"].pop(0)
        repository = HistoryRepository(market(), lambda symbol: bundles[symbol])
        for symbols in ([], ["3552"], ["3552", "3552"]):
            with self.assertRaises(DataError):
                repository.combine(symbols)
        repository.inspect("3552")
        repository.inspect("4422")
        with self.assertRaisesRegex(DataError, "CALENDAR_MISMATCH"):
            repository.combine(["3552", "4422"])

    def test_combine_preserves_degraded_market_coverage_and_all_source_provenance(self):
        catalog = market()
        catalog.update(status="DEGRADED", errors=["MARKET_COMMON_STOCK_QUOTES_MISSING"],
                       sources=[{"url": "fixture:market"}], coverage={"missing_symbols": ["1111"]})
        repository = HistoryRepository(catalog, history)
        repository.inspect("3552")
        combined = repository.combine(["3552"])
        self.assertEqual(combined["collection"]["status"], "DEGRADED")
        self.assertIn({"url": "fixture:market"}, combined["collection"]["sources"])
        self.assertEqual(combined["discovery_market_snapshot"]["coverage"], catalog["coverage"])
        self.assertIn("DATA_COLLECTION_DEGRADED", run(combined)["securities"][0]["reasons"])


class DiscoveryToolTests(unittest.TestCase):
    def setUp(self):
        self.market = market()
        self.repository = HistoryRepository(self.market, history)
        self.tools = DiscoveryTools(self.repository)

    def test_overview_and_pagination_cover_dynamic_market_without_preselection(self):
        overview = self.tools.execute("market_overview", {})
        self.assertEqual(overview["market_total"], 3)
        self.assertEqual(self.tools.seen_symbols, set())
        first = self.tools.execute("search_market", {"limit": 2})
        second = self.tools.execute("search_market", {"offset": 2, "limit": 2})
        self.assertEqual(first["matched_total"], 3)
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual(self.tools.seen_symbols, {"3552", "4422", "6789"})
        self.assertEqual(self.repository.bundles, {})
        self.assertEqual(self.tools.audit[1]["arguments"], {"limit": 2})
        self.assertEqual(self.tools.audit[1]["result_digest"], digest(first))

    def test_filters_and_each_sort_are_model_selected(self):
        results = self.tools.execute("search_market", {"industry": "半導體", "min_turnover_twd": 20_000_000})
        self.assertEqual([r["symbol"] for r in results["securities"]], ["6789"])
        for sort, expected in (("symbol", ["3552", "4422", "6789"]),
                               ("turnover_desc", ["6789", "4422", "3552"]),
                               ("change_asc", ["3552", "4422", "6789"]),
                               ("change_abs_asc", ["4422", "3552", "6789"])):
            with self.subTest(sort=sort):
                output = self.tools.execute("search_market", {"sort": sort})
                self.assertEqual([r["symbol"] for r in output["securities"]], expected)
        output = self.tools.execute("search_market", {"query": "4422"})
        self.assertEqual(output["matched_total"], 1)
        output["securities"][0]["name"] = "poison"
        self.market["securities"][0]["name"] = "another poison"
        self.assertNotEqual(self.repository.market["securities"][1]["name"], "poison")
        self.assertNotEqual(self.repository.market["securities"][0]["name"], "another poison")

    def test_unknown_extra_and_invalid_arguments_fail_and_consume_budget(self):
        cases = [("shell", {}), ("market_overview", {"extra": True}),
                 ("inspect_support", {"symbol": "3552", "extra": True}),
                 ("search_market", {"limit": True}), ("search_market", {"offset": -1}),
                 ("search_market", {"min_turnover_twd": float("inf")}),
                 ("search_market", {"industry": []}), ("search_market", {"sort": []})]
        tools = DiscoveryTools(self.repository, max_tool_calls=9)
        for name, args in cases:
            self.assertEqual(tools.execute(name, args)["status"], "ERROR")
        self.assertEqual(tools.tool_call_count, len(cases))
        self.assertEqual(tools.execute("market_overview", {})["status"], "OK")
        self.assertIn("TOOL_BUDGET_EXHAUSTED", tools.execute("market_overview", {})["error"])
        self.assertEqual(len(tools.audit), 10)
        json.dumps(tools.audit, allow_nan=False)

    def test_history_budget_is_per_scout_and_repeated_symbol_uses_cache(self):
        first = DiscoveryTools(self.repository, max_history_symbols=1)
        second = DiscoveryTools(self.repository, max_history_symbols=1)
        result = first.execute("inspect_support", {"symbol": "3552"})
        self.assertEqual(result["status"], "OK")
        self.assertEqual(first.execute("inspect_support", {"symbol": "3552"})["status"], "OK")
        self.assertIn("HISTORY_BUDGET_EXHAUSTED", first.execute("inspect_support", {"symbol": "4422"})["error"])
        self.assertEqual(second.execute("inspect_support", {"symbol": "4422"})["status"], "OK")
        self.assertNotIn("3552", second.inspected)
        self.assertEqual(first.inspected["3552"]["history_evidence_id"], result["history_evidence_id"])


if __name__ == "__main__":
    unittest.main()
