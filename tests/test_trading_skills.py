"""Unit, integration, leakage, accounting and paper-safety regression gates."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import ast
import json
import math
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from research.trading_skills.experiment import (load_protocol, digest, save, forecast_events, score_forecasts,
    replay, run, portfolio_metrics, market_regimes, VARIANTS)
from research.trading_skills.market import Costs, Market, capital_quote, decimal, instant
from research.trading_skills.simulation import pool_blocks, simulate, exit_price, calibrated_probability, calibration_metrics, setup_tags, rsi
from research.trading_skills.evidence import available_evidence
from research.trading_skills.forward import ForwardStore, known_bars
from research.trading_skills.review import validate_answer
from research.trading_skills.entry import opening_range_signal
from research.selection_lab.data import FIELDS, write_csv

ROOT = Path(__file__).resolve().parents[1]


def days(count=115):
    day, result = date(2026, 7, 1), []
    while len(result) < count:
        if day.weekday() < 5:
            result.append(day.isoformat())
        day += timedelta(days=1)
    return result


def bars(count=100, symbol="1234"):
    result = []
    for i, day in enumerate(days(count)):
        value = 100 + i * .03 + math.sin(i * .8)
        result.append(dict(symbol=symbol, exchange="TWSE", date=day, session_index=i,
                           open=value, high=value + 1.2, low=value - 1.2, close=value + .2,
                           volume=1000000, turnover_twd=100000000, available_at=day + "T14:00:00+08:00",
                           retrieved_at=day + "T14:01:00+08:00", source="TEST", source_sha256="a" * 64,
                           price_basis="RAW_UNADJUSTED_UNVERIFIED_ACTIONS"))
    return result


def quote(at, symbol="1234"):
    return dict(symbol=symbol, exchange="TWSE", source="CAPITAL_SKCOM", quote_basis="capital_intraday_odd_lot",
                instrument_type="COMMON_STOCK", retrieval_mode="BROKER", volume_unit="shares", quote_status="LIVE",
                is_trial=False, bid=100, ask=100.5, limit_down=90, limit_up=110,
                bid_size_shares=100, ask_size_shares=100, quote_timestamp=at, received_at=at, evidence_id="q-test")


class MarketAndCostUnitTests(unittest.TestCase):
    def setUp(self):
        self.p = load_protocol()
        self.m = Market()
        self.c = Costs(self.p["costs"])

    def test_tick_effective_date_and_boundary(self):
        self.assertEqual(self.m.tick(1000, "2026-11-25"), 5)
        self.assertEqual(self.m.tick(1000, "2027-07-05"), 1)
        self.assertEqual(self.m.round_price("999.9", "2026-11-25", True), 1000)
        self.assertEqual(self.m.round_price("1001", "2026-11-25"), 1000)

    def test_oddlot_session_effective_date(self):
        self.assertFalse(self.m.session_open("2026-11-25T09:05:00+08:00", ["2026-11-25"]))
        self.assertTrue(self.m.session_open("2026-12-07T09:05:00+08:00", ["2026-12-07"]))
        self.assertFalse(self.m.session_open("2026-11-25T13:30:00+08:00", ["2026-11-25"]))

    def test_holiday_and_timezone(self):
        self.assertFalse(self.m.session_open("2026-11-25T10:00:00+08:00", []))
        self.assertTrue(self.m.session_open("2026-11-25T02:00:00Z", ["2026-11-25"]))
        with self.assertRaises(ValueError):
            instant("2026-11-25T10:00:00")

    def test_symbols_preserve_leading_zero_and_reject_us(self):
        self.assertEqual(self.m.symbol("0050", "TWSE"), "TWSE:0050")
        for s in ("TSLA", "2330.TW", "12345", 2330):
            with self.assertRaises(ValueError):
                self.m.symbol(s, "TWSE")

    def test_cost_round_trip_includes_fee_and_tax(self):
        self.assertEqual(self.c.amount(100, 10, "BUY"), 1020)
        self.assertEqual(self.c.amount(100, 10, "SELL"), 977)
        self.assertEqual(self.c.amount(100, 10, "SELL") - self.c.amount(100, 10, "BUY"), -43)

    def test_budget_sizes_cash_with_fees(self):
        self.assertEqual(self.c.size(100, 1000, 1000, 100, 98), 9)
        self.assertEqual(self.c.size(100, 19, 1000, 100, 98), 0)
        self.assertEqual(self.c.size(100, 1000000, 1000000, 100000, 98), 999)

    def test_nominal_risk_includes_costs(self):
        qty = self.c.size(100, 10000, 10000, 50, 98)
        self.assertLessEqual(self.c.amount(100, qty, "BUY") - self.c.amount(98, qty, "SELL"), 50)

    def test_invalid_money_and_protocol(self):
        for v in (True, "NaN", "Infinity"):
            with self.assertRaises(ValueError):
                decimal(v)
        with self.assertRaises(ValueError):
            self.c.amount(100, -1, "BUY")
        with self.assertRaises(ValueError):
            Costs(dict(self.p["costs"], slippage_bps="-1"))

    def test_quote_accepts_capital_and_refuses_yahoo(self):
        at = "2026-11-25T10:00:00+08:00"
        self.assertEqual(capital_quote(quote(at), at, ["2026-11-25"], self.p)["ask"], Decimal("100.5"))
        q = quote(at)
        q["source"] = "Yahoo"
        with self.assertRaises(ValueError):
            capital_quote(q, at, ["2026-11-25"], self.p)

    def test_quote_age_future_trial_and_depth(self):
        at = "2026-11-25T10:00:00+08:00"
        for changes in (dict(quote_timestamp="2026-11-25T09:59:00+08:00"), dict(is_trial=True),
                        dict(received_at="2026-11-25T10:01:00+08:00"), dict(ask_size_shares=None),
                        dict(ask=120), dict(quote_status="LAST_KNOWN"), dict(bid=100.01)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                capital_quote(dict(quote(at), **changes), at, ["2026-11-25"], self.p)


class SimulationAndLeakageTests(unittest.TestCase):
    def setUp(self):
        self.p = load_protocol()
        self.p.update(paths=30, test_from=days()[61], minimum_turnover_twd=100)
        self.b = bars()
        self.asof = self.b[85]["date"]

    def test_blocks_retain_consecutive_rows(self):
        blocks = pool_blocks(self.b, self.asof, self.p, 3)
        self.assertTrue(blocks)
        self.assertTrue(all(b["end"] <= self.asof and len(b["factors"]) == 3 for b in blocks))
        first = blocks[0]
        i = next(i for i, b in enumerate(self.b) if b["date"] == first["start"])
        self.assertEqual(first["factors"][1][3], self.b[i + 1]["close"] / self.b[i]["close"])

    def test_future_prices_do_not_change_pool_or_forecast(self):
        changed = deepcopy(self.b)
        for row in changed[86:]:
            row.update(open=99999, high=999999, low=1, close=90000)
        a = pool_blocks(self.b, self.asof, self.p, 3)
        b = pool_blocks(changed, self.asof, self.p, 3)
        self.assertEqual(a, b)
        self.assertEqual(simulate(a, 100, self.asof, self.p), simulate(b, 100, self.asof, self.p))

    def test_future_market_state_does_not_change_past(self):
        a = market_regimes({"1234": self.b}, days(100))
        changed = deepcopy(self.b)
        changed[-1]["close"] *= 50
        b = market_regimes({"1234": changed}, days(100))
        self.assertEqual({k: v for k, v in a.items() if k < days(100)[-1]}, {k: v for k, v in b.items() if k < days(100)[-1]})

    def test_gap_and_illiquidity_blocks_excluded(self):
        changed = deepcopy(self.b)
        changed[50]["turnover_twd"] = 0
        changed[60]["session_index"] += 5
        blocks = pool_blocks(changed, self.asof, self.p, 3)
        self.assertFalse(any(x["start"] <= changed[50]["date"] <= x["end"] for x in blocks))
        self.assertFalse(any(x["start"] <= changed[60]["date"] <= x["end"] for x in blocks))

    def test_conditioning_does_not_use_outcome_regime(self):
        regimes = {d: "UP_LOW_VOL" for d in days(100)}
        regimes[self.asof] = "DOWN_LOW_VOL"
        blocks = pool_blocks(self.b, self.asof, self.p, 3, regimes, True)
        self.assertEqual(blocks, [])

    def test_sparse_pool_never_silently_falls_back(self):
        self.assertEqual(simulate([], 100, self.asof, self.p)["status"], "INSUFFICIENT_POOL")

    def test_seed_reproducibility(self):
        pool = pool_blocks(self.b, self.asof, self.p, 3)
        self.assertEqual(simulate(pool, 100, self.asof, self.p, seed=6), simulate(pool, 100, self.asof, self.p, seed=6))

    def test_gap_and_double_touch_stop_first(self):
        self.assertEqual(exit_price(dict(open=90, low=89, high=110, close=100), Decimal(98), Decimal(102)), (90, "GAP_STOP"))
        self.assertEqual(exit_price(dict(open=100, low=97, high=103, close=100), Decimal(98), Decimal(102)), (98, "STOP"))

    def test_simulation_fees_reduce_expectancy(self):
        pool = pool_blocks(self.b, self.asof, self.p, 3)
        costly = simulate(pool, 100, self.asof, self.p)
        p = deepcopy(self.p)
        p["costs"] = dict(commission_rate="0", minimum_commission_twd="0", sell_tax_rate="0", slippage_bps="0")
        free = simulate(pool, 100, self.asof, p)
        self.assertLess(costly["expected_net_twd"], free["expected_net_twd"])

    def test_calibration_requires_full_maturity_before_cutoff(self):
        self.p["calibration_minimum"] = 2
        rows = [dict(signal_date="2026-01-01", ready_at="2026-02-01", probability=.7, outcome=1) for _ in range(2)]
        self.assertIsNone(calibrated_probability(rows, .7, "2026-02-01", self.p)["probability"])
        self.assertEqual(calibrated_probability(rows, .7, "2026-02-02", self.p)["probability"], .75)

    def test_calibration_metrics_known_result(self):
        m = calibration_metrics([(.75, 1), (.25, 0)])
        self.assertEqual(m["brier"], .0625)
        self.assertEqual(m["ece"], .25)

    def test_scanner_shapes_and_flat_rsi(self):
        self.assertIn("breakout_20d", setup_tags(list(range(100, 161))))
        self.assertEqual(rsi([100] * 61), 50)

    def test_historical_publication_missing_or_late_rejected(self):
        late = deepcopy(self.b)
        late[30]["available_at"] = "2027-01-01T00:00:00Z"
        with self.assertRaises(ValueError):
            pool_blocks(late, self.asof, self.p, 3, strict_availability=True)


class EvidenceTests(unittest.TestCase):
    def test_restatements_after_decision_do_not_replace_original(self):
        base = dict(symbol="2330", kind="fundamental", metric="revenue", period_end="2026-06-30",
                    published_at="2026-08-01T00:00:00Z", received_at="2026-08-01T01:00:00Z", evidence_id="a", source_url="https://mops.twse.com.tw/")
        later = dict(base, published_at="2026-12-01T00:00:00Z", received_at="2026-12-01T01:00:00Z", evidence_id="b")
        result = available_evidence([base, later], "2026-11-25T00:00:00Z")
        self.assertEqual([r["evidence_id"] for r in result["records"]], ["a"])
        self.assertEqual(len(result["rejected"]), 1)

    def test_period_end_is_not_publication_time(self):
        self.assertEqual(len(available_evidence([dict(period_end="2026-06-30")], "2026-11-25T00:00:00Z")["records"]), 0)

    def test_absent_news_is_unknown(self):
        self.assertEqual(available_evidence([], "2026-11-25T00:00:00Z")["completeness"], "UNKNOWN_UNLESS_SEPARATELY_ATTESTED")


class OpeningRangeTests(unittest.TestCase):
    def minute_bars(self):
        result = []
        for i, minute in enumerate(("00", "05", "10", "15", "20")):
            result.append(dict(symbol="2330", start="2026-11-25T09:" + minute + ":00+08:00", open=100, high=102,
                               low=99, close=101 if i < 3 else 103, volume=100 if i < 3 else 200,
                               received_at="2026-11-25T09:20:00+08:00", available_at="2026-11-25T09:20:00+08:00", evidence_id=str(i)))
        result[3]["high"] = 104
        result[4]["high"] = 104
        return result

    def test_only_completed_bars_can_trigger(self):
        result = opening_range_signal(self.minute_bars(), "2026-11-25T09:19:00+08:00", ["2026-11-25"])
        self.assertIsNone(result["trigger"])
        result = opening_range_signal(self.minute_bars(), "2026-11-25T09:20:00+08:00", ["2026-11-25"])
        self.assertEqual(result["trigger"]["evidence_id"], "3")

    def test_missing_range_bar_refuses_confirmation(self):
        result = opening_range_signal(self.minute_bars()[1:], "2026-11-25T09:20:00+08:00", ["2026-11-25"])
        self.assertEqual(result["status"], "INCOMPLETE_OPENING_RANGE")

    def test_future_partial_bar_high_cannot_change_signal(self):
        rows = self.minute_bars()
        a = opening_range_signal(rows, "2026-11-25T09:20:00+08:00", ["2026-11-25"])
        rows[-1]["high"] = 999999
        b = opening_range_signal(rows, "2026-11-25T09:20:00+08:00", ["2026-11-25"])
        self.assertEqual(a, b)


class PortfolioAndIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.p = load_protocol()
        self.p.update(paths=10, test_from=days()[61])
        self.b = bars(75)
        self.events = {days()[61]: [dict(symbol="1234", close=self.b[61]["close"], previous_volume=1000000,
            setups=[], simulations={v: dict(status="OK", expected_net_twd=10) for v in VARIANTS[2:]})]}

    def test_cash_and_equity_reconcile_every_day(self):
        result = replay({"1234": self.b}, days(75), self.events, self.p, VARIANTS[0])
        self.assertTrue(result["trades"])
        for row in result["equity"]:
            self.assertGreaterEqual(row["cash_twd"], 0)
            self.assertAlmostEqual(row["cash_twd"] + row["unsettled_twd"] + row["liquidation_value_twd"], row["liquidation_equity_twd"])

    def test_no_same_bar_entry_exit(self):
        changed = deepcopy(self.b)
        changed[62].update(high=120, low=80)
        result = replay({"1234": changed}, days(75), self.events, self.p, VARIANTS[0])
        self.assertFalse(any(r["side"] == "SELL" and r["date"] == days()[62] for r in result["trades"]))

    def test_entry_size_does_not_use_future_bar_volume(self):
        a = replay({"1234": self.b}, days(75), self.events, self.p, VARIANTS[0])
        changed = deepcopy(self.b)
        changed[62]["volume"] = 1
        b = replay({"1234": changed}, days(75), self.events, self.p, VARIANTS[0])
        self.assertEqual(a["trades"][0], b["trades"][0])

    def test_sale_proceeds_wait_two_sessions(self):
        changed = deepcopy(self.b)
        changed[63].update(open=105, high=110, low=104, close=106)
        result = replay({"1234": changed}, days(75), self.events, self.p, VARIANTS[0])
        rows = {r["date"]: r for r in result["equity"]}
        self.assertGreater(rows[days()[63]]["unsettled_twd"], 0)
        self.assertEqual(rows[days()[63]]["cash_twd"], rows[days()[64]]["cash_twd"])
        self.assertEqual(rows[days()[65]]["unsettled_twd"], 0)

    def test_no_backfill_after_skill_exclusion(self):
        r = replay({"1234": self.b}, days(75), self.events, self.p, "skill_scanner")
        self.assertEqual(r["trades"], [])
        self.assertEqual(r["rejected"]["NO_SCANNER_SETUP"], 1)

    def test_metrics_use_account_nav_including_initial_capital(self):
        eq = [dict(liquidation_equity_twd=x) for x in (900, 1100, 1000)]
        m = portfolio_metrics(eq, [dict(side="SELL", net_pnl_twd=10), dict(side="SELL", net_pnl_twd=-5)], 1000, .05)
        self.assertAlmostEqual(m["maximum_drawdown"], .1)
        self.assertEqual(m["net_return"], 0)
        self.assertEqual(m["profit_factor"], 2)
        self.assertEqual(m["expectancy_twd"], 2.5)
        self.assertEqual(m["benchmark_relative_return"], -.05)

    def test_end_to_end_independent_run_and_no_overwrite(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "research-runs") as tmp:
            folder = Path(tmp)
            dataset = folder / "dataset"
            dataset.mkdir()
            write_csv(dataset / "bars.csv", FIELDS, self.b)
            write_csv(dataset / "sessions.csv", ["date"], [dict(date=d) for d in days(75)])
            save(dataset / "manifest.json", dict(rows=75, files={name: digest((dataset / name).read_bytes()) for name in ("bars.csv", "sessions.csv")}))
            result = run(dataset, folder / "run", self.p)
            self.assertEqual(set(result["variants"]), set(VARIANTS))
            self.assertFalse(result["point_in_time_certified"])
            events = json.loads((folder / "run/decision_events.json").read_text())
            self.assertNotIn("outcome", json.dumps(events))
            with self.assertRaises(FileExistsError):
                run(dataset, folder / "run", self.p)


class ForwardSafetyTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "research-runs").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "research-runs")
        self.store = ForwardStore(Path(self.tmp.name) / "paper.sqlite3")
        self.p = load_protocol()
        self.p["paths"] = 10
        self.history = bars(80)
        self.sessions = days(100)
        self.day = self.sessions[80]
        self.at = self.day + "T10:00:00+08:00"
        self.version = self.store.register(self.p, self.day + "T09:00:00+08:00")
        self.snapshot = dict(mode="PAPER_ONLY", at=self.at, historical_membership_verified=True,
                             sessions=self.sessions, bars=self.history, quotes=[quote(self.at)])

    def tearDown(self):
        # Close SQLite contexts explicitly: sqlite context managers don't close.
        self.tmp.cleanup()

    def positive_cycle(self):
        with patch("research.trading_skills.forward.simulate", return_value=dict(status="OK", expected_net_twd=20)):
            return self.store.cycle(self.version, self.snapshot, self.at)

    def test_forward_paper_fill_without_broker_import(self):
        result = self.positive_cycle()
        self.assertEqual(result["mode"], "PAPER_ONLY")
        self.assertTrue(result["simulated_fills"])
        self.assertGreaterEqual(decimal(result["cash_twd"]), 0)

    def test_live_mode_rejected_at_registration_and_snapshot(self):
        with self.assertRaises(ValueError):
            self.store.register(dict(self.p, mode="LIVE"), self.at)
        self.snapshot["mode"] = "LIVE"
        with self.assertRaises(ValueError):
            self.positive_cycle()

    def test_repeated_or_earlier_cycle_cannot_rewrite(self):
        self.positive_cycle()
        with self.assertRaises(ValueError):
            self.positive_cycle()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE runs SET at='x'")

    def test_rollback_control_keeps_account_and_history(self):
        self.positive_cycle()
        self.store.set_active(self.version, False, self.at)
        self.snapshot["at"] = self.day + "T10:01:00+08:00"
        with self.assertRaises(ValueError):
            self.store.cycle(self.version, self.snapshot, self.snapshot["at"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM controls").fetchone()[0], 2)

    def test_new_version_does_not_reset_old_account(self):
        self.positive_cycle()
        other = self.store.register(dict(self.p, seed=123), self.at)
        self.assertNotEqual(other, self.version)
        with self.store.connect() as db:
            old, _, _, _ = self.store._account(db, self.version, self.p, self.day)
            new, _, _, _ = self.store._account(db, other, self.p, self.day)
        self.assertLess(old, new)
        self.assertEqual(new, decimal(self.p["initial_cash_twd"]))

    def test_forward_bar_receipt_and_future_prices_rejected(self):
        self.snapshot["bars"][-1]["retrieved_at"] = "2027-01-01T00:00:00Z"
        with self.assertRaises(ValueError):
            self.positive_cycle()

    def test_source_version_drift_requires_new_account(self):
        with patch("research.trading_skills.forward.digest", return_value="changed"):
            with self.assertRaisesRegex(ValueError, "SOURCE_CHANGED"):
                self.positive_cycle()

    def test_store_cannot_point_to_production_ledger(self):
        with self.assertRaises(ValueError):
            ForwardStore(ROOT / "data/execution/live_execution.sqlite3")

    def test_old_or_missing_depth_snapshot_never_creates_fill(self):
        self.snapshot["quotes"][0].pop("ask_size_shares")
        result = self.positive_cycle()
        self.assertFalse(result["simulated_fills"])
        self.assertEqual(result["decisions"][0]["status"], "QUOTE_REJECTED")

    def test_partial_exit_retains_basis_and_does_not_reuse_unsettled_cash(self):
        first = self.positive_cycle()
        qty = first["simulated_fills"][0]["qty"]
        at = self.day + "T10:01:00+08:00"
        self.snapshot.update(at=at, quotes=[dict(quote(at), bid=97, ask=97.1, bid_size_shares=10)])
        result = self.store.cycle(self.version, self.snapshot, at)
        self.assertEqual(result["simulated_fills"][0]["side"], "SELL")
        self.assertEqual(result["simulated_fills"][0]["qty"], 10)
        with self.store.connect() as db:
            cash, unsettled, _, positions = self.store._account(db, self.version, self.p, self.day)
        self.assertEqual(positions["1234"]["qty"], qty - 10)
        self.assertGreater(positions["1234"]["basis"], 0)
        self.assertEqual(cash, decimal(first["cash_twd"]))
        self.assertGreater(unsettled, 0)

    def test_source_archive_is_immutable(self):
        with self.store.connect() as db:
            row = db.execute("SELECT path,sha256,content FROM source_archives LIMIT 1").fetchone()
            self.assertEqual(row["sha256"], digest(row["content"].encode()))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM source_archives")

    def test_source_archive_preserves_crlf_bytes_and_hash(self):
        folder = Path(self.tmp.name) / "archive_inputs"
        folder.mkdir()
        raw = "# 台股\r\nVALUE = 1\r\n".encode("utf-8")
        (folder / "forward.py").write_bytes(raw)
        with patch("research.trading_skills.forward.__file__", str(folder / "forward.py")):
            version = self.store.register(self.p, self.at)
        with self.store.connect() as db:
            row = db.execute("SELECT sha256,content FROM source_archives WHERE version=? AND path=?",
                             (version, "forward.py")).fetchone()
        self.assertEqual(row["content"].encode("utf-8"), raw)
        self.assertEqual(row["sha256"], digest(raw))


class ReviewAndIsolationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.packet = dict(packet_id="p", candidates=[dict(symbol="2330", evidence_id="e")])
        self.answer = dict(packet_id="p", opinions=[dict(symbol="2330", evidence_id="e", verdict="WATCH", thesis="假說", counterevidence="反證", missing_data=["公告"])])

    def test_review_requires_exact_evidence_coverage(self):
        self.assertEqual(validate_answer(self.answer, self.packet), self.answer)
        self.answer["opinions"][0]["evidence_id"] = "wrong"
        with self.assertRaises(ValueError):
            validate_answer(self.answer, self.packet)

    def test_review_cannot_return_executable_buy(self):
        self.answer["opinions"][0]["verdict"] = "BUY"
        with self.assertRaises(ValueError):
            validate_answer(self.answer, self.packet)

    def test_duplicate_opinion_does_not_hide_missing_candidate(self):
        self.answer["opinions"] *= 2
        with self.assertRaises(ValueError):
            validate_answer(self.answer, self.packet)

    def test_pure_research_imports_do_not_load_production(self):
        code = "import sys; import research.trading_skills.__main__; assert not any(x in sys.modules for x in ('capital','config','execution.capital_executor','strategy_experiment','openai'))"
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_order_transport_in_module_ast(self):
        forbidden = {"capital", "config", "execution", "comtypes", "requests", "socket", "urllib"}
        for path in (ROOT / "research/trading_skills").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else ([node.module.split(".")[0]] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else [])
                self.assertFalse(forbidden.intersection(names), path)


if __name__ == "__main__":
    unittest.main()
