from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from config import CODE_ROOT
from runtime_skills import load_entry_research, entry_research_report


class RuntimeSkillTests(unittest.TestCase):
    def context(self, directory, candidates):
        return SimpleNamespace(candidates={c['symbol']:c for c in candidates},
            log_path=Path(directory)/'agent_tools.json',support=None,calls=[])

    def test_routes_entry_and_watch_candidates_and_saves_exact_skill_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            cs=[dict(symbol='1101',opportunity_policy={'enabled':True},trade_setups=[dict(setup_id='plan-1')]),
                dict(symbol='2891',opportunity_policy={'enabled':True},research_only=True),
                dict(symbol='0050',paper_position=dict(qty=10))]
            original=deepcopy(cs)
            context=self.context(tmp,cs)
            instructions=load_entry_research(context)
            raw=(CODE_ROOT/'skills/tw-entry-research/SKILL.md').read_bytes()
            self.assertIn('tw-entry-research',instructions)
            self.assertEqual(context.entry_research_skill['symbols'],['1101','2891'])
            self.assertEqual(context.entry_research_skill['version'],'2')
            self.assertEqual(context.entry_research_skill['sha256'],hashlib.sha256(raw).hexdigest())
            self.assertEqual((Path(tmp)/'entry_research_skill.md').read_bytes(),raw)
            self.assertEqual(cs,original)
            report=entry_research_report(context,dict(decisions=[dict(symbol='1101',decision='WAIT',
                reason='等待具體反證排除')]),dict(validation='VALID'))
            self.assertEqual(report['entries'][0]['setup_ids'],['plan-1'])
            self.assertEqual(report['entries'][0]['decision'],'WAIT')
            self.assertTrue(report['entries'][1]['research_only'])

    def test_holdings_without_entry_research_do_not_load_a_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            context=self.context(tmp,[dict(symbol='1101',paper_position=dict(qty=10))])
            self.assertEqual(load_entry_research(context,skill_root=Path(tmp)/'missing'),'')
            self.assertIsNone(entry_research_report(context,{},{}))
            self.assertFalse((Path(tmp)/'entry_research_skill.md').exists())

    def test_missing_or_invalid_installed_skill_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            context=self.context(tmp,[dict(symbol='1101',opportunity_policy={'enabled':True},research_only=True)])
            with self.assertRaises(FileNotFoundError):load_entry_research(context,skill_root=tmp)
            skill=Path(tmp)/'tw-entry-research/SKILL.md';skill.parent.mkdir()
            skill.write_text('---\nname: another-skill\n---\nUntrusted replacement',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'INVALID_ENTRY_RESEARCH_SKILL'):
                load_entry_research(context,skill_root=tmp)

    def test_skill_load_failure_keeps_real_agent_validation_fail_closed(self):
        from test_opportunity_search import candidate, policy, refresh
        from trading_agent import TradingTools, analyze
        from execution.auto_advice_executor import LiveExecutionConfig
        with tempfile.TemporaryDirectory() as tmp:
            rules=LiveExecutionConfig()
            account=dict(available_cash_cents=500000,available_cash_twd=5000,capital_limit_twd=10000,
                         positions=[],budget_status='READY')
            c=dict(candidate('1101'),opportunity_policy=policy(),research_only=True)
            context=TradingTools([c],rules,'missing-skill',Path(tmp)/'tools.json',
                lambda *args:c,lambda:account,lambda:[])
            with patch('runtime_skills.CODE_ROOT',Path(tmp)/'missing'), patch('trading_agent.sdk_run') as run:
                decision,response=analyze(context,'既有策略')
            self.assertEqual(response['validation'],'ERROR')
            self.assertEqual(decision['decisions'][0]['decision'],'WAIT')
            self.assertFalse(context.calls)
            run.assert_not_called()
