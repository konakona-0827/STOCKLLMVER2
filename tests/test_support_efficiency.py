from copy import deepcopy
import json
import unittest

from support_scout.agents import make_packet
from support_scout.transport import model_packet,expand_packet
import test_support_trading_integration as support_tests


class SupportTransportTests(unittest.TestCase):
    def packet(self):
        entry=dict(quote=dict(ask=100,bid=99.9),trade_setups=[dict(entry=100,target=120,invalidation=95)])
        return make_packet(dict(as_of='2026-11-09T10:00:00+08:00',symbol='X',
            observation=dict(daily_bars=[dict(date=str(i),close=100+i,volume=1000) for i in range(22)]),
            entry_context=entry,evidence=[dict(id='X:ENTRY_CONTEXT',kind='ENTRY_CONTEXT',values=deepcopy(entry))],
            required_confirmation_evidence=['X:ENTRY_CONTEXT']))

    def test_transport_removes_duplication_without_changing_signed_evidence(self):
        packet=self.packet();before=deepcopy(packet);wire=model_packet(packet)
        self.assertEqual(packet,before);self.assertEqual(wire['packet_id'],packet['packet_id'])
        self.assertEqual(expand_packet(wire),packet)
        self.assertLess(len(json.dumps(wire,ensure_ascii=False,separators=(',',':'))),len(json.dumps(packet,ensure_ascii=False)))

    def test_missing_and_null_daily_fields_remain_distinct(self):
        packet=self.packet();packet['core']['observation']['daily_bars']=[dict(close=100),dict(close=101,volume=None)]
        self.assertEqual(expand_packet(model_packet(packet)),packet)

    def test_invalid_internal_reference_is_rejected(self):
        wire=model_packet(self.packet());wire['tasks']={'$ref':'#/not-present'}
        with self.assertRaisesRegex(ValueError,'INVALID_SUPPORT_REFERENCE'):expand_packet(wire)


class BatchedSupportTests(unittest.TestCase):
    def setUp(self):
        self.f=support_tests.SupportTradingIntegrationTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_one_parent_batch_keeps_two_concurrent_independent_reviews(self):
        decision,response,model=self.f.sdk_analyze(batch=True)
        self.assertEqual(response['validation'],'VALID',response)
        self.assertEqual(decision['decisions'][0]['decision'],'BUY')
        self.assertCountEqual(model.child_roles,['researcher','reviewer'])
        self.assertGreaterEqual(model.max_active_children,2)
        self.assertEqual(model.parent_calls,5)
        a,b=model.child_inputs
        self.assertEqual(a['packet_id'],b['packet_id'])
        self.assertNotIn('private_researcher_summary',json.dumps(b))
        self.assertEqual(len(response['support_research']['calls']),2)

    def test_repeated_same_snapshot_batch_reuses_valid_reviews_without_resampling(self):
        decision,response,model=self.f.sdk_analyze(batch=True,repeat_batch=True)
        self.assertEqual(response['validation'],'VALID',response)
        self.assertEqual(len(model.child_inputs),2)
        self.assertEqual(len(response['support_research']['cache_hits']),2)
        self.assertTrue(all(c['new_model_requests']==0 for c in response['support_research']['cache_hits']))

    def test_multiple_symbols_keep_separate_packet_pairs(self):
        other=deepcopy(self.f.candidate);other['symbol']='2891';self.f.context.candidates['2891']=other
        wait=deepcopy(self.f.decision['decisions'][0]);wait.update(symbol='2891',decision='WAIT',
            suggested_qty=0,suggested_price=None,strategy_id='NONE',target_price=None,invalidation_price=None,holding_days=None)
        self.f.decision['decisions'].append(wait)
        decision,response,model=self.f.sdk_analyze(batch=True,batch_symbols=['2330','2891'])
        self.assertEqual(response['validation'],'VALID',response)
        self.assertEqual(len(model.child_inputs),4)
        ids={}
        for packet in model.child_inputs:ids.setdefault(packet['core']['symbol'],set()).add(packet['packet_id'])
        self.assertEqual({s:len(v) for s,v in ids.items()},{'2330':1,'2891':1})
        self.assertNotEqual(ids['2330'],ids['2891'])

    def test_invalid_batch_does_not_call_any_specialist(self):
        decision,response,model=self.f.sdk_analyze(batch=True,batch_symbols=['2330','2330'])
        self.assertFalse(model.child_inputs)
        self.assertEqual(decision['decisions'][0]['decision'],'WAIT')

    def test_reject_is_not_bypassed_by_batch_or_cache(self):
        decision,response,model=self.f.sdk_analyze(batch=True,repeat_batch=True,researcher_reject=True)
        self.assertEqual(decision['decisions'][0]['decision'],'WAIT')
        self.assertEqual(len(model.child_inputs),2)

    def test_batch_quota_failure_stops_parent_and_cancels_other_review(self):
        decision,response,model=self.f.sdk_analyze(batch=True,child_quota=True)
        self.assertEqual(response['validation'],'ERROR')
        self.assertEqual(response['failure_kind'],'API_QUOTA_EXHAUSTED')
        self.assertEqual(decision['decisions'][0]['decision'],'WAIT')
        self.assertFalse(self.f.coordinator.accepted)

    def test_changed_account_does_not_reuse_old_review(self):
        self.f.accept_pair()
        self.assertIsNotNone(self.f.coordinator.cached_review('researcher','2330'))
        self.f.context.account['available_cash_cents']-=1
        self.assertIsNone(self.f.coordinator.cached_review('researcher','2330'))
