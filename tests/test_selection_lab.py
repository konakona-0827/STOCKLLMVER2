"""Time, provenance, denominator and adapter invariants for the isolated lab."""
from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from research.selection_lab.data import digest, normalize, load_dataset, write_csv, write_json, FIELDS, download, export_adapters
from research.selection_lab.experiment import DEFAULT_PROTOCOL, features, rank, build_observations, summarize, snapshot, evaluate, validate_protocol
from research.selection_lab.costs import api_estimate
from research.selection_lab.review import review_factorial


class SelectionLabTests(unittest.TestCase):
    def setUp(self):
        self.protocol = json.loads(DEFAULT_PROTOCOL.read_text(encoding="utf-8"))
        self.protocol["minimum_average_turnover_twd"] = 0
        self.protocol["test_from"] = "2026-01-01"
        self.days = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(80)]

    def bars(self, symbol="1234"):
        return [dict(symbol=symbol, exchange="TWSE", date=day, session_index=i,
                     open=100 + i * .1, high=102 + i * .1, low=99 + i * .1,
                     close=101 + i * .1, volume=10000, turnover_twd=1000000,
                     retrieved_at="2026-11-11T06:00:00+00:00", source="TEST",
                     source_sha256="test", price_basis="RAW_UNADJUSTED_UNVERIFIED_ACTIONS") for i, day in enumerate(self.days)]

    def dataset(self, directory, bars=None):
        out = Path(directory) / "dataset"
        out.mkdir()
        write_csv(out / "bars.csv", FIELDS, bars or self.bars())
        write_csv(out / "sessions.csv", ["date"], [dict(date=d) for d in self.days])
        write_json(out / "manifest.json", dict(rows=80, files={name: digest((out / name).read_bytes()) for name in ("bars.csv", "sessions.csv")}))
        return out

    def test_finmind_uses_actual_money_not_trade_count_or_price_times_volume(self):
        rows, errors = normalize([dict(stock_id="0050", date="2026-01-02", open=100,
                                      max=102, min=99, close=101, Trading_Volume=200,
                                      Trading_money=19999, Trading_turnover=12)], "0050", "TWSE", "now", "hash")
        self.assertFalse(errors)
        self.assertEqual(rows[0]["symbol"], "0050")
        self.assertEqual(rows[0]["turnover_twd"], 19999)

    def test_missing_money_stays_unknown(self):
        rows, _ = normalize([dict(stock_id="1234", date="2026-01-02", open=100,
                                 max=102, min=99, close=101, Trading_Volume=200)], "1234", "TWSE", "now", "hash")
        self.assertIsNone(rows[0]["turnover_twd"])

    def test_invalid_prices_and_fractional_volume_are_rejected(self):
        for changed in (dict(close=0), dict(Trading_Volume=1.5), dict(max=90), dict(close=float("nan"))):
            raw = dict(stock_id="1234", date="2026-01-02", open=100, max=102, min=99, close=101, Trading_Volume=200)
            raw.update(changed)
            rows, errors = normalize([raw], "1234", "TWSE", "now", "hash")
            self.assertFalse(rows)
            self.assertEqual(len(errors), 1)

    def test_feature_prefix_is_unchanged_by_future_prices(self):
        bars = self.bars()
        expected, _ = features(bars, 60, self.protocol)
        for bar in bars[61:]:
            bar.update(open=9999, high=10000, low=9998, close=9999, volume=1)
        actual, _ = features(bars, 60, self.protocol)
        self.assertEqual(actual, expected)

    def test_missing_warmup_session_is_not_compressed(self):
        bars = self.bars()
        del bars[30]
        row, reason = features(bars, 61, self.protocol)
        self.assertIsNone(row)
        self.assertEqual(reason, "INSUFFICIENT_OR_GAPPED_WARMUP")

    def test_missing_actual_amount_blocks_feature(self):
        bars = self.bars()
        bars[60]["turnover_twd"] = None
        self.assertEqual(features(bars, 60, self.protocol)[1], "ACTUAL_TURNOVER_MISSING")

    def test_large_discontinuity_is_visible(self):
        bars = self.bars()
        bars[59]["close"] *= .5
        self.assertEqual(features(bars, 60, self.protocol)[1], "POSSIBLE_CORPORATE_ACTION_OR_PRICE_DISCONTINUITY")

    def test_ties_have_equal_scores(self):
        row, _ = features(self.bars(), 60, self.protocol)
        other = dict(row, symbol="9999")
        result = rank([row, other], "composite")
        self.assertEqual(result[0]["rank_score"], result[1]["rank_score"])

    def test_future_gap_does_not_remove_eligible_signal(self):
        bars = self.bars()
        del bars[63]
        rows, reasons = build_observations({"1234": bars}, self.days, self.protocol)
        signal = next(r for r in rows if r["features"]["signal_date"] == self.days[60])
        self.assertIsNone(signal["label"])
        self.assertEqual(signal["unscored_reason"], "FUTURE_SESSION_GAP")

    def test_incomplete_window_remains_unscored_even_if_target_touched(self):
        rows, _ = build_observations({"1234": self.bars()}, self.days, self.protocol)
        tail = rows[-1]
        self.assertIsNone(tail["label"])
        self.assertEqual(tail["unscored_reason"], "IMMATURE_FULL_WINDOW")

    def test_summary_does_not_backfill_or_hide_unscored_selection(self):
        rows = [dict(features=dict(symbol="1234", signal_date="2026-01-01"), label=None),
                dict(features=dict(symbol="9999", signal_date="2026-01-01"),
                     label=dict(status="COMPLETE", net_pnl_twd="20", buy_cost_twd="1000", holding_trading_days=2,
                                net_return=".02", ambiguous_bar=False))]
        result = summarize(rows, {("1234", "2026-01-01")})
        self.assertEqual((result["selected"], result["scored"], result["unscored"]), (1, 0, 1))
        self.assertEqual(result["missed_positive_opportunities"], 1)

    def test_hash_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self.dataset(tmp)
            with (folder / "bars.csv").open("a") as stream:
                stream.write("tampering")
            with self.assertRaisesRegex(ValueError, "HASH_MISMATCH"):
                load_dataset(folder)

    def test_missing_latest_bar_is_not_replaced_with_stale_features(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self.dataset(tmp, self.bars()[:-1])
            result = snapshot(folder, self.days[-1], self.protocol)
            self.assertEqual(result["eligible_candidates"], [])
            self.assertEqual(result["excluded"][0]["reason"], "LATEST_SESSION_MISSING")

    def test_export_has_no_fabricated_adjustment_factor(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self.dataset(tmp)
            out = export_adapters(folder, Path(tmp) / "export")
            header = (out / "qlib_raw_features/tw1234.csv").read_text().splitlines()[0]
            self.assertNotIn("factor", header)
            self.assertFalse(json.loads((out / "manifest.json").read_text())["adjusted_qlib_training_ready"])

    def test_evaluation_rejects_existing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self.dataset(tmp)
            out = Path(tmp) / "result"
            result = evaluate(folder, out, self.protocol)
            self.assertFalse(result["superiority_proven"])
            with self.assertRaises(FileExistsError):
                evaluate(folder, out, self.protocol)

    def test_no_live_modules_imported_and_no_environment_required(self):
        code = "import sys; import research.selection_lab.__main__; assert not ({'config','broker_runtime','openai','trading_agent','execution_live'} & set(sys.modules))"
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_download_refuses_today_before_any_network_call(self):
        with patch("research.selection_lab.data.FinMind.fetch") as fetch:
            with self.assertRaisesRegex(ValueError, "END_MUST_PRECEDE"):
                download("unused", ["2330"], "2023-01-01", "2999-01-01")
            fetch.assert_not_called()

    def test_api_estimate_does_not_double_count_cached_input_or_reasoning(self):
        self.assertAlmostEqual(api_estimate(1000000, 100000, 200000), 2.64)
        with self.assertRaises(ValueError):
            api_estimate(10, 10, 11)

    def test_training_boundary_must_precede_test_period(self):
        protocol = deepcopy(self.protocol)
        protocol["train_through"] = protocol["test_from"]
        with self.assertRaisesRegex(ValueError, "MUST_NOT_OVERLAP"):
            validate_protocol(protocol)

    def test_snapshot_rejects_model_scores_from_a_different_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self.dataset(tmp)
            scores = Path(tmp) / "scores"
            scores.mkdir()
            write_json(scores / "scores_manifest.json", dict(dataset_manifest_sha256="wrong"))
            with self.assertRaisesRegex(ValueError, "PROVENANCE_OR_TIME"):
                snapshot(folder, self.days[-1], self.protocol, scores)

    def review_fixture(self, root, fail=False, wrong_evidence=False):
        folder = self.dataset(root)
        packet = snapshot(folder, self.days[-1], self.protocol)
        packet_path = Path(root) / "packet.json"
        write_json(packet_path, packet)
        transport = Path(root) / "codex_cli.py"
        # Fake transport: no OpenAI import, authentication, network or billing.
        transport.write_text('''import json
from types import SimpleNamespace
class CodexClient:
    def __init__(self, **unused):
        self.responses = SimpleNamespace(create=self.create)
    def create(self, **request):
        if FAIL: raise RuntimeError("CODEX_QUOTA_EXHAUSTED: no API fallback")
        p = json.loads(request["input"])
        opinions = [dict(symbol=r["symbol"], verdict="WATCH", evidence_id=("wrong" if WRONG else r["evidence_id"]), thesis="research", counterevidence="unknown", missing_data=["live quotes"]) for r in p["candidates"]]
        usage = SimpleNamespace(model_dump=lambda: dict(input_tokens=100, output_tokens=20))
        return SimpleNamespace(output_text=json.dumps(dict(packet_id=p["packet_id"], opinions=opinions)), usage=usage)
'''.replace("FAIL", repr(fail)).replace("WRONG", repr(wrong_evidence)), encoding="utf-8")
        return packet_path, transport

    def test_review_pilot_is_bounded_to_four_calls_and_keeps_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet, transport = self.review_fixture(tmp)
            result = review_factorial(packet, transport, Path(tmp) / "reviews", "fake", 1)
            self.assertEqual((result["attempted_calls"], result["valid_calls"]), (4, 4))
            self.assertEqual(result["total_input_tokens"], 400)
            self.assertTrue(result["future_outcomes_not_scored"])

    def test_quota_failure_stops_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet, transport = self.review_fixture(tmp, fail=True)
            result = review_factorial(packet, transport, Path(tmp) / "reviews", "fake", 1)
            self.assertEqual((result["attempted_calls"], result["valid_calls"]), (1, 0))

    def test_wrong_evidence_reference_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet, transport = self.review_fixture(tmp, wrong_evidence=True)
            result = review_factorial(packet, transport, Path(tmp) / "reviews", "fake", 1)
            self.assertEqual(result["valid_calls"], 0)


if __name__ == "__main__":
    unittest.main()
