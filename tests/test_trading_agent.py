import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from decimal import Decimal

from config import now,trading_mode
from execution.auto_advice_executor import LiveExecutionConfig
from trading_agent import TradingTools,agent_model,analyze


class AgentDecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rules = LiveExecutionConfig(min_available_to_buy_twd=Decimal('0'))
        self.candidate = dict(symbol='0050',last_price=100,bid=99.9,ask=100,quote_status='LIVE',
            quote_timestamp=now().isoformat(),is_trial=False,asset_type='ETF',max_buy_qty=9,
            eligible_strategies=['intraday_recovery'],paper_position=dict(qty=0),position=dict(ai_managed_qty=0))
        self.context = TradingTools([self.candidate],self.rules,'test',Path(self.tmp.name)/'agent_tools.json',
            lambda symbol,c:dict(c),lambda:dict(available_cash_cents=100000,positions=[]),lambda:[])
        self.decision = dict(market_view='測試',decisions=[dict(symbol='0050',decision='BUY',confidence=0.8,
            action_ratio=0,suggested_qty=5,reference_price=100,suggested_price=100,strategy_id='intraday_recovery',
            target_price=130,invalidation_price=95,holding_days=10,reason='測試買進',data_quality='LIVE',warnings=[])])

    def read(self,context):
        context.get_portfolio()
        context.get_trade_history()
        context.get_market_data('0050')

    def test_sol_default_is_independent_of_llm_model(self):
        with patch.dict(os.environ, {'OPENAI_MODEL': 'legacy-llm'}):
            os.environ.pop('OPENAI_AGENT_MODEL', None)
            self.assertEqual(agent_model(), 'gpt-6-sol')
            os.environ['OPENAI_AGENT_MODEL'] = 'explicit-agent-model'
            self.assertEqual(agent_model(), 'explicit-agent-model')

    def test_account_refresh_requires_a_new_market_observation(self):
        self.assertIn('error', self.context.get_market_data('0050'))
        async def runner(context, *args):
            self.read(context)
            context.get_portfolio()
            return copy.deepcopy(self.decision), {}
        decision, response = analyze(self.context, '策略', runner=runner)
        self.assertEqual(response['validation'], 'ERROR')
        self.assertEqual(decision['decisions'][0]['decision'], 'WAIT')

    def test_existing_json_contract_validates_and_bad_budget_fails_closed(self):
        async def runner(context,*args):
            self.read(context)
            return copy.deepcopy(self.decision),dict(input_tokens=1,output_tokens=1)
        decision,response = analyze(self.context,'策略',runner=runner)
        self.assertEqual(response['validation'],'VALID')
        self.assertEqual(decision['decisions'][0]['decision'],'BUY')
        self.decision['decisions'][0]['suggested_qty'] = 10
        _,response = analyze(self.context,'策略',runner=runner)
        self.assertEqual(response['validation'],'ERROR')
        self.assertIn('EXCEEDS',response['api']['error'])

    def test_invalid_json_and_read_only_never_emit_trade(self):
        async def runner(context,*args):
            self.read(context)
            return copy.deepcopy(self.decision),{}
        self.context.read_only = True
        decision,response = analyze(self.context,'策略',runner=runner)
        self.assertEqual(response['validation'],'ERROR')
        self.assertEqual(decision['decisions'][0]['decision'],'WAIT')
        self.context.read_only = False
        del self.decision['decisions'][0]['reason']
        _,response = analyze(self.context,'策略',runner=runner)
        self.assertEqual(response['validation'],'ERROR')

    def test_unknown_symbol_and_invalid_mode_are_rejected(self):
        self.assertIn('error',self.context.get_market_data('SHELL'))
        with patch.dict(os.environ,{'TRADING_MODE':'invalid'}):
            with self.assertRaises(ValueError):
                trading_mode()

    def test_refreshes_use_the_same_live_fee_and_order_limits(self):
        self.context.rules = LiveExecutionConfig(
            min_buy_fee_reserve_twd=Decimal('100'), max_order_twd=Decimal('550'))
        self.candidate['ask'] = 110
        self.context.get_portfolio()
        self.assertEqual(self.context.get_market_data('0050')['max_buy_qty'], 5)
        self.context.portfolio_reader = lambda: dict(available_cash_cents=30000, positions=[])
        self.context.get_portfolio()
        self.assertEqual(self.candidate['max_buy_qty'], 1)
        self.assertEqual(self.candidate['budget_price'], 110)
        self.context.live_enabled = False
        self.context.get_portfolio()
        self.assertEqual(self.candidate['max_buy_qty'], 2)
        self.assertEqual(self.candidate['budget_price'], 100)

    def test_latest_failed_tool_read_cannot_reuse_an_earlier_success(self):
        for reader, method in (('portfolio_reader', 'get_portfolio'),
                               ('history_reader', 'get_trade_history'),
                               ('quote_reader', 'get_market_data')):
            with self.subTest(tool=method):
                async def runner(context, *args):
                    self.read(context)
                    with patch.object(context, reader, side_effect=RuntimeError('test outage')):
                        result = getattr(context, method)(*(['0050'] if method == 'get_market_data' else []))
                    self.assertIn('error', result)
                    return copy.deepcopy(self.decision), {}
                decision, response = analyze(self.context, '策略', runner=runner)
                self.assertEqual(response['validation'], 'ERROR')
                self.assertEqual(decision['decisions'][0]['decision'], 'WAIT')

    def test_tool_log_preserves_the_original_snapshot(self):
        portfolio = dict(available_cash_cents=100000, positions=[dict(symbol='0050', qty=10)])
        self.context.portfolio_reader = lambda: portfolio
        self.context.get_portfolio()
        portfolio['positions'][0]['qty'] = 5
        self.context.get_portfolio()
        self.assertEqual(self.context.calls[0]['result']['positions'][0]['qty'], 10)
        self.assertEqual(self.context.calls[1]['result']['positions'][0]['qty'], 5)

    def scripted_model(self,endless=False,delay=0,web_search=False,excess_search=False):
        try:
            from agents import Model,ModelResponse,Usage
            from openai.types.responses import (ResponseFunctionToolCall, ResponseFunctionWebSearch,
                                               ResponseOutputMessage, ResponseOutputText)
        except ImportError:
            self.skipTest('Optional Agents SDK not installed')
        actions = [('get_portfolio',{}),('get_trade_history',{}),('get_market_data',{'symbol':'0050'}),
                   ('get_market_data',{'symbol':'0050'})]
        report = copy.deepcopy(self.decision)
        class ScriptedModel(Model):
            def __init__(self):
                self.requests,self.tools = 0,set()
                self.tool_sets, self.search_limits = [], []
            async def get_response(self,*args,**kwargs):
                import asyncio
                if delay:
                    await asyncio.sleep(delay)
                current_tools = {t.name for t in kwargs.get('tools', [])}
                self.tools.update(current_tools)
                self.tool_sets.append(current_tools)
                self.search_limits.append((kwargs['model_settings'].extra_args or {}).get('max_tool_calls'))
                i = self.requests
                self.requests += 1
                if i<len(actions) or endless:
                    name,arguments = actions[i] if i<len(actions) else ('get_portfolio',{})
                    out = [ResponseFunctionToolCall(type='function_call',name=name,arguments=json.dumps(arguments),
                                                   call_id=f'call-{i}',id=f'fc-{i}')]
                else:
                    out = [ResponseOutputMessage(type='message',role='assistant',id='final',status='completed',
                        content=[ResponseOutputText(type='output_text',text=json.dumps(report),annotations=[])])]
                if web_search and 'web_search' in current_tools and i < 2:
                    for search_index in range(3 if excess_search else 1):
                        out.insert(0, ResponseFunctionWebSearch(
                            id=f'web-{i}-{search_index}', type='web_search_call', status='completed',
                            action=dict(type='search', queries=['測試公司公告'], sources=[
                                dict(type='url', url='https://example.com/announcement')])))
                return ModelResponse(output=out,usage=Usage(requests=1,input_tokens=10,output_tokens=5),response_id=f'r-{i}')
            async def stream_response(self,*args,**kwargs):
                raise NotImplementedError
                yield
        return ScriptedModel()

    def test_sdk_search_reuses_config_and_caps_the_whole_run(self):
        self.context.search_request = dict(
            tools=[dict(type='web_search', external_web_access=True,
                        search_context_size='medium', user_location=dict(
                            type='approximate', country='TW', timezone='Asia/Taipei'))],
            max_tool_calls=2, include=['web_search_call.action.sources'])
        model = self.scripted_model(web_search=True)
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-test'}), \
                patch('agents.OpenAIResponsesModel', return_value=model):
            decision, response = analyze(self.context, '策略')
        self.assertEqual(response['validation'], 'VALID', response)
        self.assertEqual(decision['decisions'][0]['decision'], 'BUY')
        self.assertEqual(model.search_limits[:2], [2, 1])
        self.assertTrue(all('web_search' not in tools for tools in model.tool_sets[2:]))
        self.assertEqual(response['api']['web_search']['tool_calls'], 2)
        self.assertEqual(response['api']['web_search']['sources'][0]['url'],
                         'https://example.com/announcement')
        self.assertIsNone(response['api']['web_search']['billed_search_requests'])

    def test_sdk_search_over_limit_discards_decision(self):
        self.context.search_request = dict(tools=[dict(type='web_search')], max_tool_calls=2)
        model = self.scripted_model(web_search=True, excess_search=True)
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-test'}), \
                patch('agents.OpenAIResponsesModel', return_value=model):
            decision, response = analyze(self.context, '策略')
        self.assertEqual(response['validation'], 'ERROR')
        self.assertEqual(decision['decisions'][0]['decision'], 'WAIT')
        self.assertIn('AGENT_WEB_SEARCH_LIMIT_EXCEEDED', response['api']['error'])

    def test_real_responses_adapter_serializes_search_budget_and_sources(self):
        try:
            import agents
            try:
                import httpx2 as httpx
            except ImportError:
                import httpx
            from openai import AsyncOpenAI
        except ImportError:
            self.skipTest('Optional Agents SDK not installed')
        self.context.search_request = dict(tools=[dict(type='web_search', external_web_access=True)],
                                           max_tool_calls=2, include=['web_search_call.action.sources'])
        requests = []
        actions = [('get_portfolio', {}), ('get_trade_history', {}), ('get_market_data', {'symbol': '0050'})]
        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            index = len(requests) - 1
            if index < len(actions):
                name, arguments = actions[index]
                output = [dict(type='function_call', name=name, arguments=json.dumps(arguments),
                               call_id=f'call-{index}', id=f'fc-{index}')]
            else:
                output = [dict(type='message', role='assistant', id='final', status='completed',
                               content=[dict(type='output_text', text=json.dumps(self.decision), annotations=[])])]
            if index < 2:
                output.insert(0, dict(type='web_search_call', id=f'web-{index}', status='completed',
                                     action=dict(type='search', queries=['test announcement'], sources=[
                                         dict(type='url', url='https://example.com/announcement')])))
            return httpx.Response(200, json=dict(
                id=f'resp-{index}', object='response', created_at=0, model='test-model', status='completed',
                parallel_tool_calls=False, tool_choice='auto', tools=body['tools'], output=output,
                usage=dict(input_tokens=10, output_tokens=5, total_tokens=15)))
        def client(**kwargs):
            return AsyncOpenAI(api_key='sk-test', http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler)), **kwargs)
        with patch('openai.AsyncOpenAI', side_effect=client):
            decision, response = analyze(self.context, '策略', model='test-model')
        self.assertEqual(response['validation'], 'VALID', response)
        self.assertEqual(decision['decisions'][0]['decision'], 'BUY')
        self.assertEqual([body.get('max_tool_calls') for body in requests], [2, 1, None, None])
        self.assertTrue(all(body['store'] is False for body in requests))
        self.assertIn('web_search', {tool['type'] for tool in requests[0]['tools']})
        self.assertNotIn('web_search', {tool['type'] for tool in requests[2]['tools']})
        self.assertEqual(response['api']['web_search']['tool_calls'], 2)

    def test_actual_sdk_loop_returns_original_json_and_only_read_tools(self):
        model = self.scripted_model()
        with patch.dict(os.environ,{'OPENAI_API_KEY':'sk-test'}),patch('agents.OpenAIResponsesModel',return_value=model):
            result,response = analyze(self.context,'策略')
        self.assertEqual(response['validation'],'VALID',response)
        self.assertEqual(model.requests,5)
        self.assertEqual(model.tools,{'get_portfolio','get_trade_history','get_market_data'})
        self.assertEqual(result['decisions'][0]['suggested_qty'],5)

    def test_sdk_turn_limit_discards_decision(self):
        model = self.scripted_model(endless=True)
        with patch.dict(os.environ,{'OPENAI_API_KEY':'sk-test','AGENT_MAX_TURNS':'5'}), \
                patch('agents.OpenAIResponsesModel',return_value=model):
            result,response = analyze(self.context,'策略')
        self.assertEqual(response['validation'],'ERROR')
        self.assertIn('MaxTurnsExceeded',response['api']['error'])
        self.assertEqual(result['decisions'][0]['decision'],'WAIT')

    def test_sdk_timeout_discards_decision(self):
        model = self.scripted_model(delay=2)
        with patch.dict(os.environ,{'OPENAI_API_KEY':'sk-test','AGENT_TIMEOUT_SECONDS':'1'}), \
                patch('agents.OpenAIResponsesModel',return_value=model):
            _,response = analyze(self.context,'策略')
        self.assertEqual(response['validation'],'ERROR')
        self.assertIn('TimeoutError',response['api']['error'])


if __name__=='__main__':
    unittest.main()
