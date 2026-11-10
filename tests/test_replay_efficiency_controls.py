from copy import deepcopy
import unittest

from operations.expanded_opportunity_replay import prior_candidate_pool,inference_manifest,retryable_infrastructure_result
from operations.revalidate_saved_decisions import revalidate
from test_opportunity_search import candidate,decision
from opportunity_engine import refresh,policy
from execution.auto_advice_executor import LiveExecutionConfig
import json


class ReplayEfficiencyControlTests(unittest.TestCase):
    def test_reuse_uses_original_full_competitive_pool(self):
        b=dict(groups=[dict(group_id='g',events=['e'],selection_pool=[dict(symbol='X'),dict(symbol='COMPETITOR')])],
            events=[dict(event_id='e',candidate=dict(symbol='X'))])
        pool=prior_candidate_pool(b,'g')
        self.assertEqual([r['symbol'] for r in pool],['X','COMPETITOR'])
        pool[0]['symbol']='changed';self.assertEqual(b['groups'][0]['selection_pool'][0]['symbol'],'X')

    def test_tests_and_readme_do_not_invalidate_inference_identity(self):
        files={'tests/test_example.py':'a','README.md':'b','config/execution_live.json':'c',
            'strategies/main.md':'d','skills/main/SKILL.md':'e','trading_agent.py':'f'}
        self.assertEqual(set(inference_manifest(files)),set(files)-{'tests/test_example.py','README.md'})

    def test_decision_format_error_is_not_a_reason_to_sample_another_answer(self):
        def r(error):return dict(response=dict(validation='ERROR',api=dict(error=error)))
        self.assertFalse(retryable_infrastructure_result(r('ValueError: NON_BUY_TRADE_PLAN')))
        self.assertTrue(retryable_infrastructure_result(r('RateLimitError: credit_balance_exhausted')))

    def source(self):
        account=dict(available_cash_cents=1000000,capital_limit_twd=10000,budget_status='READY')
        rules=LiveExecutionConfig(minimum_commission_twd=1)
        c=refresh(candidate(),account,rules,policy());buy=decision(c)
        record=dict(all_candidates=[c],selected_symbols=[c['symbol']],decision=dict(decisions=[deepcopy(buy)]),
            response=dict(validation='VALID',raw_response=json.dumps(dict(market_view='test',decisions=[buy]))))
        return account,rules,record

    def test_saved_nontrade_response_can_be_corrected_without_model_call(self):
        account,rules,record=self.source();raw=json.loads(record['response']['raw_response'])
        raw['decisions'][0].update(decision='WAIT',suggested_qty=0,suggested_price=None,action_ratio=0)
        record['response'].update(validation='ERROR',raw_response=json.dumps(raw));record['decision']=deepcopy(raw)
        before=deepcopy(record);result=revalidate(record,account,rules)
        self.assertEqual(result['status'],'VALID_SAVED_REPLY');self.assertEqual(result['api_calls'],0)
        self.assertEqual(result['decision']['decisions'][0]['decision'],'WAIT')
        self.assertEqual(record,before)

    def test_saved_pretrade_veto_cannot_be_turned_into_new_buy(self):
        account,rules,record=self.source();old=record['decision']['decisions'][0]
        old.update(decision='WAIT',suggested_qty=0,suggested_price=None,strategy_id='NONE',
            target_price=None,invalidation_price=None,holding_days=None,reason='original independent veto')
        result=revalidate(record,account,rules)
        self.assertEqual(result['status'],'VALID_SAVED_REPLY')
        self.assertEqual(result['decision']['decisions'][0]['decision'],'WAIT')
        self.assertEqual(result['preserved_vetoes'],[old['symbol']])
