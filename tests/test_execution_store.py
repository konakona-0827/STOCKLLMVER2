from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from execution.execution_store import ExecutionStore
from execution.models import Side


class ExecutionStoreTests(unittest.TestCase):
    def test_claim_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            s = ExecutionStore(Path(td) / "x.sqlite3")
            self.assertTrue(s.claim("D1", "R1", "0050", "BUY"))
            self.assertFalse(s.claim("D1", "R1", "0050", "BUY"))

    def test_position_fill_updates(self):
        with tempfile.TemporaryDirectory() as td:
            s = ExecutionStore(Path(td) / "x.sqlite3")
            self.assertEqual(2, s.bootstrap_ai_qty("2891", 2))
            self.assertEqual(5, s.apply_fill("2891", Side.BUY, 3))
            self.assertEqual(4, s.apply_fill("2891", Side.SELL, 1))
            self.assertEqual(4, s.get_ai_qty("2891"))


if __name__ == "__main__":
    unittest.main()
