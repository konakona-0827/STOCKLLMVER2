from __future__ import annotations

import unittest

from execution.capital_reply_session import CapitalReplySession


class FakeReply:
    def __init__(self, state):
        self.state = state
    def SKReplyLib_IsConnectedByID(self, user):
        return self.state


class SessionHealthTests(unittest.TestCase):
    def make_session(self, state):
        s = object.__new__(CapitalReplySession)
        s.user = "USER"
        s._connected = True
        s.account = "ACC"
        s._skR = FakeReply(state)
        s._comtypes = object()
        class Snap:
            replay_complete = True
        s.snapshot = Snap()
        return s

    def test_state_1_is_healthy(self):
        s = self.make_session(1)
        h = s.health_check()
        self.assertTrue(h.ready)
        self.assertEqual("REUSE", h.action)

    def test_state_0_requires_reply_reconnect(self):
        s = self.make_session(0)
        h = s.health_check()
        self.assertFalse(h.ready)
        self.assertEqual("REPLY_RECONNECT_REQUIRED", h.action)

    def test_local_disconnected_requires_login(self):
        s = self.make_session(1)
        s._connected = False
        h = s.health_check()
        self.assertFalse(h.ready)
        self.assertEqual("LOGIN_REQUIRED", h.action)


if __name__ == "__main__":
    unittest.main()
