from __future__ import annotations

import csv
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from execution.account_guard import BuyingPower, InventoryRow
from execution.execution_store import ExecutionStore
from execution.health_monitor import run_health_check
from execution.broker_runtime import BrokerRuntime
from execution.models import Side


class AtomicAndHealthTests(unittest.TestCase):
    def test_missing_live_database_is_not_silently_recreated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live_execution.sqlite3"
            with self.assertRaisesRegex(RuntimeError, "missing"):
                ExecutionStore(path, must_exist=True)
            self.assertFalse(path.exists())

    def test_confirmed_fill_and_final_status_commit_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionStore(Path(tmp) / "execution.sqlite3")
            self.assertTrue(store.claim("D1", "R1", "0050", "BUY"))
            store.mark_send_pending("D1", {"qty": 2})
            store.mark_submitted("D1", {"seq13": "1234567890123"})
            self.assertEqual(2, store.finish_with_fill(
                "D1", "FILLED", {"filled_quantity": 2}, "0050", Side.BUY, 2,
            ))
            self.assertEqual(2, store.get_ai_qty("0050"))
            with self.assertRaises(RuntimeError):
                store.finish_with_fill(
                    "D1", "FILLED", {"filled_quantity": 2}, "0050", Side.BUY, 2,
                )
            self.assertEqual(2, store.get_ai_qty("0050"))
            self.assertEqual(
                ["STARTED", "SEND_PENDING", "SUBMITTED", "FILLED"],
                [event["status"] for event in store.events_for("D1")],
            )

    def test_failed_event_insert_rolls_back_fill_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.sqlite3"
            store = ExecutionStore(path)
            store.claim("D1", "R1", "0050", "BUY")
            store.mark_send_pending("D1", {})
            store.mark_submitted("D1", {})
            with closing(sqlite3.connect(path)) as con:
                con.execute("""CREATE TRIGGER fail_final BEFORE INSERT ON execution_events
                               WHEN NEW.status='FILLED' BEGIN SELECT RAISE(ABORT,'fail'); END""")
                con.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                store.finish_with_fill(
                    "D1", "FILLED", {"filled_quantity": 1}, "0050", Side.BUY, 1,
                )
            self.assertIsNone(store.get_ai_qty("0050"))
            with closing(sqlite3.connect(path)) as con:
                self.assertEqual("SUBMITTED", con.execute(
                    "SELECT status FROM execution_attempts WHERE decision_id='D1'"
                ).fetchone()[0])

    def test_daily_cap_reserves_before_send_and_survives_next_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionStore(Path(tmp) / "execution.sqlite3")
            store.claim("D1", "R1", "0050", "BUY")
            store.mark_send_pending("D1", {"required_twd_with_buffer": "12000"},
                                    daily_limit_twd=Decimal("20000"))
            self.assertEqual(Decimal("12000"), store.daily_reserved_buy_twd())
            store.claim("D2", "R2", "0051", "BUY")
            with self.assertRaises(RuntimeError):
                store.mark_send_pending("D2", {"required_twd_with_buffer": "9000"},
                                        daily_limit_twd=Decimal("20000"))
            with closing(sqlite3.connect(store.path)) as con:
                self.assertEqual("STARTED", con.execute(
                    "SELECT status FROM execution_attempts WHERE decision_id='D2'"
                ).fetchone()[0])

    def test_pending_order_blocks_same_symbol_but_excludes_current_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionStore(Path(tmp) / "execution.sqlite3")
            store.claim("D1", "R1", "0050", "BUY")
            store.mark_send_pending("D1", {"required_twd_with_buffer": "100"})
            store.mark_submitted("D1", {"required_twd_with_buffer": "100"})
            store.finish("D1", "ACKNOWLEDGED", {"required_twd_with_buffer": "100"})
            store.claim("D2", "R2", "0051", "SELL")
            self.assertEqual({"0050"}, store.pending_reconciliation_symbols(
                exclude_decision_id="D2"
            ))

    def test_hourly_report_flags_ai_exceeding_broker_without_editing_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "execution_live.json").write_text(
                json.dumps({"enabled": True, "min_available_to_buy_twd": 10000}),
                encoding="utf-8",
            )
            store = ExecutionStore(root / "data" / "execution" / "live_execution.sqlite3")
            store.bootstrap_ai_qty("0050", 3)
            row = InventoryRow("0050", "T", 2, 0, 0, 0, 0, 2, 2, "raw")
            power = BuyingPower(True, True, Decimal("9000"), Decimal("9000"),
                                Decimal("9000"), "raw")
            session = SimpleNamespace(ensure_ready=lambda: SimpleNamespace(action="REUSE"))
            with patch("execution.health_monitor.query_buying_power", return_value=power), patch(
                "execution.health_monitor.query_inventory", return_value=[row]
            ):
                report = run_health_check(root, session=session)
            self.assertEqual("ALERT", report["status"])
            self.assertTrue(report["balance_below_floor"])
            self.assertEqual("AI_EXCEEDS_BROKER",
                             report["inventory_comparison"][0]["status"])
            self.assertEqual(3, store.get_ai_qty("0050"))
            with (root / "data" / "health" / "health_checks.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                self.assertEqual("ALERT", list(csv.DictReader(handle))[0]["status"])

    def test_broker_runtime_reuses_one_session_for_health_and_two_executions(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_com = SimpleNamespace(CoInitialize=Mock(), CoUninitialize=Mock())
            fake_session = SimpleNamespace(connect=Mock())
            with patch.dict(sys.modules, {"comtypes": fake_com}), patch(
                "execution.broker_runtime.build_live_readonly_session_from_project",
                return_value=fake_session,
            ) as build, patch(
                "execution.pipeline_hook.execute_after_advice_written",
                return_value={"status": "COMPLETE"},
            ) as execute, patch(
                "execution.health_monitor.run_health_check",
                return_value={"status": "OK"},
            ) as health:
                runtime = BrokerRuntime(tmp)
                self.assertEqual({"status": "OK"}, runtime.health())
                runtime.execute(Path(tmp) / "one.json")
                runtime.execute(Path(tmp) / "two.json")
            fake_com.CoInitialize.assert_called_once()
            build.assert_called_once()
            fake_session.connect.assert_called_once()
            self.assertEqual(2, execute.call_count)
            health.assert_called_once()
            self.assertIs(fake_session, execute.call_args.kwargs["session"])


if __name__ == "__main__":
    unittest.main()
