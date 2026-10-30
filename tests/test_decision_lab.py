"""Fabricated fixtures validate isolation, scoring, and replay; no network/mail."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from support_scout.contracts import DataError, digest
from support_scout.decision_lab import build_prefix, direction_diagnostics, load_plan, profile_settings, replay_lab, run_lab, summarize
from support_scout.communication import load_policy as load_wire
from support_scout.discovery import SearchCompletion
from support_scout.japan_demo import demo_bundle
from support_scout.opportunity import load_policy
from support_scout.patterns import load_registry
from support_scout.providers import Completion


def fixture():
    b=demo_bundle();b['synthetic']=False;b['observation_only']=True
    b['securities']=b['securities'][:1]
    for raw in b['securities']:
        raw['observation_only']=True
        raw['yahoo_metadata']={'regularMarketPrice':999999}
        raw['dossier']={'future_fact':'UNAVAILABLE_FUTURE'}
        raw['yahoo_events']=[{'session':'2099-01-01','type':'future'}]
    p=load_plan();p.update(symbols=[b['securities'][0]['symbol']],sessions=[b['securities'][0]['bars'][-7]['session']],
        max_selected=1,max_total_calls=3,numeric_lookback_sessions=10)
    p['profiles']=p['profiles'][:1]
    return b,p


class SearchFixture:
    mode,model='test_transport','fabricated-search'
    def complete(self,packet):
        cs=packet['core']['candidates'][:1]
        return SearchCompletion({'selections':[{'candidate_id':c['candidate_id'],'evidence_ids':c['required_evidence_ids'],'rationale':'人造測試研究，非投資建議。'} for c in cs]})


class ReviewFixture:
    mode,model='test_transport','fabricated-review'
    def complete(self,packet,role):
        core=packet['core']
        plans=core.get('opportunity_reference_plans',{})
        pid=next((p for p in core['opportunity_policy']['pattern_ids'] if plans.get(p)),core['opportunity_policy']['pattern_ids'][0])
        return Completion({'verdict':'WATCH','evidence_ids':[e['id'] for e in core['evidence']],
            'summary':'人造資料的受控測試。','risks':['沒有真實市場意涵'],'missing_data':['來源未核實']},
            usage={'total_tokens':10},opportunity_view={'action':'WAIT','pattern_id':pid,'reason':'人造資料待核對，不強制買入。'})


class LabTests(unittest.TestCase):
    def test_prefix_has_no_future_prices_quotes_dossier_events_or_account_state(self):
        b,p=fixture();day=p['sessions'][0]
        b['portfolio_snapshot']={'secret_position':123};b['sizing_policy']={'capital':999}
        b['discovery']={'future_decision':'BUY'}
        v=build_prefix(b,p['symbols'],day,registry=load_registry(),policy=load_policy(),communication=load_wire(),evaluated_at=b['as_of'],study_id=p['study_id'])
        self.assertTrue(all(r['session']<=day for r in v['securities'][0]['bars']))
        self.assertNotIn('yahoo_metadata',v['securities'][0]);self.assertEqual(v['securities'][0]['dossier'],{})
        self.assertFalse(v['securities'][0]['yahoo_events'])
        for key in ['portfolio_snapshot','sizing_policy','discovery','public_information_policy']:self.assertNotIn(key,v)
        self.assertEqual(v['securities'][0]['bars'][-1]['available_at'],b['securities'][0]['bars'][-7]['available_at'])

    def test_changing_future_prices_cannot_change_any_prefix_input(self):
        b,p=fixture();kwargs=dict(registry=load_registry(),policy=load_policy(),communication=load_wire(),evaluated_at=b['as_of'],study_id=p['study_id'])
        before=build_prefix(b,p['symbols'],p['sessions'][0],**kwargs)
        for r in b['securities'][0]['bars'][-6:]:r.update(close=9000,high=9999,low=1)
        b['securities'][0]['yahoo_metadata']['regularMarketPrice']=1
        self.assertEqual(before,build_prefix(b,p['symbols'],p['sessions'][0],**kwargs))

    def test_profiles_do_not_mutate_production_parameters_or_ohlcv(self):
        b,p=fixture();reg,policy=load_registry(),load_policy();original=deepcopy((reg,policy,b))
        profile={'id':'wide','label':'test','short_parameters':{'stop_price_fraction':.03},'minimum_strategy_rr':.5,'agent_sessions':[]}
        r,q=profile_settings(reg,policy,profile)
        self.assertEqual(q['short_horizon']['minimum_strategy_rr'],.5)
        self.assertTrue(all(d['parameters']['stop_price_fraction']==.03 for d in r['definitions'] if d['handler']=='short_cycle'))
        self.assertEqual((reg,policy,b),original)

    def test_small_budget_refuses_before_any_provider_call(self):
        b,p=fixture();p['max_total_calls']=2
        with tempfile.TemporaryDirectory() as d,self.assertRaisesRegex(DataError,'CALL_BUDGET'):
            run_lab(b,plan=p,agents=True,providers=(SearchFixture(),ReviewFixture(),ReviewFixture()),output_root=d)
        self.assertFalse(Path(d).exists())

    def test_no_agents_has_no_model_success_rate_and_replay_is_offline(self):
        b,p=fixture()
        with tempfile.TemporaryDirectory() as d:
            r,out=run_lab(b,plan=p,output_root=d)
            self.assertEqual(r['summary'],[]);self.assertEqual(r['model_call_count'],0)
            self.assertEqual(replay_lab(out)['status'],'MATCH')
            source=json.loads((out/'source.json').read_text(encoding='utf-8'))
            source['securities'][0]['bars'][-1]['close']+=1
            (out/'source.json').write_text(json.dumps(source),encoding='utf-8')
            with self.assertRaisesRegex(DataError,'INTEGRITY'):replay_lab(out)

    def test_provider_decisions_are_saved_and_replayed_without_mail_or_positions(self):
        b,p=fixture()
        with tempfile.TemporaryDirectory() as d:
            r,out=run_lab(b,plan=p,agents=True,providers=(SearchFixture(),ReviewFixture(),ReviewFixture()),output_root=d)
            self.assertLessEqual(r['model_call_count'],3)
            self.assertEqual(replay_lab(out)['status'],'MATCH')
            self.assertFalse(r['execution_enabled'])
            self.assertTrue((out/'frozen.json').exists())

    def test_proposals_unfilled_and_ambiguous_have_distinct_denominators(self):
        rows=[]
        for action,status,gross,amb in [('PROBE_BUY_REVIEW','TARGET',.02,False),('PROBE_BUY_REVIEW','AMBIGUOUS_STOP_FIRST',-.015,True),
                ('PROBE_BUY_REVIEW','NOT_FILLED_GAP_ABOVE_ENTRY_BAND',None,False),('WAIT','NO_AI_BUY_PROPOSAL',None,False)]:
            rows.append({'profile_id':'baseline','month':'2026-10','selected_by_search':True,'action':action,
                'outcome':{'holding_sessions':3,'status':status,'gross_return':gross,'ambiguous':amb},'primary_comparator':{'status':'TARGET'}})
        s=next(x for x in summarize(rows) if x['month']=='ALL')
        self.assertEqual((s['buy_proposals'],s['resolved_simulated_entries'],s['targets'],s['ambiguous_stop_first']),(3,2,1,1))
        self.assertEqual(s['target_rate_stop_first'],.5);self.assertEqual(s['buy_proposal_rate'],.75)
        self.assertEqual(s['missed_frozen_primary_targets'],1)

    def test_price_touch_after_stop_is_not_trade_success_or_assumed_fill(self):
        b,p=fixture();symbol=p['symbols'][0];session=p['sessions'][0]
        future=[r for r in b['securities'][0]['bars'] if r['session']>session]
        for r in future:r.update(open=100,high=103,low=95,close=101)
        row={'profile_id':'baseline','month':session[:7],'session':session,'symbol':symbol,'research_status':'REVIEWED',
            'action':'PROBE_BUY_REVIEW','outcome':{'holding_sessions':3,'status':'STOP','gross_return':-.015,'ambiguous':False},
            'primary_comparator':{'status':'STOP','gross_return':-.015}}
        value=next(x for x in direction_diagnostics([row],b) if x['month']=='ALL')
        self.assertEqual(value['price_touch_rate_ignore_execution'],1)
        self.assertEqual(value['rule_primary_target_rate'],0)
        self.assertEqual(row['outcome']['status'],'STOP')

    def test_incomplete_price_window_without_hit_is_unknown(self):
        b,p=fixture();symbol=p['symbols'][0];session=b['securities'][0]['bars'][-2]['session']
        b['securities'][0]['bars'][-1].update(open=100,high=101,low=99,close=100)
        row={'profile_id':'baseline','month':session[:7],'session':session,'symbol':symbol,'research_status':'REVIEWED',
            'action':'PROBE_BUY_REVIEW','outcome':{'holding_sessions':5},'primary_comparator':{'gross_return':None}}
        v=next(x for x in direction_diagnostics([row],b) if x['month']=='ALL')
        self.assertEqual(v['unknown_price_windows'],1);self.assertEqual(v['observable_price_windows'],0)
        self.assertIsNone(v['price_touch_rate_ignore_execution'])


if __name__=='__main__':unittest.main()
