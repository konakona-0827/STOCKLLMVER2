import copy
from datetime import date
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from decimal import Decimal

from config import now,trading_mode
from agent_decision import refreshed_market_context
from execution.auto_advice_executor import LiveExecutionConfig
from market_calendar import previous_trading_day
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

    def test_agent_model_is_independent_of_llm_model_and_can_be_overridden(self):
        observed = []
        async def runner(context, instructions, model, *args):
            observed.append(model)
            self.read(context)
            return copy.deepcopy(self.decision), {}
        with patch.dict(os.environ, {'OPENAI_MODEL':'gpt-5.6-luna'}, clear=True):
            self.assertEqual(agent_model(), 'gpt-6-sol')
            _, response = analyze(self.context, '策略', runner=runner)
            self.assertEqual(response['validation'], 'VALID')
            self.assertEqual(response['api']['model'], 'gpt-6-sol')
        with patch.dict(os.environ, {'OPENAI_MODEL':'gpt-5.6-luna',
                                     'OPENAI_AGENT_MODEL':'gpt-6.1-sol'}, clear=True):
            self.assertEqual(agent_model(), 'gpt-6.1-sol')
            _, response = analyze(self.context, '策略', runner=runner)
            self.assertEqual(response['validation'], 'VALID')
        self.assertEqual(observed, ['gpt-6-sol', 'gpt-6.1-sol'])

    def test_market_tool_uses_refreshed_portfolio_and_live_order_cap(self):
        self.candidate['max_sell_qty'] = 3
        self.context.rules = LiveExecutionConfig(
            max_order_twd=Decimal('200'), min_buy_fee_reserve_twd=Decimal('20'),
            min_available_to_buy_twd=Decimal('0'))
        self.context.quote_reader = lambda symbol, candidate: {**candidate, 'ask':120}
        self.context.portfolio_reader = lambda: dict(
            available_cash_cents=100000,
            positions=[dict(symbol='0050', qty=10, cost_cents=None)])
        self.assertIn('PORTFOLIO_MUST_BE_READ_FIRST',
                      self.context.get_market_data('0050')['error'])
        self.context.get_portfolio()
        market = self.context.get_market_data('0050')
        self.assertEqual(market['paper_position']['qty'], 10)
        self.assertEqual(market['max_sell_qty'], 3)
        self.assertEqual(market['max_buy_qty'], 1)
        self.context.get_portfolio()
        self.assertNotIn('0050', self.context.observed)

    def test_fresh_quote_preserves_verified_daily_strategy_evidence(self):
        today = date(2026, 10, 21)
        cursor = today
        bars = []
        for _ in range(5):
            cursor = previous_trading_day(cursor)
            bars.append(dict(date=cursor.isoformat(), open=99, high=101, low=98,
                             close=100, volume=1000, complete=True, source='TWSE_DAILY'))
        previous = dict(asset_type='ETF', market_history=[], daily_bars=bars,
                        daily_trend={'ma5': 100}, daily_history_status={'coverage': 'PARTIAL'})
        current = dict(symbol='0050', quote_timestamp='2026-10-21T10:00:00+08:00',
                       quote_status='LIVE', is_trial=False, last_price=100,
                       open=99, high=101, low=98, bid=99.9, ask=100, volume=1000)
        with patch('agent_decision.scan_quote', return_value={'metrics': {}, 'components': {}}):
            refreshed = refreshed_market_context(current, previous, {})
        self.assertEqual(refreshed['daily_bars'], bars)
        self.assertEqual(refreshed['daily_trend'], previous['daily_trend'])
        self.assertIn('multi_day_trend', refreshed['eligible_strategies'])
        self.assertEqual(refreshed['strategy_evidence']['history_kind'],
                         'TWSE_DAILY_AND_INTRADAY_SAMPLES')

    def scripted_model(self,endless=False,delay=0):
        try:
            from agents import Model,ModelResponse,Usage
            from openai.types.responses import ResponseFunctionToolCall,ResponseOutputMessage,ResponseOutputText
        except ImportError:
            self.skipTest('Optional Agents SDK not installed')
        actions = [('get_portfolio',{}),('get_trade_history',{}),('get_market_data',{'symbol':'0050'}),
                   ('get_market_data',{'symbol':'0050'})]
        report = copy.deepcopy(self.decision)
        class ScriptedModel(Model):
            def __init__(self):
                self.requests,self.tools = 0,set()
            async def get_response(self,*args,**kwargs):
                import asyncio
                if delay:
                    await asyncio.sleep(delay)
                self.tools.update(t.name for t in kwargs.get('tools',[]))
                i = self.requests
                self.requests += 1
                if i<len(actions) or endless:
                    name,arguments = actions[i] if i<len(actions) else ('get_portfolio',{})
                    out = [ResponseFunctionToolCall(type='function_call',name=name,arguments=json.dumps(arguments),
                                                   call_id=f'call-{i}',id=f'fc-{i}')]
                else:
                    out = [ResponseOutputMessage(type='message',role='assistant',id='final',status='completed',
                        content=[ResponseOutputText(type='output_text',text=json.dumps(report),annotations=[])])]
                return ModelResponse(output=out,usage=Usage(requests=1,input_tokens=10,output_tokens=5),response_id=f'r-{i}')
            async def stream_response(self,*args,**kwargs):
                raise NotImplementedError
                yield
        return ScriptedModel()

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
