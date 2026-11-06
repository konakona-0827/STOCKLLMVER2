from copy import deepcopy
from datetime import date,datetime,timedelta
import json
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import Mock,patch
import asyncio
from types import SimpleNamespace

from config import TAIPEI,CODE_ROOT
import market_discovery as m
from opportunity_engine import policy,allocation_limit,refresh
from execution.auto_advice_executor import LiveExecutionConfig
from decimal import Decimal
from test_opportunity_search import candidate


def fixtures():
    companies=[];daily=[]
    for i in range(50):
        symbol=str(4000+i)
        companies.append({'公司代號':symbol,'產業別':str(i%12),'出表日期':'1151103'})
        daily.append(dict(Date='1151103',Code=symbol,Name='研究'+symbol,TradeVolume='3000000',
            TradeValue='300000000',OpeningPrice='100',HighestPrice='110',LowestPrice='95',
            ClosingPrice=str(100+i/10),Change=str(i/10)))
    return daily,companies


def answer(packet,model,p,path):
    rows=packet['candidates']
    picks=[dict(symbol=r['symbol'],trigger_kind='BREAK_ABOVE_HIGH',rationale='優先核對回升延續與零股成交價差',
                counter_evidence_to_check='檢查完整日線及近端阻力，未通過成本與風險時持續觀察') for r in rows[:2]]
    usage=[dict(input_tokens=1400,output_tokens=250,requests=1,cached_input_tokens=0,
                cache_write_tokens=0,reasoning_tokens=10,response_id='test',role='market_scout')]
    return dict(summary='兩檔初步交易情境；尚無下單批准',selections=picks),usage


class MarketDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.daily,self.companies=fixtures();self.at=datetime(2026,11,4,10,5,tzinfo=TAIPEI)
        self.account=dict(capital_limit_twd=10000,available_cash_twd=7350,budget_status='READY')
        self.op=policy();self.op['allocation_capital_fractions']=dict(Early=.35,Confirmed=.45,Strong=.50)
        self.p=m.settings()
        self.fetch=Mock(side_effect=lambda url:self.daily if url==m.DAILY_URL else self.companies)

    def test_only_current_common_stocks_with_valid_prices_are_normalized(self):
        self.daily.append(dict(self.daily[0],Code='0050'))
        rows=m.normalize(self.daily,self.companies,date(2026,11,3))
        self.assertEqual(len(rows),50)
        self.daily[0]['Date']='1151104'
        with self.assertRaisesRegex(ValueError,'LATEST_COMPLETE'):m.normalize(self.daily,self.companies,date(2026,11,3))
        self.daily[0]['Date']='1151103';self.daily.append(self.daily[0])
        with self.assertRaisesRegex(ValueError,'DUPLICATE'):m.normalize(self.daily,self.companies,date(2026,11,3))

    def test_screen_covers_new_symbols_and_filters_cost_liquidity_and_disabled(self):
        rows=m.normalize(self.daily,self.companies,date(2026,11,3))
        rows[0]['close']=6000;rows[1]['turnover']=10
        screened,total=m.screen(rows,self.account,self.op,self.p,excluded=['4002'])
        self.assertEqual(total,47);self.assertEqual(len(screened),40)
        self.assertFalse({'4000','4001','4002'} & {r['symbol'] for r in screened})
        self.assertGreater(len({r['industry'] for r in screened}),6)

    def test_scout_runs_before_plan_filter_and_cached_watchlist_does_not_rebuy_research(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);run=root/'run';run.mkdir();runner=Mock(side_effect=answer)
            first=m.discover(root,run,self.account,self.op,at=self.at,runner=runner,fetcher=self.fetch)
            self.assertEqual(first['status'],'COMPLETE');self.assertTrue(first['symbols'])
            self.assertTrue(first['model_called']);self.assertEqual(first['market_count'],50)
            second=m.discover(root,run,self.account,self.op,at=self.at+timedelta(hours=1),runner=runner,fetcher=self.fetch)
            self.assertEqual(second['symbols'],first['symbols']);self.assertFalse(second['model_called'])
            self.assertEqual(runner.call_count,1);self.assertEqual(self.fetch.call_count,2)
            m.discover(root,run,self.account,self.op,at=self.at+timedelta(hours=3),runner=runner,fetcher=self.fetch)
            self.assertEqual(runner.call_count,2)
            # Successful unchanged packets remain cached rather than repeated.
            p={**self.p,'minimum_review_gap_seconds':3600}
            with patch('market_discovery.settings',return_value=p):
                third=m.discover(root,run,self.account,self.op,at=self.at+timedelta(hours=3,minutes=5),runner=runner,fetcher=self.fetch)
            self.assertFalse(third['model_called'])

    def test_failure_is_accounted_and_next_scan_can_retry_without_daily_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);run=root/'run';run.mkdir();runner=Mock(side_effect=TimeoutError('test'))
            first=m.discover(root,run,self.account,self.op,at=self.at,runner=runner,fetcher=self.fetch)
            self.assertEqual(first['status'],'UNAVAILABLE');self.assertFalse(first['usage_accounting_complete'])
            second=m.discover(root,run,self.account,self.op,at=self.at+timedelta(hours=1),runner=runner,fetcher=self.fetch)
            self.assertTrue(second['model_called']);self.assertEqual(runner.call_count,2)
            self.assertEqual(second['status'],'UNAVAILABLE')

    def test_daily_research_can_exceed_two_calls_when_new_reviews_are_due(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);run=root/'run';run.mkdir();runner=Mock(side_effect=answer)
            p={**self.p,'minimum_review_gap_seconds':3600}
            with patch('market_discovery.settings',return_value=p):
                for offset in range(4):
                    result=m.discover(root,run,self.account,self.op,at=self.at+timedelta(hours=offset),runner=runner,fetcher=self.fetch)
                    self.assertTrue(result['model_called'])
            self.assertEqual(runner.call_count,4)

    def test_invalid_selection_cannot_inject_an_unseen_symbol_and_usage_is_kept(self):
        def bad(*args):
            result,usage=answer(*args);result['selections'][0]['symbol']='9999';return result,usage
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);run=root/'run';run.mkdir()
            result=m.discover(root,run,self.account,self.op,at=self.at,runner=bad,fetcher=self.fetch)
            self.assertEqual(result['status'],'UNAVAILABLE');self.assertEqual(result['symbols'],[])
            self.assertEqual(result['usage_requests'][0]['input_tokens'],1400)
            response=dict(validation='NOT_CALLED',api={})
            m.merge_usage(response,run,result)
            self.assertEqual(response['api']['usage']['input_tokens'],1400)
            self.assertTrue((run/'agent_response.json').exists())

    def test_selection_limits_industry_and_preserves_observed_trigger_provenance(self):
        rows=m.normalize(self.daily,self.companies,date(2026,11,3))
        packet=dict(candidates=rows);a,_=answer(packet,None,self.p,None)
        watch=m.validate_selection(a,rows,self.p,'2026-11-04')
        self.assertEqual(watch[0]['condition_price'],110)
        self.assertEqual(watch[0]['condition_source_date'],'2026-11-03')
        a['selections'].append(deepcopy(a['selections'][0]))
        with self.assertRaisesRegex(ValueError,'INVALID_SCOUT_SELECTION'):m.validate_selection(a,rows,self.p,'2026-11-04')
        a['selections']=[dict(a['selections'][0],symbol=str(4000+i*12)) for i in range(3)]
        with self.assertRaisesRegex(ValueError,'INDUSTRY_LIMIT'):m.validate_selection(a,rows,self.p,'2026-11-04')

    def test_detailed_research_is_not_rejected_by_old_output_character_limits(self):
        rows=m.normalize(self.daily,self.companies,date(2026,11,3));a,_=answer(dict(candidates=rows),None,self.p,None)
        a['summary']='完整研究依據與待確認風險。'*200
        a['selections'][0]['rationale']='觀察價格結構並確認反證。'*100
        result=m.validate_selection(a,rows,self.p,'2026-11-04')
        self.assertEqual(result[0]['rationale'],a['selections'][0]['rationale'])

    def test_no_model_with_unverified_budget_stop_or_outside_session(self):
        runner=Mock()
        with tempfile.TemporaryDirectory() as tmp:
            for account,at,stop in [({**self.account,'budget_status':'BLOCKED'},self.at,False),
                (self.account,self.at.replace(hour=9,minute=5),False),(self.account,self.at,True)]:
                result=m.discover(tmp,tmp,account,self.op,at=at,stopped=lambda:stop,runner=runner,fetcher=self.fetch)
                self.assertFalse(result['model_called'])
        runner.assert_not_called();self.fetch.assert_not_called()

    def test_watch_touch_requires_live_quote_and_is_never_order_approval(self):
        rows=m.normalize(self.daily,self.companies,date(2026,11,3));a,_=answer(dict(candidates=rows),None,self.p,None)
        watch=m.validate_selection(a,rows,self.p,'2026-11-04')
        quotes=[dict(symbol=watch[0]['symbol'],quote_status='LIVE',is_trial=False,last_price=111,
                     quote_timestamp=self.at.isoformat())]
        result=m.watch_status(watch,quotes)
        self.assertEqual(result[0]['status'],'RESEARCH_CONDITION_MET')
        self.assertEqual(result[0]['purpose'],'RESEARCH_TRIGGER_NOT_ORDER')
        quotes[0]['is_trial']=True
        self.assertEqual(m.watch_status(watch,quotes)[0]['status'],'NO_LIVE_QUOTE')

    def test_expanded_allocation_still_enforces_cash_and_combined_stop_risk(self):
        c=candidate();account=dict(capital_limit_twd=10000,available_cash_cents=735000,available_cash_twd=7350)
        self.assertEqual(allocation_limit(c,account,self.op,'Early'),3500)
        self.assertEqual(allocation_limit(c,account,self.op,'Confirmed'),4500)
        self.assertEqual(allocation_limit(c,account,self.op,'Strong'),5000)
        rules=LiveExecutionConfig(minimum_commission_twd=Decimal(1))
        result=refresh(c,account,rules,self.op)
        self.assertTrue(result['trade_setups'])
        self.assertTrue(all(s['combined_modeled_risk_twd']<=50 for s in result['trade_setups']))
        c['paper_position']['qty']=20
        self.assertEqual(allocation_limit(c,account,self.op,'Early'),1000)

    def test_usage_includes_scout_once_without_losing_existing_specialist_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            response=dict(api=dict(usage=dict(input_tokens=9000,output_tokens=900,requests=3),usage_requests=[]))
            _,rows=answer(dict(candidates=m.normalize(self.daily,self.companies,date(2026,11,3))),None,self.p,None)
            m.merge_usage(response,tmp,dict(model_called=True,usage_requests=rows,usage_accounting_complete=True))
            self.assertEqual(response['api']['usage']['input_tokens'],10400)
            self.assertEqual(response['api']['usage']['requests'],4)

    def test_daily_audit_counts_scout_after_later_pipeline_crash_and_skips_cache_only_run(self):
        from operations.audit_agent_usage import audit
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'config').mkdir()
            shutil.copyfile(CODE_ROOT/'config/execution_live.json',root/'config/execution_live.json')
            run=root/'data/analysis/runs/scout-only';run.mkdir(parents=True)
            (run/'run.json').write_text(json.dumps(dict(started_at=self.at.isoformat())),encoding='utf-8')
            (run/'market_discovery.json').write_text(json.dumps(dict(model_called=True,usage_accounting_complete=True)),encoding='utf-8')
            (run/'market_scout_usage_requests.json').write_text(json.dumps([dict(input_tokens=1400,output_tokens=250)]),encoding='utf-8')
            cached=run.parent/'cached-only';cached.mkdir()
            (cached/'run.json').write_text(json.dumps(dict(started_at=self.at.isoformat())),encoding='utf-8')
            (cached/'market_discovery.json').write_text(json.dumps(dict(model_called=False)),encoding='utf-8')
            result=audit(root,'2026-11-04')
            self.assertEqual(result['input_tokens'],1400);self.assertEqual(result['output_tokens'],250)
            self.assertEqual(result['missing_usage_runs'],[]);self.assertEqual(len(result['runs']),1)

    def test_usage_audit_does_not_double_count_merged_scout_requests(self):
        from operations.audit_agent_usage import audit
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'config').mkdir()
            shutil.copyfile(CODE_ROOT/'config/execution_live.json',root/'config/execution_live.json')
            run=root/'data/analysis/runs/merged';run.mkdir(parents=True)
            (run/'run.json').write_text(json.dumps(dict(started_at=self.at.isoformat())),encoding='utf-8')
            (run/'market_discovery.json').write_text(json.dumps(dict(model_called=True,usage_accounting_complete=True,
                usage_requests=[dict(input_tokens=1400,output_tokens=250)])),encoding='utf-8')
            (run/'agent_response.json').write_text(json.dumps(dict(api=dict(usage=dict(input_tokens=10400,output_tokens=1150),
                market_scout_usage=dict(input_tokens=1400,output_tokens=250)))),encoding='utf-8')
            result=audit(root,'2026-11-04')
            self.assertEqual(result['input_tokens'],10400);self.assertEqual(result['output_tokens'],1150)

    def test_installed_sdk_accepts_scout_schema_and_preserves_per_request_usage(self):
        from agents.usage import Usage
        from openai import AsyncOpenAI
        rows=m.normalize(self.daily,self.companies,date(2026,11,3));packet=dict(candidates=rows)
        async def fake_run(agent,payload,**kwargs):
            self.assertEqual(agent.tools,[]);self.assertIsNone(kwargs['max_turns'])
            self.assertIsNone(agent.model_settings.max_tokens)
            data=json.loads(payload);result,_=answer(data,None,self.p,None)
            return SimpleNamespace(final_output=agent.output_type.model_validate(result),
                raw_responses=[SimpleNamespace(usage=Usage(requests=1,input_tokens=1400,output_tokens=250),response_id='test-sdk')])
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'usage.json'
            client=AsyncOpenAI(api_key='test-no-network')
            with patch('openai.AsyncOpenAI',return_value=client),patch('agents.Runner.run',side_effect=fake_run):
                result,usage=asyncio.run(m._sdk_scout(packet,'gpt-6-sol',self.p,path))
            self.assertTrue(result['selections']);self.assertEqual(usage[0]['input_tokens'],1400)
            self.assertTrue(path.exists())

    def test_real_sdk_truncated_response_finishes_without_replaying_tools_and_counts_both_calls(self):
        try:import httpx2 as httpx
        except ImportError:import httpx
        from openai import AsyncOpenAI
        seen=[]
        def handler(request):
            seen.append(request)
            if len(seen)==1:return httpx.Response(200,json=dict(id='resp-truncated',object='response',created_at=1,
                model='gpt-6-sol',status='incomplete',output=[],incomplete_details=dict(reason='max_output_tokens'),
                max_output_tokens=3200,
                usage=dict(input_tokens=3919,output_tokens=1800,total_tokens=5719,
                    input_tokens_details=dict(cached_tokens=0),output_tokens_details=dict(reasoning_tokens=1800))))
            self.assertEqual(json.loads(request.content)['max_output_tokens'],6400)
            self.assertEqual(json.loads(request.content)['input'],json.loads(seen[0].content)['input'])
            rows=m.normalize(self.daily,self.companies,date(2026,11,3))
            screened,_=m.screen(rows,self.account,self.op,self.p)
            result,_=answer(dict(candidates=screened),None,self.p,None)
            return httpx.Response(200,json=dict(id='resp-complete',object='response',created_at=2,
                model='gpt-6-sol',status='completed',output=[dict(type='message',id='msg',role='assistant',status='completed',
                    content=[dict(type='output_text',text=json.dumps(result,ensure_ascii=False),annotations=[])])],
                usage=dict(input_tokens=3919,output_tokens=700,total_tokens=4619,
                    input_tokens_details=dict(cached_tokens=0),output_tokens_details=dict(reasoning_tokens=0))))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);run=root/'run';run.mkdir()
            client=AsyncOpenAI(api_key='test-no-network',base_url='https://unit.test/v1',
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            with patch('openai.AsyncOpenAI',return_value=client):
                result=m.discover(root,run,self.account,self.op,at=self.at,fetcher=self.fetch)
            self.assertEqual(len(seen),2);self.assertEqual(result['status'],'COMPLETE',result)
            self.assertTrue(result['usage_accounting_complete'])
            self.assertEqual(result['usage_requests'][0]['input_tokens'],3919)
            self.assertEqual(result['usage_requests'][0]['output_tokens'],1800)
            self.assertEqual(result['usage_requests'][0]['response_status'],'incomplete')
            self.assertEqual(len(result['usage_requests']),2)
            self.assertNotIn('max_output_tokens',json.loads(seen[0].content))

    def test_rejected_entry_has_actionable_economic_diagnostics_without_a_trade(self):
        c=candidate();op=deepcopy(self.op);op['minimum_target_net_profit_twd']=10000
        result=refresh(c,dict(self.account,available_cash_cents=735000),
            LiveExecutionConfig(minimum_commission_twd=Decimal(1)),op)
        self.assertEqual(result['trade_setups'],[])
        self.assertIn('TARGET_NET_PROFIT_BELOW_RESEARCH_HURDLE',result['opportunity_status']['rejection_reasons'])
        self.assertTrue(result['opportunity_status']['near_misses'])
        self.assertTrue(all(r['purpose']=='DIAGNOSTIC_ONLY_NOT_ORDER_APPROVAL'
                            for r in result['opportunity_status']['near_misses']))

    def test_off_hours_health_defers_idle_checks_but_retains_pending_reconciliation(self):
        from dashboard import Dashboard
        from scheduler import is_health_check_window
        at=self.at.replace(hour=20)
        dashboard=SimpleNamespace(last_health={})
        with patch('dashboard.time.time',return_value=at.timestamp()):
            delay=Dashboard.health_delay(dashboard)
            self.assertGreater(delay,3600)
            self.assertTrue(is_health_check_window(at.timestamp()+delay))
            dashboard.last_health=dict(unresolved_attempts=[dict(symbol='1101')])
            self.assertEqual(Dashboard.health_delay(dashboard),60)


if __name__=='__main__':unittest.main()
