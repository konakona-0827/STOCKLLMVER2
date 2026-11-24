"""Source-empty versus missing system delivery, including original SDK adapters."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock,patch

from capital import normalize_stock
from capital_multi_quote import MultiCapitalMarket
from execution.broker_runtime import BrokerRuntime
from opportunity_flow.common import policy,read,stamp
from opportunity_flow.data import load_dataset,merge_research_quotes,validate_quote
from opportunity_flow.demo import create
from opportunity_flow.engine import build
from opportunity_flow.intraday import IntradayEvidence,availability,decode_tick,material_history,quote_trace
from opportunity_flow.legacy_runtime import LegacyRuntime
from opportunity_flow.pipeline import run_cycle
from opportunity_flow.candidate_explanation import research_card
from opportunity_flow.company_events import current_events,for_symbol
from test_opportunity_flow import FakeRunner

AT=stamp('2026-11-24T10:05:30+08:00')


def stock():
    return SimpleNamespace(bstrStockNo='1111',bstrStockName='isolated',sDecimal=2,
        nClose=10000,nBid=9990,nAsk=10000,nRef=9900,nOpen=9900,nHigh=10100,nLow=9800,
        nTQty=10000,nBc=100,nAc=100,nTradingDay=20261124,nDealTime=100530,nTradingLotFlag=1,nSimulate=0)


def tick(at=AT,ptr=1,price=100,quantity=2,source='HISTORY_TICK'):
    return dict(exchange_time=at.isoformat(),ptr=ptr,microseconds=0,price=price,quantity=quantity,source=source)


def quote(at=AT):
    return dict(symbol='1111',exchange='TWSE',quote_status='LIVE',is_trial=False,quote_timestamp=at.isoformat(),
        last_price=100,bid=99.9,ask=100,low=98,high=101,quote_basis='capital_intraday_odd_lot')


class SourceTraceTests(unittest.TestCase):
    def test_empty_sdk_ask_is_provider_evidence_not_proof_of_market_absence(self):
        s=stock();s.nAsk=s.nAc=0;raw=normalize_stock(s,AT)
        q={**quote(), 'ask':None}
        trace=quote_trace(q,raw)
        self.assertEqual(trace['fields']['ask']['origin'],'PROVIDER_EMPTY_FIELD')
        self.assertEqual(trace['fields']['ask']['sdk_value'],0)
        self.assertFalse(trace['fields']['ask']['market_absence_confirmed'])
        self.assertEqual(validate_quote(q,'1111','TWSE',AT,120)[1],'ODDLOT_ASK_MISSING')

    def test_source_positive_but_delivery_missing_is_a_system_defect(self):
        trace=quote_trace({**quote(),'ask':None},normalize_stock(stock(),AT))
        self.assertEqual(trace['fields']['ask']['origin'],'SYSTEM_TRANSFORM_LOSS')

    def test_invalid_quote_identity_or_time_is_not_called_mapping_loss(self):
        trace=quote_trace(dict(quote_status='UNAVAILABLE'),normalize_stock(stock(),AT))
        self.assertEqual(trace['fields']['ask']['origin'],'SOURCE_VALIDATION_REJECTED')
        self.assertEqual(quote_trace({},None,'TimeoutError:read deadline')['fields']['ask']['origin'],
            'SOURCE_REQUEST_OR_VALIDATION_ERROR')

    def test_no_raw_trace_does_not_claim_provider_or_market_has_no_information(self):
        self.assertEqual(quote_trace({},dict(symbol='1111'))['fields']['ask']['origin'],'SOURCE_TRACE_UNAVAILABLE')
        self.assertEqual(quote_trace({})['fields']['ask']['origin'],'SOURCE_NO_RESPONSE')

    def test_tick_identity_trial_date_and_decimal_scale(self):
        event=('HISTORY_TICK',5,7,42,20261124,100401,123,9990,10000,10000,3,0)
        row=decode_tick(event,stock(),'1111',5,AT)
        self.assertEqual((row['price'],row['quantity']),(100,3))
        for changed in [event[:-1]+(1,),event[:4]+(20261125,)+event[5:]]:
            with self.assertRaises(ValueError):decode_tick(changed,stock(),'1111',5,AT)
        with self.assertRaises(ValueError):decode_tick(event,stock(),'2222',5,AT)
        with self.assertRaises(ValueError):decode_tick(event,stock(),'1111',6,AT)

    def test_cached_ticks_do_not_hide_new_source_or_system_failure(self):
        history=dict(tick_count=3,history_status='PROVIDER_REQUEST_REJECTED:3027')
        self.assertEqual(availability(None,[],None,history)['intraday_history']['origin'],'SOURCE_RESPONSE_INCOMPLETE')
        history['history_status']='RESPONSE_DROPPED_OR_INVALID'
        self.assertEqual(availability(None,[],None,history)['intraday_history']['origin'],'SYSTEM_RECEIVE_OR_DECODE_ERROR')


class HistoryPersistenceTests(unittest.TestCase):
    def test_real_received_ticks_aggregate_and_deduplicate_without_filling_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache=IntradayEvidence(Path(tmp)/'history.json')
            a=tick(AT.replace(minute=1,second=1),1,100,2)
            b=tick(AT.replace(minute=1,second=30),2,101,3)
            c=tick(AT.replace(minute=3,second=1),3,99,4)
            forming=tick(AT.replace(second=1),4,102,5)
            q={**quote(),'intraday_ticks':[a,b,c,forming,{**a,'source':'LIVE_TICK'}],
                'tick_history_status':'RECEIVED_TICKS_PARTIAL_COVERAGE'}
            cache.record([q],AT)
            restored=IntradayEvidence(cache.path).evidence('1111',AT)
            self.assertEqual(restored['tick_count'],4)
            rows=restored['minute_bars']['rows']
            self.assertEqual(len(rows),2);self.assertEqual(rows[0][1:],[100,101,100,101,5,2])
            self.assertNotIn(AT.replace(minute=2,second=0).isoformat(),[r[0] for r in rows])
            self.assertFalse(restored['continuous_coverage_verified'])

    def test_wrong_day_future_and_nontrade_samples_never_become_current_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache=IntradayEvidence(Path(tmp)/'history.json')
            cache.record([{**quote(),'intraday_ticks':[tick(AT-timedelta(days=1)),tick(AT+timedelta(seconds=1))]}],AT)
            self.assertEqual(cache.evidence('1111',AT)['tick_count'],0)
            cache.record([{**quote(),'is_trial':True}],AT)
            self.assertEqual(cache.evidence('1111',AT+timedelta(days=1))['observation_count'],0)

    def test_failed_persistence_keeps_in_memory_data_and_reports_storage_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache=IntradayEvidence(Path(tmp)/'history.json')
            with patch('opportunity_flow.intraday.save',side_effect=PermissionError('isolated')):
                cache.record([quote()],AT)
            evidence=cache.evidence('1111',AT)
            self.assertEqual(evidence['observation_count'],1)
            self.assertEqual(evidence['storage_error'],'STORAGE_WRITE_FAILED')

    def test_new_time_evidence_changes_research_key_but_duplicate_ticks_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache=IntradayEvidence(Path(tmp)/'history.json');levels=dict(support=99)
            empty=material_history(cache.evidence('1111',AT),levels,AT)
            q={**quote(),'intraday_ticks':[tick(AT.replace(minute=1),1,100)],'tick_history_status':'RECEIVED_TICKS_PARTIAL_COVERAGE'}
            cache.record([q],AT);first=material_history(cache.evidence('1111',AT),levels,AT)
            self.assertNotEqual(empty,first)
            cache.record([q],AT)
            self.assertEqual(material_history(cache.evidence('1111',AT),levels,AT),first)


class OriginalSDKHistoryTests(unittest.TestCase):
    def market(self,rc=0):
        market=MultiCapitalMarket.__new__(MultiCapitalMarket)
        market.subscribed=('1111',);market.pending_indices=set();market.callback_times={}
        market.stopped=lambda:False;market.event=Mock();market.flow_require_complete_buy=True
        sdk=Mock();sdk.SKQuoteLib_RequestTicksWithMarketNo.return_value=rc
        sdk.SKQuoteLib_GetStockByIndexLONG.return_value=(stock(),0)
        market.objects={'SKQuoteLib':sdk};market.sk=SimpleNamespace(SKSTOCKLONG=stock)
        clock=[0.];pumps=[]
        def pump(delay):
            clock[0]+=.2;pumps.append(delay);market.pending_indices.add((5,7))
            if len(pumps)==1:
                market.queue_intraday_tick('HISTORY_TICK',5,7,42,20261124,100401,0,9990,10000,10000,3,0)
                market.queue_intraday_tick('LIVE_TICK',5,7,43,20261124,100501,0,9990,10000,10000,3,1)
        market.pump=pump
        return market,sdk,clock,pumps

    def test_original_loop_subscribes_and_delivers_history_on_same_quote_owner(self):
        market,sdk,clock,pumps=self.market()
        with patch('capital_multi_quote.time.monotonic',side_effect=lambda:clock[0]),patch('capital_multi_quote.now',return_value=AT):
            rows,errors=market._fetch_one_batch(('1111',),timeout=12,batch_index=1,batch_count=1)
        sdk.SKQuoteLib_RequestTicksWithMarketNo.assert_called_once_with(0,5,'1111')
        self.assertEqual(rows['1111']['tick_history_status'],'RECEIVED_TICKS_PARTIAL_COVERAGE')
        self.assertEqual(len(rows['1111']['intraday_ticks']),1);self.assertFalse(errors)
        self.assertLess(clock[0],12);self.assertFalse(market.collect_intraday_ticks)

    def test_history_request_rejection_does_not_discard_quote_or_repeat_request(self):
        market,sdk,clock,pumps=self.market(rc=3027)
        with patch('capital_multi_quote.time.monotonic',side_effect=lambda:clock[0]),patch('capital_multi_quote.now',return_value=AT):
            rows,errors=market._fetch_one_batch(('1111',),timeout=12,batch_index=1,batch_count=1)
        self.assertEqual(rows['1111']['tick_history_status'],'PROVIDER_REQUEST_REJECTED:3027')
        self.assertEqual(rows['1111']['ask'],100);sdk.SKQuoteLib_RequestTicksWithMarketNo.assert_called_once()

    def test_history_after_fast_quote_is_waited_for_within_original_read_timeout(self):
        market,sdk,clock,pumps=self.market();emitted=[]
        def delayed(delay):
            clock[0]+=.2;market.pending_indices.add((5,7))
            if clock[0]>=3 and not emitted:
                emitted.append(True)
                market.queue_intraday_tick('HISTORY_TICK',5,7,42,20261124,100401,0,9990,10000,10000,3,0)
        market.pump=delayed
        with patch('capital_multi_quote.time.monotonic',side_effect=lambda:clock[0]),patch('capital_multi_quote.now',return_value=AT):
            rows,errors=market._fetch_one_batch(('1111',),timeout=12,batch_index=1,batch_count=1)
        self.assertEqual(len(rows['1111']['intraday_ticks']),1)
        self.assertGreaterEqual(clock[0],3);self.assertLess(clock[0],12)

    def test_sdk_documented_tick_pages_start_at_zero_and_stay_below_ten(self):
        market,sdk,clock,pumps=self.market();names=tuple(str(i) for i in range(1111,1121))
        market.subscribed=names
        with patch('capital_multi_quote.time.monotonic',side_effect=lambda:clock[0]),patch('capital_multi_quote.now',return_value=AT):
            market._fetch_one_batch(names,timeout=.4,batch_index=1,batch_count=1)
        self.assertEqual([c.args[0] for c in sdk.SKQuoteLib_RequestTicksWithMarketNo.call_args_list],list(range(10)))

    def test_tick_subscription_limit_is_respected_by_batching_and_deadline(self):
        market=MultiCapitalMarket.__new__(MultiCapitalMarket);market.flow_require_complete_buy=True
        market.event=Mock()
        market.connect=Mock();market.leave_monitor=Mock();market._enter_monitor=Mock()
        market._fetch_one_batch=Mock(return_value=({},{}))
        with patch('market_venue.oddlot_market',return_value=5):market.fetch_batch([str(i) for i in range(1000,1021)])
        self.assertEqual([len(c.args[0]) for c in market._fetch_one_batch.call_args_list],[10,10,1])
        self.assertEqual(BrokerRuntime._read_wait_seconds('quotes',(range(21),None,None,12,5,True)),81)


class ResearchDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.at=stamp(create(self.root/'dataset')).replace(hour=10,minute=5,second=30)
        self.ds=load_dataset(self.root/'dataset');self.p=policy()
        price=self.ds['grouped']['1111'][-1]['close']
        self.q={**quote(self.at),'last_price':price,'bid':price-.1,'ask':price,'low':price-1}

    def test_full_daily_history_and_stored_observations_are_in_both_model_requests(self):
        cache=IntradayEvidence(self.root/'history.json')
        cache.record([{**self.q,'intraday_ticks':[tick(self.at.replace(minute=1),1,self.q['ask'])],
            'tick_history_status':'RECEIVED_TICKS_PARTIAL_COVERAGE'}],self.at)
        self.q['intraday_evidence']=cache.evidence('1111',self.at)
        runner=FakeRunner()
        result=run_cycle(self.ds,self.at,self.p,self.root/'research',quotes={('TWSE','1111'):self.q},
            runner=runner,use_llm=True,review_symbols={'1111'})
        self.assertEqual(len(runner.calls),2)
        for packets,role in runner.calls:
            evidence={e['evidence_id']:e['facts'] for e in packets[0]['evidence']}
            self.assertEqual(len(evidence['E_DAILY_HISTORY']['rows']),60)
            self.assertEqual(evidence['E_INTRADAY']['history']['tick_count'],1)
            self.assertEqual(evidence['E_INTRADAY']['history']['observation_count'],1)
            self.assertEqual(evidence['E_DATA_LIMITS']['information_availability']['daily_history']['delivered_bars'],60)
        changed={**self.q,'intraday_evidence':{}}
        self.assertNotEqual(build('1111',self.ds,self.at,self.p,changed)['material_key'],
            build('1111',self.ds,self.at,self.p,self.q)['material_key'])

    def test_public_reference_keeps_source_gap_and_history_without_authorizing_buy(self):
        partial={**self.q,'ask':None,'information_trace':quote_trace({**self.q,'ask':None},
            dict(raw_fields=dict(nAsk=0))), 'intraday_evidence':dict(tick_count=3)}
        public={**self.q,'quote_status':'PUBLIC_REFERENCE','quote_basis':'TWSE_MIS_BOARD_LOT_NOT_ODDLOT'}
        merged=merge_research_quotes({('TWSE','1111'):public},{'1111':partial},self.at,120)[('TWSE','1111')]
        self.assertEqual(merged['information_trace']['fields']['ask']['origin'],'PROVIDER_EMPTY_FIELD')
        self.assertEqual(merged['intraday_evidence']['tick_count'],3)
        self.assertIsNone(validate_quote(merged,'1111','TWSE',self.at,120)[0])

    def test_new_minutes_automatically_research_after_watch_without_price_change(self):
        state=self.root/'recheck';runner=FakeRunner(verdict='WATCH')
        args=dict(quotes={('TWSE','1111'):self.q},runner=runner,use_llm=True,review_symbols={'1111'})
        run_cycle(self.ds,self.at,self.p,state,**args)
        self.assertEqual(len(runner.calls),2)
        cache=IntradayEvidence(self.root/'time.json')
        cache.record([{**self.q,'intraday_ticks':[tick(self.at.replace(minute=1),1,self.q['ask'])],
            'tick_history_status':'RECEIVED_TICKS_PARTIAL_COVERAGE'}],self.at)
        self.q['intraday_evidence']=cache.evidence('1111',self.at)
        run_cycle(self.ds,self.at,self.p,state,**args)
        self.assertEqual(len(runner.calls),4)
        run_cycle(self.ds,self.at,self.p,state,**args)
        self.assertEqual(len(runner.calls),4)

    def test_watch_request_for_missing_minutes_is_reported_as_system_gap_not_market_wait(self):
        class MinutesRunner(FakeRunner):
            def __call__(self,packets,role):
                value=super().__call__(packets,role)
                for row in value['answer']['opinions']:row['wait_conditions']=['需要5分鐘價格序列核對站穩。']
                return value
        result=run_cycle(self.ds,self.at,self.p,self.root/'watch',quotes={('TWSE','1111'):self.q},
            runner=MinutesRunner(verdict='WATCH'),use_llm=True,review_symbols={'1111'})
        c=next(c for c in result['candidates'] if c['symbol']=='1111')
        self.assertEqual(c['state'],'RESEARCH_WATCH')
        self.assertEqual(c['research_information_gaps'][0]['origin'],'SYSTEM_NOT_REQUESTED')
        self.assertIn('不能解讀為市場沒有資料',research_card('1111','test','觀望',c))

    def test_real_adapter_passes_raw_quote_provenance_and_history_to_research(self):
        runtime=LegacyRuntime.__new__(LegacyRuntime);runtime.output=self.root/'broker';runtime.audit=Mock()
        runtime.observations={};runtime.stop_event=None;runtime.market_map=lambda *a:{'1111':5}
        raw=normalize_stock(stock(),AT);raw.update(venue='TWSE',intraday_ticks=[tick(AT.replace(minute=1))],
            tick_history_status='RECEIVED_TICKS_PARTIAL_COVERAGE')
        runtime.broker=Mock();runtime.broker.read_quarantined=False
        runtime.broker.fetch_quotes_with_markets.return_value=({'1111':raw},{})
        with patch('opportunity_flow.legacy_runtime.datetime') as clock,patch('capital_multi_quote.now',return_value=AT):
            clock.now.return_value=AT
            rows=runtime.fetch_buy_quotes(['1111'],self.ds)
        self.assertEqual(rows['1111']['intraday_evidence']['tick_count'],1)
        self.assertEqual(rows['1111']['information_trace']['fields']['ask']['origin'],'PRESENT')
        self.assertNotIn('intraday_ticks',rows['1111'])
        self.assertEqual(read(runtime.output/'latest_buy_quotes.json')['quotes'][0]['intraday_history']['tick_count'],1)


class DisclosureSchemaTests(unittest.TestCase):
    def test_changed_source_columns_do_not_become_checked_empty_announcements(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot=current_events(tmp,AT,fetch=lambda url:[dict(new_company_field='1111',new_date='20261124')])
            event=for_symbol(snapshot,'1111','TWSE',AT,5)
            self.assertEqual(event['status'],'SOURCE_UNAVAILABLE')
            self.assertEqual(len(snapshot['errors']),4)
        with tempfile.TemporaryDirectory() as tmp:
            snapshot=current_events(tmp,AT,fetch=lambda url:[{'公司代號':None,'發言日期':'20261124','主旨':None}])
            self.assertEqual(len(snapshot['errors']),4)

    def test_valid_empty_official_feed_is_distinct_from_schema_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot=current_events(tmp,AT,fetch=lambda url:[])
            event=for_symbol(snapshot,'1111','TWSE',AT,5)
            self.assertEqual(event['status'],'SOURCE_REVIEW_REQUIRED')
            self.assertEqual(snapshot['errors'],{})
            self.assertFalse(event['historical_corporate_actions_verified'])


if __name__=='__main__':unittest.main()
