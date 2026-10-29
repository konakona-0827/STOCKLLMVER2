"""A silent Dashboard exit restarts; a user window close stops supervision."""

from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from operations import dashboard_supervisor as supervisor


class DashboardSupervisorTests(unittest.TestCase):
    def test_restart_after_silent_exit_then_stop_on_normal_close(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "data" / "health" / "dashboard_lifecycle.jsonl"
            path.parent.mkdir(parents=True)
            stamp = (supervisor.utc_now() + timedelta(seconds=1)).isoformat()
            path.write_text("\n".join(json.dumps(row) for row in (
                {"time_utc": stamp, "pid": 22, "run_id": "second", "event": "START"},
                {"time_utc": stamp, "pid": 22, "run_id": "second", "event": "EXIT",
                 "detail": {"reason": "NORMAL_CLOSE"}},
            )) + "\n", encoding="utf-8")
            children = [Mock(pid=11, wait=Mock(return_value=1)),
                        Mock(pid=22, wait=Mock(return_value=0))]
            with patch.object(supervisor.subprocess, "Popen", side_effect=children) as launch, \
                    patch.object(supervisor.time, "sleep"):
                self.assertEqual(0, supervisor.supervise(
                    root, mode="agent", fallback_strategy=None, max_restarts=3))
            self.assertEqual(2, launch.call_count)
            rows = [json.loads(line) for line in (root / "data" / "health" /
                    "dashboard_supervisor.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(["SUPERVISOR_START", "DASHBOARD_STARTED", "UNEXPECTED_EXIT",
                              "DASHBOARD_STARTED", "NORMAL_CLOSE"],
                             [row["event"] for row in rows])

    def test_repeated_unexpected_exits_stop_at_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            children = [Mock(pid=11, wait=Mock(return_value=1)),
                        Mock(pid=22, wait=Mock(return_value=1))]
            with patch.object(supervisor.subprocess, "Popen", side_effect=children) as launch, \
                    patch.object(supervisor.time, "sleep"):
                self.assertEqual(1, supervisor.supervise(
                    Path(temporary), mode="agent", fallback_strategy=None,
                    max_restarts=1))
            self.assertEqual(2, launch.call_count)
            rows = [json.loads(line) for line in (Path(temporary) / "data" / "health" /
                    "dashboard_supervisor.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual("RESTART_LIMIT_REACHED", rows[-1]["event"])


if __name__ == "__main__":
    unittest.main()
