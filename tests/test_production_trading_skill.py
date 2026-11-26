"""Skill guidance, stale approval isolation and actual executor adapter checks."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import test_flow_execution_bridge as fixtures
from test_opportunity_flow import FakeRunner
from opportunity_flow.common import digest,policy
from opportunity_flow.review import CodexRunner,PROMPT_VERSION,ROLE_TASKS,RULES
from opportunity_flow.skill_profile import load_profile,validate_review_profile
from opportunity_flow.state import Ledger
from opportunity_flow.legacy_runtime import LegacyRuntime


class ProductionSkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.profile=load_profile();self.assertTrue(self.profile.enabled)

    def test_host_schema_prices_and_request_unchanged_except_instructions(self):
        request=dict(instructions='host rules',input='{"price":100,"quantity":2}',text=dict(format=dict(name='pretrade_review')))
        before=deepcopy(request)
        for stage in ('pretrade','position_exit'):
            applied=self.profile.request(request,stage)
            self.assertEqual(applied['input'],request['input']);self.assertEqual(applied['text'],request['text'])
            self.assertIn(self.profile.body,applied['instructions']);self.assertIn(stage,applied['instructions'])
        self.assertEqual(before,request)

    def test_off_control_preserves_legacy_cache_key_and_never_reuses_skill_approval(self):
        ledger=Ledger(self.root/'state');self.addCleanup(ledger.close);candidate=dict(material_key='case')
        active=ledger.key(candidate,'model')
        with patch.dict(os.environ,FLOW_TRADING_SKILL='off'):
            off=load_profile();self.assertFalse(off.enabled)
            original=digest(['case','model',PROMPT_VERSION,ROLE_TASKS,RULES])
            self.assertEqual(ledger.key(candidate,'model'),original)
            with self.assertRaisesRegex(ValueError,'SKILL_MISMATCH'):validate_review_profile(dict(trading_skill=self.profile.metadata))
        self.assertNotEqual(active,original)
        with self.assertRaisesRegex(ValueError,'SKILL_MISMATCH'):validate_review_profile({})

    def test_skill_tamper_fails_before_model_or_order(self):
        fixture=self.root/'code';(fixture/'config').mkdir(parents=True)
        source=fixture/'skills/tw-production-decision/SKILL.md';source.parent.mkdir(parents=True)
        source.write_text('tampered',encoding='utf-8')
        original=Path(__file__).resolve().parents[1]/'config/trading_skill.json'
        (fixture/'config/trading_skill.json').write_bytes(original.read_bytes())
        with patch('opportunity_flow.skill_profile.CODE_ROOT',fixture):
            with self.assertRaisesRegex(ValueError,'HASH_MISMATCH'):load_profile()

    def test_exact_skill_snapshot_persisted_in_research_ledger(self):
        ledger=Ledger(self.root/'state');self.addCleanup(ledger.close)
        data=ledger.db.execute('SELECT metadata,source_text FROM trading_skill_profiles').fetchone()
        self.assertEqual(json.loads(data[0]),self.profile.metadata)
        self.assertEqual(hashlib.sha256(data[1].encode()).hexdigest(),self.profile.sha256)

    def fixture_bridge(self):
        fixture=fixtures.BridgeTests('test_approved_research_reaches_existing_interface_without_chasing')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        return fixture

    def test_researcher_and_challenger_cli_both_receive_skill_and_exact_packets(self):
        fixture=self.fixture_bridge();packet=fixture.c['review']['packet'];fake=FakeRunner()
        for role in ('researcher','challenger'):
            runner=CodexRunner.__new__(CodexRunner)
            runner.executable='not-a-real-tool.exe';runner.model=policy()['model'];runner.timeout=180;runner.auth_checked=True;runner.stop_event=None
            captures=[]
            async def capture(args,cwd,stdin=None,timeout=30):
                captures.append(stdin.decode())
                path=Path(args[args.index('--output-last-message')+1])
                path.write_text(json.dumps(fake([packet],role)['answer']),encoding='utf-8')
                return 0,json.dumps(dict(type='turn.completed',usage=dict(input_tokens=10,output_tokens=5))),''
            runner._capture=capture
            response=runner([packet],role)
            self.assertEqual(response['trading_skill'],self.profile.metadata)
            self.assertIn(self.profile.body,captures[0]);self.assertIn(packet['packet_id'],captures[0])
            self.assertEqual(response['answer']['role'],role)

    def test_real_pretrade_and_exit_providers_receive_skill_and_log_it(self):
        runtime=LegacyRuntime.__new__(LegacyRuntime);runtime.stopped=lambda:False
        runtime.output=self.root/'live'/'runtime';runtime.output.mkdir(parents=True)
        runtime.model=policy()['model'];runtime.production_skill=self.profile
        response=SimpleNamespace(output_text='{"reviews":[]}',usage=SimpleNamespace(model_dump=lambda:dict(input_tokens=10,output_tokens=5)))
        client=Mock();client.responses.create.return_value=response
        with patch('codex_cli.CodexClient',return_value=client):
            for role,format_name in [('pretrade','pretrade_review'),('position_exit','position_plan')]:
                req=dict(model=runtime.model,instructions='host',input='{}',text=dict(format=dict(name=format_name)))
                runtime.provider(req)
                actual=client.responses.create.call_args.kwargs
                self.assertIn(self.profile.body,actual['instructions']);self.assertIn(role,actual['instructions'])
                self.assertEqual(actual['input'],'{}')
                self.assertTrue(actual['_pin_model'])
        ledger=Ledger(runtime.output.parent);self.addCleanup(ledger.close)
        calls=[json.loads(x[0]) for x in ledger.db.execute('SELECT data FROM calls')]
        self.assertEqual(len(calls),2);self.assertTrue(all(x['trading_skill']==self.profile.metadata for x in calls))

    def test_skill_approved_buy_reaches_original_sdk_and_only_confirmed_fill_updates_position(self):
        fixture=self.fixture_bridge()
        fixture.runtime.snapshot['current_buy_budget_twd']=3373;fixture.runtime.context['available_cash_twd']=3373
        fixture.check_existing_executor_partial_fill('9000','8373')
        self.assertEqual(fixture.runtime.sent[0]['trading_skill'],self.profile.metadata)

    def test_skill_position_exit_reaches_same_executor_interface_when_buy_budget_zero(self):
        fixture=self.fixture_bridge()
        fixture.test_full_bridge_sells_held_position_without_any_new_buy_candidate()
        self.assertEqual(fixture.runtime.sent[0]['trading_skill'],self.profile.metadata)

    def test_unprofiled_payload_cannot_bypass_original_runtime(self):
        runtime=LegacyRuntime.__new__(LegacyRuntime);runtime.stopped=lambda:False
        runtime.read_only=False;runtime.production_skill=self.profile;runtime.broker=Mock()
        with self.assertRaisesRegex(ValueError,'EXECUTION_TRADING_SKILL_MISMATCH'):runtime.execute({},self.root/'advice.json')
        runtime.broker.execute_if_active.assert_not_called()
        runtime.read_only=True
        self.assertEqual(runtime.execute({},self.root/'advice.json')['status'],'READ_ONLY')

    def test_model_deadline_is_bounded_and_stdout_is_captured_without_pipes(self):
        from bounded_cli_capture import capture
        started=time.monotonic()
        with self.assertRaises(TimeoutError):
            asyncio.run(capture([sys.executable,'-c','import time;time.sleep(30)'],cwd=self.root,env=dict(os.environ),timeout=.15))
        self.assertLess(time.monotonic()-started,8)
        code,out,err=asyncio.run(capture([sys.executable,'-c','import sys;print(sys.stdin.read().upper())'],
            cwd=self.root,env=dict(os.environ),stdin=b'fixture',timeout=5))
        self.assertEqual(code,0);self.assertIn(b'FIXTURE',out)

    def test_descendant_holding_stdout_does_not_stall_completed_model(self):
        from bounded_cli_capture import capture
        script="import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(15)']); print(p.pid,flush=True)"
        started=time.monotonic();child=None
        try:
            code,out,err=asyncio.run(capture([sys.executable,'-c',script],cwd=self.root,env=dict(os.environ),timeout=4))
            child=int(out.strip());self.assertEqual(code,0)
            self.assertLess(time.monotonic()-started,5)
        finally:
            if child:
                if os.name=='nt':
                    import ctypes
                    from ctypes import wintypes
                    kernel=ctypes.windll.kernel32
                    kernel.OpenProcess.restype=wintypes.HANDLE
                    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
                    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
                    handle=kernel.OpenProcess(0x100000,False,child)
                    try:
                        try:os.kill(child,signal.SIGTERM)
                        except ProcessLookupError:pass
                        if handle:kernel.WaitForSingleObject(handle,5000)
                    finally:
                        if handle:kernel.CloseHandle(handle)
                else:
                    try:os.kill(child,signal.SIGTERM)
                    except ProcessLookupError:pass


if __name__=='__main__':unittest.main()
