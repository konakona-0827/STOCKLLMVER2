import asyncio
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import codex_cli
from config import now
from execution.auto_advice_executor import LiveExecutionConfig
from trading_agent import TradingTools, analyze


class CodexTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.commands, self.answers, self.events = [], [], []
        self.request = dict(model='gpt-6-sol', instructions='test', input='test',
            text=dict(format=dict(type='json_schema', schema=dict(type='object',
                properties=dict(ok=dict(type='boolean')), required=['ok'], additionalProperties=False))))

    async def capture(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if command[-2:] == ['login', 'status']:
            return 0, '', 'Logged in using ChatGPT\n'
        answer = self.answers.pop(0)
        Path(command[command.index('--output-last-message')+1]).write_text(
            json.dumps(answer), encoding='utf-8')
        events = self.events + [dict(type='turn.completed', usage=dict(
            input_tokens=20, cached_input_tokens=5, output_tokens=10))]
        return 0, '\n'.join(json.dumps(e) for e in events), ''

    def transport(self):
        return patch('codex_cli._capture', side_effect=self.capture)

    def test_chatgpt_only_ignores_api_keys_and_preserves_structured_output(self):
        self.answers = [dict(tool_call=None, final_output=dict(ok=True))]
        with patch.dict(os.environ, dict(OPENAI_API_KEY='sk-do-not-pass', CODEX_API_KEY='also-secret',
                                         CAPITAL_PASSWORD='broker-secret')), self.transport():
            response = asyncio.run(codex_cli.create_response(self.request))
        self.assertEqual(json.loads(response.output_text), dict(ok=True))
        self.assertEqual(response.metadata['billing_source'], 'chatgpt-plan')
        self.assertEqual(response.usage.input_tokens_details.cached_tokens, 5)
        command, options = self.commands[-1]
        self.assertNotIn('OPENAI_API_KEY', options['env'])
        self.assertNotIn('CODEX_API_KEY', options['env'])
        self.assertNotIn('CAPITAL_PASSWORD', options['env'])
        self.assertIn('forced_login_method="chatgpt"', command)
        self.assertIn('--ignore-user-config', command)
        self.assertIn('features.shell_tool=false', command)
        self.assertIn('read-only', command)
        self.assertFalse(Path(options['cwd']).exists())

    def test_api_key_login_refused_before_inference(self):
        capture = AsyncMock(return_value=(0, 'Logged in using an API key', ''))
        with patch('codex_cli._capture', capture):
            with self.assertRaisesRegex(RuntimeError, 'CHATGPT_LOGIN_REQUIRED'):
                asyncio.run(codex_cli.create_response(self.request))
        self.assertEqual(capture.await_count, 1)

    def test_failed_cli_never_falls_back_to_api(self):
        capture = AsyncMock(side_effect=[(0, 'Logged in using ChatGPT', ''), (1, '', 'quota exhausted')])
        with patch('codex_cli._capture', capture), patch('openai.AsyncOpenAI', side_effect=AssertionError('API used')):
            with self.assertRaisesRegex(RuntimeError, 'CODEX_EXEC_FAILED'):
                asyncio.run(codex_cli.create_response(self.request))
        self.assertEqual(capture.await_count, 2)

    def test_invalid_final_schema_is_rejected(self):
        from jsonschema import ValidationError
        self.answers = [dict(tool_call=None, final_output=dict(ok='yes'))]
        with self.transport(), self.assertRaises(ValidationError):
            asyncio.run(codex_cli.create_response(self.request))

    def test_native_search_sources_are_preserved_without_api_cost(self):
        from web_research import search_audit
        self.request.update(tools=[dict(type='web_search')], max_tool_calls=1)
        self.answers = [dict(tool_call=None, final_output=dict(ok=True))]
        self.events = [dict(type='item.completed', item=dict(id='search-1', type='web_search',
            action=dict(type='search', query='test'),
            results=[dict(url='https://example.com/evidence', title='Evidence')]))]
        with self.transport():
            response = asyncio.run(codex_cli.create_response(self.request))
        audit = search_audit(response.model_dump())
        self.assertEqual(audit['tool_calls'], 1)
        self.assertEqual(audit['sources'][0]['url'], 'https://example.com/evidence')
        self.assertEqual(audit['billing_source'], 'chatgpt-plan')
        self.assertIsNone(audit['estimated_search_tool_cost_usd'])

    def test_search_limit_and_missing_completion_reject_output(self):
        self.request.update(tools=[dict(type='web_search')], max_tool_calls=1)
        self.answers = [dict(tool_call=None, final_output=dict(ok=True))]
        self.events = [dict(type='item.completed', item=dict(id=f'search-{i}', type='web_search',
            action=dict(type='search', query='test'))) for i in range(2)]
        with self.transport(), self.assertRaisesRegex(RuntimeError, 'WEB_SEARCH_LIMIT'):
            asyncio.run(codex_cli.create_response(self.request))
        with patch('codex_cli._capture', AsyncMock(side_effect=[
                (0,'Logged in using ChatGPT',''), (0,'','')])):
            with self.assertRaisesRegex(RuntimeError, 'INCOMPLETE_OUTPUT'):
                asyncio.run(codex_cli.create_response(self.request))

    def test_plan_usage_is_excluded_from_legacy_api_cost_estimate(self):
        from operations.audit_agent_usage import audit
        import shutil
        root = Path(self.tmp.name)
        (root/'config').mkdir()
        shutil.copyfile(Path(__file__).resolve().parents[1]/'config/execution_live.json', root/'config/execution_live.json')
        directory = root/'data/analysis/runs/cli-run'
        directory.mkdir(parents=True)
        (directory/'run.json').write_text(json.dumps(dict(started_at='2026-11-09T10:00:00+09:00')), encoding='utf-8')
        (directory/'agent_response.json').write_text(json.dumps(dict(api=dict(
            billing_source='chatgpt-plan', usage=dict(input_tokens=1000,output_tokens=200)))), encoding='utf-8')
        result = audit(root, '2026-11-09')
        self.assertEqual(result['codex_plan_tokens'], 1200)
        self.assertEqual(result['total_cost_usd_estimate'], 0)

    def test_ambiguous_and_unknown_tool_outputs_rejected(self):
        for answer in (dict(tool_call=None, final_output=None),
                       dict(tool_call=dict(name='execute_order', arguments='{}'), final_output=None)):
            self.answers = [answer]
            with self.transport(), self.assertRaises(Exception):
                asyncio.run(codex_cli.create_response(self.request))

    def test_sdk_function_roundtrip_and_pretrade_review_use_cli_without_api(self):
        candidate = dict(symbol='0050', last_price=100, bid=99.9, ask=100,
            quote_status='LIVE', open=100, high=130, low=95, quote_timestamp=now().isoformat(),
            is_trial=False, asset_type='ETF', max_buy_qty=9, eligible_strategies=['intraday_recovery'],
            paper_position=dict(qty=0), position=dict(ai_managed_qty=0))
        context = TradingTools([candidate], LiveExecutionConfig(min_available_to_buy_twd=Decimal('0')),
            'cli-test', Path(self.tmp.name)/'agent_tools.json', lambda s, c: dict(c),
            lambda: dict(available_cash_cents=100000, positions=[]), lambda: [])
        report = dict(market_view='測試', decisions=[dict(symbol='0050', decision='BUY', confidence=.8,
            action_ratio=0, suggested_qty=5, reference_price=100, suggested_price=100,
            strategy_id='intraday_recovery', target_price=130, invalidation_price=95, holding_days=10,
            reason='測試買進', data_quality='LIVE', warnings=[])])
        self.answers = [dict(tool_call=dict(name='get_market_data', arguments='{"symbol":"0050"}'), final_output=None),
                        dict(tool_call=None, final_output=report),
                        dict(tool_call=None, final_output=dict(reviews=[dict(symbol='0050', approved=True, reason='核准')]))]
        with self.transport(), patch('openai.AsyncOpenAI', side_effect=AssertionError('API used')):
            decision, response = analyze(context, '測試策略')
        self.assertEqual(response['validation'], 'VALID', response)
        self.assertEqual(decision['decisions'][0]['decision'], 'BUY', response)
        self.assertEqual(response['api']['runtime'], 'codex-cli')
        self.assertEqual(response['pretrade_review']['status'], 'VALID')
        self.assertEqual([c['tool'] for c in context.calls], ['get_portfolio', 'get_trade_history', 'get_market_data'])
        self.assertFalse(self.answers)

    def test_cli_failure_results_in_wait(self):
        candidate = dict(symbol='0050', last_price=100, quote_status='LIVE')
        context = TradingTools([candidate], LiveExecutionConfig(), 'failed',
            Path(self.tmp.name)/'agent_tools.json', lambda s, c: c,
            lambda: dict(available_cash_cents=0, positions=[]), lambda: [])
        capture = AsyncMock(return_value=(1, '', 'not logged in'))
        with patch('codex_cli._capture', capture):
            decision, response = analyze(context, 'test')
        self.assertEqual(response['validation'], 'ERROR')
        self.assertEqual(decision['decisions'][0]['decision'], 'WAIT')

    def test_cancelled_process_is_killed(self):
        async def blocked(_):
            await asyncio.Event().wait()
        process = unittest.mock.Mock(pid=123, returncode=None)
        process.communicate = blocked
        kill = AsyncMock()
        with patch('asyncio.create_subprocess_exec', AsyncMock(return_value=process)), patch('codex_cli._kill', kill):
            with self.assertRaises(TimeoutError):
                asyncio.run(codex_cli._capture(['codex'], cwd=self.tmp.name, env={}, timeout=.01))
        kill.assert_awaited_once_with(process)


if __name__ == '__main__':
    unittest.main()
