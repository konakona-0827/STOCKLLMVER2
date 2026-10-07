from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from execution.execution_store import ExecutionStore


class ExecutionStoreStep10Tests(unittest.TestCase):
    def test_event_timeline(self):
        with tempfile.TemporaryDirectory() as td:
            store = ExecutionStore(Path(td) / "x.sqlite3")
            self.assertTrue(store.claim("D1", "R1", "0050", "BUY"))
            store.append_event("D1", "SUBMITTED", {"seq13": "1234567890123"})
            store.finish("D1", "ACKNOWLEDGED", {"ok": True})

            events = store.events_for("D1")
            self.assertEqual(
                ["STARTED", "SUBMITTED", "ACKNOWLEDGED"],
                [x["status"] for x in events],
            )


if __name__ == "__main__":
    unittest.main()
