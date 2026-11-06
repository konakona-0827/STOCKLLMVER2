from pathlib import Path
from types import SimpleNamespace
from threading import Event,get_ident
from unittest.mock import Mock,patch
import sys
import tempfile
import unittest

from execution.capital_reply_session import CapitalReplySession,CapitalReplySessionError,ReplySnapshot
from execution.broker_runtime import BrokerRuntime


class ReplyRecoveryTests(unittest.TestCase):
    def session(self,root,state=0):
        s=object.__new__(CapitalReplySession)
        s.project_root=Path(root);s.session_id='test';s.user='USER';s.account='ACC';s._connected=True
        s._reply_needs_reset=False;s.snapshot=ReplySnapshot(replay_complete=False,tc_rows=['old'],accounts=['ACC'])
        reply=SimpleNamespace(state=state)
        reply.SKReplyLib_IsConnectedByID=Mock(side_effect=lambda user:reply.state)
        reply.SKReplyLib_ConnectByID=Mock(return_value=0)
        reply.SKReplyLib_SolaceCloseByID=Mock(side_effect=lambda user:setattr(reply,'state',0) or 0)
        s._skR=reply;s._comtypes=SimpleNamespace(PumpEvents=Mock())
        return s

    def test_connected_socket_without_completed_replay_is_not_trade_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=self.session(tmp,1)
            self.assertFalse(s.health_check().ready)
            self.assertEqual(s.health_check().action,'REPLAY_PENDING')

    def test_downloading_reply_is_pumped_without_restarting_the_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=self.session(tmp,2)
            def complete(seconds):s.snapshot.replay_complete=True;s._skR.state=1
            s._comtypes.PumpEvents.side_effect=complete
            self.assertTrue(s.ensure_ready().ready)
            s._skR.SKReplyLib_ConnectByID.assert_not_called()
            s._skR.SKReplyLib_SolaceCloseByID.assert_not_called()

    def test_timed_out_reply_requires_clean_reset_then_fresh_complete_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=self.session(tmp)
            with self.assertRaisesRegex(CapitalReplySessionError,'clean reset'):
                s._reconnect_reply_only(replay_wait_seconds=0,replay_grace_seconds=0)
            self.assertTrue(s._reply_needs_reset)
            def complete(seconds):s.snapshot.replay_complete=True;s._skR.state=1
            s._comtypes.PumpEvents.side_effect=complete
            s._reconnect_reply_only(replay_wait_seconds=.1,replay_grace_seconds=0)
            s._skR.SKReplyLib_SolaceCloseByID.assert_called_once_with('USER')
            self.assertTrue(s.health_check().ready);self.assertFalse(s._reply_needs_reset)
            self.assertEqual(s.snapshot.tc_rows,[]);self.assertEqual(s.snapshot.accounts,['ACC'])

    def test_failed_clean_reset_does_not_issue_another_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=self.session(tmp);s._reply_needs_reset=True
            s._skR.SKReplyLib_SolaceCloseByID.side_effect=None
            s._skR.SKReplyLib_SolaceCloseByID.return_value=999
            with self.assertRaisesRegex(CapitalReplySessionError,'reset failed'):s._reconnect_reply_only()
            s._skR.SKReplyLib_ConnectByID.assert_not_called()

    def test_idle_broker_thread_services_com_callbacks_without_relogin(self):
        event=Event();thread_ids=[]
        def pump(seconds):thread_ids.append(get_ident());event.set()
        session=SimpleNamespace(connect=Mock(),pump=Mock(side_effect=pump))
        fake_com=SimpleNamespace(CoInitialize=Mock(),CoUninitialize=Mock())
        with tempfile.TemporaryDirectory() as tmp,patch.dict(sys.modules,{'comtypes':fake_com}),patch(
            'execution.broker_runtime.build_live_readonly_session_from_project',return_value=session),patch(
            'execution.health_monitor.run_health_check',return_value=dict(status='OK')):
            runtime=BrokerRuntime(tmp)
            try:
                runtime.health()
                self.assertTrue(event.wait(2),'idle STA did not process COM events')
                self.assertNotEqual(thread_ids[0],get_ident())
                session.connect.assert_called_once()
            finally:runtime.shutdown()


if __name__=='__main__':unittest.main()
