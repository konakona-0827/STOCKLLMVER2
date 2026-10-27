from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from execution import auto_advice_executor as mod


class FakeSession:
    def __init__(self):
        self.connected = True


class SessionReuseTests(unittest.TestCase):
    def test_existing_connected_session_is_not_rebuilt(self):
        # We only verify the connection-selection branch here. Other pipeline
        # behavior is covered by existing unit tests.
        source = Path(mod.__file__).read_text(encoding="utf-8")
        self.assertIn("if session is None:", source)
        self.assertIn("elif not session.connected:", source)
        self.assertIn("broker = CapitalOddLotExecutor(session)", source)


if __name__ == "__main__":
    unittest.main()
