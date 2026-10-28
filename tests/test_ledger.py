from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import unittest

from support_scout.contracts import DataError, digest
from support_scout.ledger import record_observation


def report(day="2026-10-27", *, snapshot="snap-1", state="RETEST_HELD", close=104,
           anchor_at="2026-10-15", lower=99, upper=101, anchor=100, basis="raw_unverified"):
    return {"schema_version": 1, "synthetic": False, "as_of": day + "T17:00:00+08:00",
        "rules": {"version": "test-v1", "zone_atr": .35}, "snapshot_id": snapshot,
        "universe_version": "test-universe-v1", "securities": [{"symbol": "2330", "name": "測試",
            "price_basis": basis, "data_status": "OBSERVATION", "technical": {
                "state": state, "latest_session": day, "latest_close": close,
                "support": {"anchor_at": anchor_at, "anchor": anchor, "lower": lower, "upper": upper}}}]}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "local_data" / "observations.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def query(self, sql):
        conn = sqlite3.connect(self.db)
        try:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql)]
        finally:
            conn.close()

    def test_first_observation_and_next_day_keep_one_episode(self):
        first = record_observation(report(), self.db)
        second = record_observation(report("2026-10-28", snapshot="snap-2", state="RULE_CONFIRMED", close=106), self.db)
        self.assertEqual(first["new"], 1)
        self.assertEqual(second["changed"], 1)
        self.assertEqual(first["rows"][0]["episode_id"], second["rows"][0]["episode_id"])
        self.assertEqual(second["rows"][0]["previous_state"], "RETEST_HELD")
        self.assertEqual(second["rows"][0]["current_state"], "RULE_CONFIRMED")
        self.assertEqual(len(self.query("SELECT * FROM episodes")), 1)
        self.assertEqual(len(self.query("SELECT * FROM observations")), 2)

    def test_same_session_snapshot_deduplicates_and_preserves_run_audit(self):
        first = record_observation(report(), self.db)
        duplicate = record_observation(report(), self.db)
        self.assertEqual(duplicate["duplicate"], 1)
        self.assertNotEqual(first["run_id"], duplicate["run_id"])
        self.assertEqual(len(self.query("SELECT * FROM observations")), 1)
        self.assertEqual(len(self.query("SELECT * FROM runs")), 2)

    def test_old_report_cannot_override_newer_current_view(self):
        record_observation(report("2026-10-28", snapshot="new", state="RULE_CONFIRMED", close=106), self.db)
        stale = record_observation(report("2026-10-27", snapshot="old", close=95), self.db)
        self.assertEqual(stale["stale"], 1)
        self.assertEqual(self.query("SELECT * FROM episodes")[0]["current_state"], "RULE_CONFIRMED")
        current = self.query("SELECT * FROM current_view")[0]
        self.assertEqual(current["last_session"], "2026-10-28")
        self.assertEqual(current["last_close"], 106)

    def test_duplicate_replay_reports_newer_invalidation_instead_of_old_approval(self):
        original = report(state="RULE_CONFIRMED", close=106)
        record_observation(original, self.db)
        record_observation(report("2026-10-28", snapshot="broken", close=98), self.db)
        replay = record_observation(original, self.db)
        self.assertEqual(replay["stale"], 1)
        self.assertEqual(replay["rows"][0]["current_state"], "INVALIDATED")
        self.assertEqual(len(self.query("SELECT * FROM observations")), 2)

    def test_duplicate_replay_reports_persistent_revision_review(self):
        original = report(state="RULE_CONFIRMED", close=106)
        record_observation(original, self.db)
        record_observation(report("2026-10-28", snapshot="revision", lower=98, upper=102), self.db)
        replay = record_observation(original, self.db)
        self.assertEqual(replay["stale"], 1)
        self.assertEqual(replay["rows"][0]["current_state"], "REVISION_REVIEW")
        self.assertTrue(replay["rows"][0]["revision_review"])

    def test_revised_support_is_reviewed_and_never_redrawn(self):
        record_observation(report(), self.db)
        revised = record_observation(report("2026-10-28", snapshot="revision", lower=98, upper=102, state="RULE_CONFIRMED"), self.db)
        self.assertEqual(revised["revision"], 1)
        self.assertEqual(revised["rows"][0]["current_state"], "REVISION_REVIEW")
        episode = self.query("SELECT * FROM episodes")[0]
        self.assertEqual((episode["frozen_lower"], episode["frozen_upper"]), (99, 101))
        restored = record_observation(report("2026-10-29", snapshot="back-to-old", state="RULE_CONFIRMED"), self.db)
        self.assertEqual(restored["rows"][0]["current_state"], "REVISION_REVIEW")

    def test_revised_anchor_price_on_same_date_is_same_episode(self):
        first = record_observation(report(), self.db)
        revised = record_observation(report("2026-10-28", snapshot="revision", anchor=100.5), self.db)
        self.assertEqual(first["rows"][0]["episode_id"], revised["rows"][0]["episode_id"])
        self.assertEqual(revised["revision"], 1)
        self.assertEqual(self.query("SELECT * FROM episodes")[0]["frozen_anchor"], 100)

    def test_invalidated_episode_cannot_resurrect(self):
        record_observation(report(), self.db)
        broken = record_observation(report("2026-10-28", snapshot="broken", close=98, state="RULE_CONFIRMED"), self.db)
        rebound = record_observation(report("2026-10-29", snapshot="rebound", close=106, state="RULE_CONFIRMED"), self.db)
        self.assertEqual(broken["invalidated"], 1)
        self.assertEqual(rebound["rows"][0]["current_state"], "INVALIDATED")
        self.assertEqual(self.query("SELECT * FROM episodes")[0]["invalidated_session"], "2026-10-28")

    def test_new_anchor_is_new_episode_but_old_zone_is_still_checked(self):
        first = record_observation(report(), self.db)
        new = record_observation(report("2026-10-28", snapshot="new-anchor", anchor_at="2026-10-22", anchor=90,
            lower=89, upper=91, close=95), self.db)
        self.assertEqual(new["new"], 1)
        self.assertEqual(new["invalidated"], 1)
        self.assertEqual(len(self.query("SELECT * FROM episodes")), 2)
        previous = self.query("SELECT * FROM episodes WHERE anchor_at='2026-10-15'")[0]
        self.assertEqual(previous["current_state"], "INVALIDATED")
        self.assertNotEqual(first["rows"][0]["episode_id"], new["rows"][-1]["episode_id"])

    def test_no_current_setup_still_checks_old_fixed_lower(self):
        record_observation(report(), self.db)
        no_setup = report("2026-10-28", snapshot="absent", state="NO_SETUP", close=98)
        no_setup["securities"][0]["technical"]["support"] = None
        result = record_observation(no_setup, self.db)
        self.assertEqual(result["invalidated"], 1)
        self.assertEqual(self.query("SELECT * FROM episodes")[0]["current_state"], "INVALIDATED")

    def test_rejected_data_and_malformed_price_cannot_alter_previous(self):
        record_observation(report(), self.db)
        rejected = report("2026-10-28", snapshot="rejected", close=1)
        rejected["securities"][0] = {"symbol": "2330", "data_status": "REJECTED", "reasons": ["bad data"]}
        result = record_observation(rejected, self.db)
        self.assertEqual(result["rows"][0]["change"], "DATA_REJECTED")
        self.assertEqual(result["rows"][0]["previous_state"], "RETEST_HELD")
        invalid = report("2026-10-29", snapshot="nan", close=float("nan"))
        # Global report hashing rejects non-finite values before any DB write.
        with self.assertRaises(ValueError):
            record_observation(invalid, self.db)
        self.assertEqual(self.query("SELECT * FROM episodes")[0]["last_close"], 104)
        self.assertEqual(self.query("SELECT * FROM current_view")[0]["last_session"], "2026-10-27")

    def test_bad_security_isolated_with_savepoint(self):
        broken = report()
        other = deepcopy(broken["securities"][0])
        other["symbol"] = "2454"
        broken["securities"][0]["technical"]["support"]["lower"] = 1000
        broken["securities"].append(other)
        result = record_observation(broken, self.db)
        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["new"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(self.query("SELECT * FROM episodes")[0]["symbol"], "2454")

    def test_changed_rules_or_price_basis_are_separate_models(self):
        record_observation(report(), self.db)
        changed = report("2026-10-28", snapshot="new-rules")
        changed["rules"]["zone_atr"] = .40
        self.assertEqual(record_observation(changed, self.db)["new"], 1)
        self.assertEqual(record_observation(report("2026-10-28", snapshot="new-basis", basis="adjusted_ohlc"), self.db)["new"], 1)
        self.assertEqual(len(self.query("SELECT * FROM episodes")), 3)

    def test_same_snapshot_conflicting_content_is_rejected(self):
        record_observation(report(), self.db)
        result = record_observation(report(close=98), self.db)
        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["errors"][0]["error"], "LEDGER_SNAPSHOT_CONTENT_CONFLICT")
        self.assertEqual(self.query("SELECT * FROM episodes")[0]["last_close"], 104)

    def test_synthetic_or_missing_real_declaration_never_opens_database(self):
        for synthetic in (True, None, "false", 0):
            with self.subTest(synthetic=synthetic):
                value = report()
                value["synthetic"] = synthetic
                with self.assertRaisesRegex(DataError, "REAL_DATA_REQUIRED"):
                    record_observation(value, self.db)
        self.assertFalse(self.db.exists())

    def test_future_session_and_duplicate_symbols_do_not_write_state(self):
        future = report()
        future["securities"][0]["technical"]["latest_session"] = "2026-10-29"
        result = record_observation(future, self.db)
        self.assertEqual(result["errors"][0]["error"], "LEDGER_FUTURE_SESSION")
        duplicate = report(snapshot="dupe")
        duplicate["securities"].append(deepcopy(duplicate["securities"][0]))
        result = record_observation(duplicate, self.db)
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual(self.query("SELECT * FROM episodes"), [])

    def test_publish_failure_rolls_back_entire_first_run(self):
        calls = []
        def publish(summary):
            calls.append(summary["run_id"])
            self.assertEqual(summary["new"], 1)
            raise OSError("report publication failed")
        with self.assertRaisesRegex(OSError, "publication failed"):
            record_observation(report(), self.db, before_commit=publish)
        self.assertEqual(len(calls), 1)
        # Schema creation and the run's rows were inside the same transaction.
        tables = {row["name"] for row in self.query("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("runs", "observations", "episodes", "current_view"):
            self.assertTrue(table not in tables or self.query(f"SELECT * FROM {table}") == [])

    def test_publish_failure_preserves_previous_run_and_current_state(self):
        first = record_observation(report(), self.db)
        def publish(summary):
            self.assertEqual(summary["invalidated"], 1)
            # A separate reader must still see the last committed state.
            self.assertEqual(self.query("SELECT * FROM episodes")[0]["current_state"], "RETEST_HELD")
            raise OSError("disk unavailable")
        with self.assertRaisesRegex(OSError, "disk unavailable"):
            record_observation(report("2026-10-28", snapshot="not-published", close=98),
                               self.db, before_commit=publish)
        self.assertEqual(self.query("SELECT * FROM runs")[0]["run_id"], first["run_id"])
        self.assertEqual(len(self.query("SELECT * FROM runs")), 1)
        self.assertEqual(len(self.query("SELECT * FROM observations")), 1)
        episode = self.query("SELECT * FROM episodes")[0]
        self.assertEqual(episode["current_state"], "RETEST_HELD")
        self.assertEqual(episode["last_session"], "2026-10-27")

    def test_publish_callback_report_mutation_is_bound_to_final_audit_digest(self):
        value = report()
        original_digest = digest(value)
        published = []
        def publish(summary):
            value["ledger"] = summary
            value["status"] = "OBSERVATION_READY"
            published.append(deepcopy(value))
        summary = record_observation(value, self.db, before_commit=publish)
        run = self.query("SELECT * FROM runs")[0]
        self.assertEqual(run["run_id"], summary["run_id"])
        self.assertEqual(run["report_digest"], digest(published[0]))
        self.assertEqual(run["report_digest"], digest(value))
        self.assertNotEqual(run["report_digest"], original_digest)


if __name__ == "__main__":
    unittest.main()
