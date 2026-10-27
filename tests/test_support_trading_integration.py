import asyncio
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from config import now, TAIPEI
import daily_history
from execution.auto_advice_executor import LiveExecutionConfig
from support_scout.trading import SupportCoordinator, settings
from trading_agent import TradingTools, analyze


class SupportTradingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.candidate = dict(symbol='2330', name='測試', asset_type='STOCK', last_price=100,
            bid=99.9, ask=100, open=100, high=130, low=95, volume=10000,
            quote_status='LIVE', quote_timestamp=now().isoformat(), is_trial=False,
            paper_position=dict(qty=0), position=dict(ai_managed_qty=0), max_buy_qty=9,
            eligible_strategies=['intraday_recovery'])
        self.context = TradingTools([self.candidate], LiveExecutionConfig(), 'support-test',
            self.root/'agent_tools.json', lambda symbol, previous: dict(previous),
            lambda: dict(available_cash_cents=100000, capital_limit_twd=100000, positions=[]), lambda: [])
        self.config = dict(enabled=True, policy='advisory', history_bars=100,
            max_symbols=2, max_calls=4, timeout_seconds=3, max_output_tokens=1800)
        self.observation = dict(status='RAW_PRICE_OBSERVATION', technical=None,
            limitations=['CORPORATE_ACTIONS_UNVERIFIED'], history_status=dict(latest_date='2026-10-22'))
        self.context.support = SupportCoordinator(self.context, self.root, self.config,
                                                 lambda *args: deepcopy(self.observation))
        self.coordinator = self.context.support
        self.decision = dict(market_view='測試', decisions=[dict(symbol='2330', decision='BUY',
            confidence=.8, action_ratio=0, suggested_qty=5, reference_price=100, suggested_price=100,
            strategy_id='intraday_recovery', target_price=130, invalidation_price=95, holding_days=10,
            reason='測試支撐資料結合既有策略', data_quality='LIVE', warnings=[])])

    def read(self):
        self.context.get_portfolio()
        self.context.get_trade_history()

    def accept_pair(self, verdict='WATCH'):
        self.read()
        for role in ('researcher', 'reviewer'):
            packet = self.coordinator.prepare(role, '2330')
            self.coordinator.accept(role, '2330', dict(verdict=verdict,
                evidence_ids=packet['core']['required_confirmation_evidence'], summary=role+'意見',
                risks=['模型研究不是交易批准'], missing_data=['公司行動未核實'] if verdict=='WATCH' else []))

    def test_advisory_accepts_valid_watch_but_strict_support_requires_full_evidence(self):
        self.accept_pair()
        self.assertEqual(self.coordinator.buy_block_reasons('2330'), [])
        self.coordinator.config['policy'] = 'confirmed_support'
        self.assertEqual(self.coordinator.buy_block_reasons('2330'), ['SUPPORT_CONFIRMED_RESEARCH_REQUIRED'])

    def test_quote_refresh_preserves_daily_research_but_changed_account_invalidates_it(self):
        self.accept_pair()
        self.candidate.update(quote_timestamp=now().isoformat(), last_price=101)
        self.assertEqual(self.coordinator.buy_block_reasons('2330'), [])
        self.context.account['available_cash_cents'] = 100
        self.assertEqual(self.coordinator.buy_block_reasons('2330'), ['SUPPORT_INDEPENDENT_REVIEWS_REQUIRED'])

    def test_failed_replacement_does_not_reuse_previous_reviewer_pass(self):
        self.accept_pair()
        self.coordinator.prepare('reviewer', '2330')
        self.coordinator.fail('reviewer', '2330', TimeoutError('test timeout'))
        self.assertEqual(self.coordinator.buy_block_reasons('2330'), ['SUPPORT_INDEPENDENT_REVIEWS_REQUIRED'])

    def test_role_reject_blocks_buy(self):
        self.accept_pair('REJECT')
        self.assertEqual(self.coordinator.buy_block_reasons('2330'), ['SUPPORT_AGENT_REJECTED'])

    def test_unknown_symbols_missing_account_and_call_budget_are_enforced(self):
        with self.assertRaisesRegex(ValueError, 'CURRENT_ACCOUNT'):
            self.coordinator.prepare('researcher', '2330')
        self.read()
        with self.assertRaisesRegex(ValueError, 'OUTSIDE_ANALYSIS_SET'):
            self.coordinator.prepare('researcher', '9999')
        for _ in range(4):
            self.coordinator.prepare('researcher', '2330')
        with self.assertRaisesRegex(ValueError, 'BUDGET_EXHAUSTED'):
            self.coordinator.prepare('reviewer', '2330')

    def test_invented_evidence_is_rejected(self):
        self.read()
        self.coordinator.prepare('reviewer', '2330')
        with self.assertRaisesRegex(ValueError, 'INVALID_SUPPORT_REVIEW'):
            self.coordinator.accept('reviewer', '2330', dict(verdict='RESEARCH', evidence_ids=['invented'],
                summary='不能捏造證據', risks=[], missing_data=[]))

    def model(self, invalid_reviewer=False, researcher_reject=False):
        from agents import Model, ModelResponse, Usage
        from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText
        actions = [('get_portfolio', {}), ('get_trade_history', {}),
                   ('research_support', {'symbol': '2330'}), ('review_support', {'symbol': '2330'}),
                   ('get_market_data', {'symbol': '2330'})]
        final = deepcopy(self.decision)

        def packet_in(value):
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, dict) and 'packet_id' in parsed and 'core' in parsed:
                        return parsed
                except ValueError:
                    pass
            if isinstance(value, dict):
                for child in value.values():
                    found = packet_in(child)
                    if found:
                        return found
            if isinstance(value, list):
                for child in value:
                    found = packet_in(child)
                    if found:
                        return found
            return None

        class RoutedModel(Model):
            def __init__(self):
                self.parent_calls = 0
                self.child_inputs = []
                self.child_roles = []

            async def get_response(self, *args, **kwargs):
                instructions = kwargs.get('system_instructions') or ''
                child = '你是獨立的台股支撐' in str(instructions)
                if child:
                    role = 'reviewer' if '你是獨立的台股支撐反證員' in instructions else 'researcher'
                    packet = packet_in(kwargs.get('input'))
                    assert packet is not None, kwargs
                    self.child_inputs.append(deepcopy(packet))
                    self.child_roles.append(role)
                    answer = dict(verdict='REJECT' if researcher_reject and role=='researcher' else 'WATCH',
                        evidence_ids=['invented'] if invalid_reviewer and role=='reviewer'
                                     else packet['core']['required_confirmation_evidence'],
                        summary='private_researcher_summary' if role=='researcher' else 'independent_reviewer_summary',
                        risks=['未證明投資優勢'], missing_data=['公司行動未核實'])
                else:
                    index = self.parent_calls
                    self.parent_calls += 1
                    if index < len(actions):
                        name, arguments = actions[index]
                        return ModelResponse(output=[ResponseFunctionToolCall(type='function_call', name=name,
                            arguments=json.dumps(arguments), call_id=f'call-{index}', id=f'fc-{index}')],
                            usage=Usage(requests=1, input_tokens=5, output_tokens=5, total_tokens=10), response_id=f'p-{index}')
                    answer = final
                return ModelResponse(output=[ResponseOutputMessage(type='message', role='assistant', id='final',
                    status='completed', content=[ResponseOutputText(type='output_text', text=json.dumps(answer),
                        annotations=[])])], usage=Usage(requests=1, input_tokens=5, output_tokens=5, total_tokens=10),
                    response_id='synthetic')

            async def stream_response(self, *args, **kwargs):
                raise NotImplementedError
                yield
        return RoutedModel()

    def sdk_analyze(self, **options):
        model = self.model(**options)
        def approve(request):
            return json.dumps(dict(reviews=[dict(symbol='2330', approved=True, reason='測試獨立交易前審查')])), {}
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-test'}), \
                patch('agents.OpenAIResponsesModel', return_value=model):
            decision, response = analyze(self.context, '測試策略', reviewer=approve)
        return decision, response, model

    def test_real_sdk_manager_invokes_two_independent_agent_tools_then_existing_trade_validation(self):
        decision, response, model = self.sdk_analyze()
        self.assertEqual(response['validation'], 'VALID', response)
        self.assertEqual(decision['decisions'][0]['decision'], 'BUY', response.get('support_research'))
        self.assertEqual(model.child_roles, ['researcher', 'reviewer'])
        self.assertEqual(model.child_inputs[0]['packet_id'], model.child_inputs[1]['packet_id'])
        self.assertNotIn('private_researcher_summary', json.dumps(model.child_inputs[1]))
        calls = response['support_research']['calls']
        self.assertEqual([c['status'] for c in calls], ['COMPLETE', 'COMPLETE'])
        self.assertNotEqual(calls[0]['review']['agent_id'], calls[1]['review']['agent_id'])
        self.assertTrue(all(c['usage']['total_tokens']==10 for c in calls))
        self.assertEqual(response['pretrade_review']['status'], 'VALID')

    def test_invalid_subagent_output_vetoes_buy_without_replacing_the_formal_executor(self):
        decision, response, model = self.sdk_analyze(invalid_reviewer=True)
        self.assertEqual(response['validation'], 'VALID', response)
        self.assertEqual(decision['decisions'][0]['decision'], 'WAIT')
        self.assertEqual(response['support_research']['calls'][-1]['status'], 'ERROR')

    def test_sell_exit_does_not_require_support_research_approval(self):
        self.context.portfolio_reader = lambda: dict(available_cash_cents=100000,
            capital_limit_twd=100000, positions=[dict(symbol='2330', qty=10)])
        self.candidate['max_sell_qty'] = 10
        self.decision['decisions'][0].update(decision='SELL', action_ratio=1, suggested_qty=10,
            suggested_price=99.9, strategy_id='NONE', target_price=None, invalidation_price=None, holding_days=None)
        async def runner(context, *args):
            self.read()
            context.get_market_data('2330')
            return deepcopy(self.decision), {}
        def approve(request):
            return json.dumps(dict(reviews=[dict(symbol='2330', approved=True, reason='測試退出')])), {}
        decision, response = analyze(self.context, '策略', runner=runner, reviewer=approve)
        self.assertEqual(response['validation'], 'VALID', response)
        self.assertEqual(decision['decisions'][0]['decision'], 'SELL')

    def test_extended_history_uses_verified_traded_amount_and_preserves_default_window(self):
        payload = dict(stat='OK', title='115年10月 2330 台積電',
            fields=['日期','成交股數','成交金額','開盤價','最高價','最低價','收盤價'],
            data=[['115/10/22','1000','777777','100','110','90','105']])
        bars = daily_history._parse_month(payload, '2330', date(2026,10,1), now().isoformat(), 'https://www.twse.com.tw/')
        self.assertEqual(bars[0]['turnover_twd'], 777777)
        with patch.object(daily_history, '_fetch_month', return_value=(bars,None)) as fetch:
            result = daily_history.load_many(['2330'], date(2026,10,27), max_bars=100, require_turnover=True)
        self.assertEqual(fetch.call_count, 8)
        self.assertEqual(result['2330']['bars'][0]['turnover_twd'], 777777)
        self.assertEqual(daily_history.MAX_BARS, 22)

    def test_support_close_history_includes_today_only_after_publication_cutoff(self):
        bars = [dict(date='2026-10-22', open=100, high=110, low=90, close=105,
                     volume=1000, complete=True, source='TWSE_DAILY'),
                dict(date='2026-10-27', open=105, high=115, low=100, close=110,
                     volume=1200, complete=True, source='TWSE_DAILY')]
        for hour, expected in [(15, '2026-10-22'), (16, '2026-10-27')]:
            with patch.object(daily_history, '_fetch_month', return_value=(bars, None)), \
                    patch.object(daily_history, 'now', return_value=datetime(2026,10,27,hour,tzinfo=TAIPEI)):
                result = daily_history.load_many(['2330'], date(2026,10,27), include_completed_today=True)
            self.assertEqual(result['2330']['status']['latest_date'], expected)


if __name__ == '__main__':
    unittest.main()
