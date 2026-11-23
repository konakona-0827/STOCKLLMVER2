"""Exercise securities Reply recovery without login loops or real broker calls."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock,patch

from execution.capital_reply_session import CapitalReplySession,CapitalReplySessionError
from execution.broker_runtime import BrokerRuntime
from opportunity_flow.dashboard_service import broker_read_message


class AgreementFallbackTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.clock=1000.0
        self.session=CapitalReplySession(project_root=tmp.name,user='TEST',password='TEST',
            account='TEST_ACCOUNT',dll_path=Path(tmp.name)/'mock.dll')
        s=self.session;s.account='TEST_ACCOUNT';s._center_login_attempted=True
        s._skC=SimpleNamespace(SKCenterLib_RequestAgreement=Mock(return_value=0),SKCenterLib_Login=Mock())
        s._skR=SimpleNamespace(SKReplyLib_IsConnectedByID=Mock(return_value=0))
        s._comtypes=SimpleNamespace(PumpEvents=Mock(side_effect=self.pump))
        self.codes=[2018,0];self.complete=True
        s._skR.SKReplyLib_ConnectByID=Mock(side_effect=self.connect)
        for name in ['monotonic','time']:
            patcher=patch('execution.capital_reply_session.time.'+name,side_effect=lambda:self.clock)
            patcher.start();self.addCleanup(patcher.stop)
        patcher=patch('execution.capital_reply_session.record_capital_event')
        self.audit=patcher.start();self.addCleanup(patcher.stop)

    def pump(self,seconds):
        self.clock+=seconds

    def connect(self,user):
        rc=self.codes.pop(0)
        if rc==0:
            self.session._skR.SKReplyLib_IsConnectedByID.return_value=1
            if self.complete:self.session.snapshot.replay_complete=True
        return rc

    def test_transient_lookup_is_refreshed_then_requires_successful_reply(self):
        self.session._complete_reply_startup(0,0)
        self.assertTrue(self.session.health_check().ready)
        self.session._skC.SKCenterLib_RequestAgreement.assert_called_once_with('TEST')
        self.assertEqual(self.session._skR.SKReplyLib_ConnectByID.call_count,2)
        self.session._skC.SKCenterLib_Login.assert_not_called()
        self.assertFalse(self.session.reply_startup_pending)
        codes=[call.kwargs['return_code'] for call in self.audit.call_args_list
               if call.kwargs.get('api_name')=='SKReplyLib_ConnectByID']
        self.assertEqual(codes,[2018,0])

    def test_persistent_failure_cools_down_then_recovers_same_owner(self):
        self.codes=[2018,2018,0]
        with self.assertRaisesRegex(CapitalReplySessionError,'BROKER_API_AGREEMENT_UNVERIFIED'):
            self.session._complete_reply_startup(0,0)
        self.assertFalse(self.session.connected)
        self.assertTrue(self.session.reply_startup_pending)
        with self.assertRaisesRegex(CapitalReplySessionError,'rc=2018'):
            self.session.ensure_ready()
        self.assertEqual(self.session._skR.SKReplyLib_ConnectByID.call_count,2)
        self.clock+=61
        self.assertTrue(self.session.ensure_ready().ready)
        self.assertEqual(self.session._skR.SKReplyLib_ConnectByID.call_count,3)
        self.session._skC.SKCenterLib_Login.assert_not_called()

    def test_failed_refresh_is_not_permission_or_another_connection(self):
        self.codes=[2018]
        self.session._skC.SKCenterLib_RequestAgreement.return_value=2019
        with self.assertRaisesRegex(CapitalReplySessionError,'agreement_refresh_rc=2019'):
            self.session._complete_reply_startup(0,0)
        self.assertFalse(self.session.connected)
        self.assertTrue(self.session.reply_startup_pending)
        self.session._skR.SKReplyLib_ConnectByID.assert_called_once()

    def test_unavailable_refresh_keeps_account_blocked(self):
        del self.session._skC.SKCenterLib_RequestAgreement
        with self.assertRaisesRegex(CapitalReplySessionError,'UNAVAILABLE'):
            self.session._complete_reply_startup(0,0)
        self.assertFalse(self.session.health_check().ready)
        self.session._skR.SKReplyLib_ConnectByID.assert_called_once()

    def test_refresh_exception_keeps_diagnostics_without_secret_exception_text(self):
        self.session._skC.SKCenterLib_RequestAgreement.side_effect=RuntimeError('PRIVATE_ACCOUNT_PAYLOAD')
        with self.assertRaises(CapitalReplySessionError) as raised:
            self.session._complete_reply_startup(0,0)
        self.assertIn('EXCEPTION:RuntimeError',str(raised.exception))
        self.assertNotIn('PRIVATE_ACCOUNT_PAYLOAD',str(raised.exception))
        self.assertTrue(self.session.reply_startup_pending)

    def test_established_reply_reconnect_uses_same_fallback_and_fresh_replay(self):
        s=self.session;s._connected=True;s.snapshot.tc_rows=['OLD_FILL']
        s._reconnect_reply_only(replay_wait_seconds=0,replay_grace_seconds=0)
        self.assertTrue(s.health_check().ready)
        self.assertEqual(s.snapshot.tc_rows,[])
        s._skC.SKCenterLib_RequestAgreement.assert_called_once()
        s._skC.SKCenterLib_Login.assert_not_called()

    def test_other_errors_do_not_enter_agreement_or_futures_fallback(self):
        self.codes=[2019]
        with self.assertRaisesRegex(CapitalReplySessionError,'rc=2019'):
            self.session._complete_reply_startup(0,0)
        self.session._skC.SKCenterLib_RequestAgreement.assert_not_called()
        self.assertFalse(self.session.reply_startup_pending)
        self.assertFalse(self.session.connected)

    def test_stale_complete_before_retry_cannot_authorize_new_connection(self):
        self.complete=False
        def old_event(seconds):
            self.clock+=seconds
            self.session.snapshot.replay_complete=True
        self.session._comtypes.PumpEvents.side_effect=old_event
        with self.assertRaisesRegex(CapitalReplySessionError,'No OnComplete'):
            self.session._complete_reply_startup(0,0)
        self.assertFalse(self.session.connected)

    def test_pending_session_services_callbacks_but_reconnect_can_be_disabled(self):
        self.session._reply_startup_pending=True
        self.session.pump(.01)
        self.session._comtypes.PumpEvents.assert_called()
        with self.assertRaisesRegex(CapitalReplySessionError,'reconnect disabled'):
            self.session.ensure_ready(allow_reply_reconnect=False)
        self.session._skR.SKReplyLib_ConnectByID.assert_not_called()

    def test_ui_explains_ambiguous_securities_status(self):
        message=broker_read_message(dict(errors=['SKReplyLib_ConnectByID failed: rc=2018']))
        self.assertIn('證券 API',message)
        self.assertIn('網路',message)
        self.assertIn('不啟用期貨',message)

    def test_broker_preserves_pending_owner_and_blocks_actions_until_health_recovers(self):
        import sys
        s=self.session
        # The initial login succeeds, but its Reply setup cannot complete yet.
        def initial_connect():
            s._reply_startup_pending=True
            raise CapitalReplySessionError('SKReplyLib_ConnectByID failed: rc=2018')
        s.connect=Mock(side_effect=initial_connect)
        observed=[]
        def health(root,session=None,connection_error=None):
            observed.append(session)
            if len(observed)==2:session._reply_startup_pending=False;session._connected=True
            return dict(status='ERROR' if len(observed)==1 else 'OK')
        fake_com=SimpleNamespace(CoInitialize=Mock(),CoUninitialize=Mock())
        with patch.dict(sys.modules,{'comtypes':fake_com}),patch(
                'execution.broker_runtime.build_live_readonly_session_from_project',return_value=s) as build,patch(
                'execution.health_monitor.run_health_check',side_effect=health),patch(
                'execution.pipeline_hook.execute_after_advice_written') as execute:
            runtime=BrokerRuntime(self.session.project_root)
            try:
                self.assertEqual(runtime.health()['status'],'ERROR')
                with self.assertRaisesRegex(RuntimeError,'health reconciliation required'):
                    runtime.execute('unused.json')
                execute.assert_not_called()
                self.assertEqual(runtime.health()['status'],'OK')
                self.assertEqual(observed,[s,s])
                build.assert_called_once();s.connect.assert_called_once()
            finally:runtime.shutdown()


if __name__=='__main__':unittest.main()
