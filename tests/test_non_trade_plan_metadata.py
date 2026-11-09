from copy import deepcopy
from decimal import Decimal
import unittest

from test_opportunity_search import candidate,decision
from execution.auto_advice_executor import LiveExecutionConfig
from opportunity_engine import refresh,policy
from position_analyzer import validate


class NonTradeMetadataTests(unittest.TestCase):
    def setUp(self):
        account=dict(available_cash_cents=1000000,capital_limit_twd=10000,budget_status='READY')
        self.buy=refresh(candidate('BUY'),account,LiveExecutionConfig(minimum_commission_twd=Decimal(1)),policy())
        self.wait=deepcopy(self.buy);self.wait['symbol']='WAIT'

    def check(self,result):
        validate(result,[self.buy,self.wait],1000000,1,.01,capital_twd=10000,funding_fee_reserve_twd=20)

    def test_wait_explanatory_plan_does_not_discard_another_valid_buy(self):
        buy=decision(self.buy);wait=decision(self.wait)
        wait.update(decision='WAIT',suggested_qty=0,suggested_price=None,action_ratio=0)
        before=deepcopy(buy);result=dict(market_view='test',decisions=[buy,wait]);self.check(result)
        self.assertEqual(buy['decision'],'BUY')
        for key in ('suggested_price','suggested_qty','target_price','invalidation_price','holding_days'):
            self.assertEqual(buy[key],before[key])
        self.assertEqual(wait['decision'],'WAIT');self.assertEqual(wait['strategy_id'],'NONE')
        self.assertTrue(all(wait[k] is None for k in ('target_price','invalidation_price','holding_days')))

    def test_non_action_quantity_contradiction_still_fails_closed(self):
        wait=decision(self.wait);wait.update(decision='WAIT',suggested_qty=1,suggested_price=None,action_ratio=0)
        with self.assertRaisesRegex(ValueError,'NON_ACTION_QUANTITY'):
            self.check(dict(market_view='test',decisions=[decision(self.buy),wait]))
