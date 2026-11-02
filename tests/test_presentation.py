from copy import deepcopy
import json
import unittest
from urllib.request import Request,urlopen

from support_scout.presentation import ranked_securities, stock_status, report_status, runtime_status
import tests.test_internal_scheduler as scheduler_tests


def stock(symbol, action='WAIT', **kwargs):
    return {'symbol':symbol,'data_status':'OBSERVATION','technical':{'priority_score':1},
        'opportunity_decision':{'action':action},**kwargs}


class PresentationTests(unittest.TestCase):
    def test_effective_operations_then_search_order_then_candidates_and_avoid(self):
        buy=stock('BUY', 'PROBE_BUY_REVIEW',execution_plan={'suggested_amount_jpy':1000,
            'entry_reference':100,'invalidation_reference':98,'target_reference':102})
        leave=stock('EXIT',manual_action={'action':'EXIT_REVIEW','position':{'quantity':1}})
        candidate=stock('CAND',opportunity_decision={'action':'WAIT','candidate':True},technical={'priority_score':999})
        avoid=stock('AVOID','AVOID',technical={'priority_score':9999})
        a,b=stock('A'),stock('B')
        report={'securities':[avoid,candidate,b,buy,a,leave],'discovery':{'search':{'status':'COMPLETED','selection':[
            {'symbol':'A','agent_order':2},{'symbol':'B','agent_order':1},{'symbol':'BUY','agent_order':3}]}},
            'portfolio_snapshot':{'positions':{'EXIT':{'quantity':1}}}}
        before=deepcopy(report)
        self.assertEqual([s['symbol'] for s in ranked_securities(report)],['EXIT','BUY','B','A','CAND','AVOID'])
        self.assertEqual(report,before)

    def test_zero_size_or_unconfirmed_position_cannot_be_promoted_to_operation(self):
        fake_buy=stock('ZERO','PROBE_BUY_REVIEW',execution_plan={'suggested_amount_jpy':0,
            'entry_reference':100,'invalidation_reference':98,'target_reference':102})
        fake_exit=stock('EMPTY',manual_action={'action':'EXIT_REVIEW','position':{'quantity':0}})
        summary=report_status({'securities':[fake_buy,fake_exit]})
        self.assertIn('沒有有效交易提案',summary['title'])

    def test_search_failure_not_described_as_completed_research_or_low_potential(self):
        s=stock('A',opportunity_decision={'action':'WAIT','candidate':True})
        report={'securities':[s],'discovery':{'search':{'status':'FAILED','selection':[]}},'desk':{'error_count':2}}
        self.assertIn('本輪尚未深入研究',stock_status(s,report))
        summary=report_status(report)
        self.assertIn('未通過檢查',summary['text']);self.assertIn('2 筆',summary['text'])
        self.assertNotIn('FAILED',json.dumps(summary,ensure_ascii=False))

    def test_closed_market_waiting_status_includes_plain_next_time(self):
        value=runtime_status({'enabled':True,'worker_alive':True,'market_open':False,'running':False,
            'next_run_jst':'2026-11-04T09:00:00+09:00'},None)
        self.assertIn('休市或午休',value['title']);self.assertIn('11/04 09:00',value['title'])
        self.assertNotIn('IDLE',value['analyze_text'])


class RetireDashboardTests(unittest.TestCase):
    setUp=scheduler_tests.SchedulerDashboardTests.setUp
    tearDown=scheduler_tests.SchedulerDashboardTests.tearDown
    def test_retire_is_csrf_protected_and_refuses_an_inflight_analysis(self):
        self.server.analysis={'status':'RUNNING'}
        from urllib.error import HTTPError
        req=Request(self.base+'/api/retire',data=b'{}',headers={'Origin':self.base,'Content-Type':'application/json','X-Desk-Token':self.server.token})
        with self.assertRaises(HTTPError) as e:urlopen(req)
        self.assertEqual(e.exception.code,409)
        self.server.analysis={'status':'IDLE'}
        with urlopen(req) as r:self.assertTrue(json.load(r)['retiring'])


if __name__=='__main__':unittest.main()
