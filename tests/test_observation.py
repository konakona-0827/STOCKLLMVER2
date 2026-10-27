from __future__ import annotations

from copy import deepcopy
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from support_scout.contracts import DataError, Rules, digest
from support_scout.demo import demo_bundle
from support_scout.observation import observe
from support_scout.pipeline import run
from support_scout.providers import Completion
from support_scout.reporting import render_html
from support_scout.settings import doctor, load_settings, openai_config
from support_scout.storage import atomic_json, publish_latest, publish_run


def observation_fixture():
    value = demo_bundle()
    value.update(synthetic=False, observation_only=True)
    for item in value["securities"]:
        item.update(observation_only=True, price_basis="raw_unverified", actions_reviewed=False,
            tradability_reviewed=False, volume_comparable=False, point_in_time=False)
    value["collection"] = {"status": "COMPLETE", "expected_latest_session": "2026-09-08",
                           "actual_latest_session": "2026-09-08", "sources": []}
    return value


class Approver:
    model = "test-model"
    mode = "test_transport"
    def complete(self, packet, role):
        return Completion({"verdict": "RESEARCH", "evidence_ids": packet["core"]["required_confirmation_evidence"],
            "summary": "測試反例：嘗試通過未核實原始價。", "risks": [], "missing_data": []})


class ObservationGateTests(unittest.TestCase):
    def test_raw_data_is_explicitly_observation_only_and_cannot_be_promoted(self):
        bundle = observation_fixture()
        initial = run(bundle)
        self.assertEqual(initial["securities"][0]["data_status"], "OBSERVATION")
        self.assertIn("9001", initial["technical_queue"])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result, target, code = observe(bundle=bundle, researcher=Approver(), reviewer=Approver(), agents=True,
                max_packets=1, output_root=root / "out", db_path=root / "data.sqlite3", settings_path=root / "missing.json")
            self.assertEqual(result["securities"][0]["verdict"], "WATCH")
            self.assertEqual(result["reviewed_shortlist"], [])
            self.assertIn("OBSERVATION_ONLY", result["securities"][0]["reasons"])
            self.assertTrue((target / "committed.json").is_file())
            self.assertIn("PARTIAL_BUDGET", result["observation"]["agent_status"])
            self.assertEqual(code, 0)

    def test_raw_data_cannot_enter_normal_research_by_removing_bundle_flag(self):
        bundle = observation_fixture()
        bundle.pop("observation_only")
        self.assertTrue(all(i["data_status"] == "REJECTED" for i in run(bundle)["securities"]))

    def test_no_agents_is_not_claimed_as_model_success_and_replay_is_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            rules = replace(Rules(), min_drawdown=0.09)
            result, target, code = observe(bundle=observation_fixture(), agents=False, rules=rules,
                output_root=root / "out", db_path=root / "data.sqlite3", settings_path=root / "missing.json")
            self.assertEqual(result["observation"]["agent_status"], "NOT_REQUESTED")
            manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["rules_hash"], digest(result["rules"]))
            saved = json.loads((target / "input.json").read_text(encoding="utf-8"))
            replay = run(saved, rules=Rules(**manifest["rules"]), as_of=manifest["effective_as_of"])
            self.assertEqual(replay["snapshot_id"], result["snapshot_id"])
            self.assertEqual(manifest["input_digest"], digest(saved))
            self.assertEqual(code, 0)

    def test_missing_model_config_saves_rule_observation_but_exit_is_degraded(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            root = Path(folder)
            result, target, code = observe(bundle=observation_fixture(), agents=True,
                output_root=root / "out", db_path=root / "data.sqlite3", settings_path=root / "missing.json")
            self.assertEqual(code, 3)
            self.assertEqual(result["observation"]["agent_status"], "CONFIGURATION_REQUIRED")
            self.assertTrue((target / "report.html").is_file())

    def test_publication_failure_rolls_back_ledger_and_does_not_overwrite_latest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            out = root / "out"
            atomic_json(out / "latest.json", {"as_of": "2026-09-08T16:00:00+08:00", "report": "previous.html"})
            with patch("support_scout.observation.publish_run", side_effect=OSError("private-error")):
                with self.assertRaisesRegex(DataError, "OBSERVATION_FAILED:OSError"):
                    observe(bundle=observation_fixture(), agents=False, output_root=out,
                        db_path=root / "data.sqlite3", settings_path=root / "missing.json")
            with closing(sqlite3.connect(root / "data.sqlite3")) as db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertFalse(tables)  # Even initial DDL was part of the rolled-back transaction.
            self.assertEqual(json.loads((out / "latest.json").read_text())["report"], "previous.html")
            attempt = json.loads((out / "latest_attempt.json").read_text())
            self.assertEqual(attempt["status"], "FAILED")
            self.assertNotIn("private-error", (out / "latest_attempt.json").read_text())

    def test_html_escapes_rejected_symbols_names_and_model_text(self):
        bundle = observation_fixture()
        bundle["securities"][0]["symbol"] = '\"><script>alert(1)</script>'
        bundle["securities"][1]["name"] = "<img src=x onerror=alert(2)>"
        report = run(bundle)
        html = render_html(report, bundle)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("OBSERVATION ONLY", html)


class SettingsTests(unittest.TestCase):
    def test_authorized_env_reads_only_openai_and_supports_existing_multiline_key(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            path = Path(folder) / "authorized.env"
            path.write_text('OPENAI_API_KEY=\nsk-test-secret\nOPENAI_MODEL="test-model"\nCAPITAL_PASSWORD=not-authorized\n', encoding="utf-8")
            result = openai_config({"openai_env_file": str(path)})
            self.assertTrue(result["ready"])
            self.assertEqual(result["model"], "test-model")
            self.assertNotIn("CAPITAL_PASSWORD", os.environ)
            self.assertNotIn("sk-test-secret", json.dumps(result))

    def test_environment_has_priority_and_local_settings_does_not_accept_secret(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"OPENAI_API_KEY": "env-secret"}, clear=True):
            path = Path(folder) / "settings.json"
            path.write_text('{"api_key":"no-plain-secret-in-config"}')
            with self.assertRaises(DataError):
                load_settings(path)
            result = openai_config({"model": "test-model"})
            self.assertTrue(result["ready"])
            self.assertNotIn("env-secret", json.dumps(result))

    def test_doctor_handles_sqlite_failures_and_never_claims_model_ready(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            with patch("sqlite3.connect", side_effect=sqlite3.OperationalError("disk failure")):
                result = doctor(Path(folder) / "missing.json")
            self.assertFalse(result["observation_ready"])
            self.assertFalse(result["openai_ready"])
            self.assertIn("LOCAL_STORAGE_UNWRITABLE", result["errors"])


class AtomicPublicationTests(unittest.TestCase):
    def test_late_finishing_old_run_does_not_replace_latest_pointer(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "latest.json"
            self.assertTrue(publish_latest(path, {"as_of": "2026-10-28T16:00:00+08:00", "report": "new.html"}))
            self.assertFalse(publish_latest(path, {"as_of": "2026-10-27T16:00:00+08:00", "report": "old.html"}))
            self.assertEqual(json.loads(path.read_text())["report"], "new.html")

    def test_run_publication_refuses_overwrite_without_touching_existing_files(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "run"
            publish_run(target, {"report.md": "first"})
            with self.assertRaises(DataError):
                publish_run(target, {"report.md": "second"})
            self.assertEqual((target / "report.md").read_text(), "first")


if __name__ == "__main__":
    unittest.main()
