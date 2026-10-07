import ast
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import contextlib
import io
from unittest.mock import patch, Mock
from config import ROOT, TAIPEI
from universe import load_universe, load_rules
from capital_multi_quote import MultiCapitalMarket, multi_snapshot
from quant_scanner import scan_and_rank, scan_quote
from candidate_analyzer import validate, build_request, analyze
from multi_scan import candidate_payload
import scan_store


class MultiTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 10, 7, 10, tzinfo=TAIPEI)
        self.rules = load_rules(ROOT/'config'/'scan_rules.json')
        self.q = dict(symbol='2330', quote_status='LIVE', last_price=100, open=99,
                      high=102, low=98, volume=100000, bid=99.9, ask=100.1,
                      quote_timestamp=self.at.isoformat(), exchange_time=self.at.isoformat(),
                      received_at=self.at.isoformat(), request_completed_at=self.at.isoformat(),
                      warnings=[], is_trial=False)

    def candidates(self):
        return [dict(symbol='2330', quote_status='LAST_KNOWN', last_price=100, position_qty=None)]

    def answer(self):
        return dict(market_view='盤後資料不是即時行情', selected=[dict(rank=1, symbol='2330',
                    decision='WAIT', confidence=.2, reference_price=100, suggested_price=None,
                    reason='僅單筆快照', risks=['這不是即時行情'])], rejected_candidates=[], data_quality='LAST_KNOWN')

    def test_universe_and_rules(self):
        self.assertEqual(len(load_universe(ROOT/'config'/'universe_tw.json')['symbols']), 20)
        self.assertEqual(self.rules['candidate_top_n'], 10)

    def test_metrics_correct(self):
        r = scan_quote(self.q, self.rules, self.at)
        self.assertTrue(r['eligible'])
        self.assertAlmostEqual(r['metrics']['intraday_return'], 100/99-1)
        self.assertAlmostEqual(r['metrics']['range_position'], .5)
        self.assertAlmostEqual(r['metrics']['spread_pct'], .002)
        self.assertEqual(r['metrics']['liquidity_proxy_twd'], 10000000)
        self.assertAlmostEqual(r['quant_score'], sum(r['components'].values()), places=6)

    def test_daily_indicators_not_invented(self):
        metrics = scan_quote(self.q, self.rules, self.at)['metrics']
        for key in ['RSI','ATR','MA5','MA20','return_1d','return_5d','return_20d','volume_ratio']:
            self.assertIsNone(metrics[key])

    def test_stale_penalty(self):
        live = scan_quote(self.q, self.rules, self.at)
        stale = scan_quote({**self.q, 'quote_status':'LAST_KNOWN'}, self.rules, self.at)
        self.assertAlmostEqual(live['quant_score']-stale['quant_score'], self.rules['weights']['stale_penalty'])

    def test_filters(self):
        cases = [(dict(quote_status='UNAVAILABLE'), 'UNAVAILABLE'), (dict(last_price=0), 'INVALID_LAST_PRICE'),
                 (dict(bid=101,ask=100), 'INVALID_BID_ASK'), (dict(volume=1), 'LOW_OR_MISSING_VOLUME'),
                 (dict(ask=110), 'SPREAD_TOO_WIDE'), (dict(is_trial=True), 'TRIAL_QUOTE')]
        for change, reason in cases:
            r = scan_quote({**self.q, **change}, self.rules, self.at)
            self.assertFalse(r['eligible'])
            self.assertIn(reason, r['exclusion_reasons'])
            self.assertIsNone(r['quant_score'])

    def test_flat_range_null(self):
        r = scan_quote({**self.q,'high':100,'low':100}, self.rules, self.at)
        self.assertIsNone(r['metrics']['range_position'])
        self.assertEqual(r['components']['breakout'], 0)

    def test_top_n_and_stable_ties(self):
        quotes = [{**self.q, 'symbol': str(1000+i)} for i in range(20)]
        scanned, ranked, top = scan_and_rank(list(reversed(quotes)), self.rules, self.at)
        self.assertEqual(len(top), 10)
        self.assertEqual([q['symbol'] for q in top], [str(1000+i) for i in range(10)])
        self.assertEqual(len(ranked), 20)

    def test_payload_only_top_n(self):
        qs = [{**self.q,'symbol':str(1000+i)} for i in range(20)]
        _, _, top = scan_and_rank(qs, self.rules, self.at)
        with tempfile.TemporaryDirectory() as tmp:
            candidates = candidate_payload(top, qs, Path(tmp)/'db.sqlite3', self.at.isoformat())
        payload = json.loads(build_request(candidates)['input'])
        self.assertEqual(len(payload['candidates']), 10)
        self.assertNotIn('1019', [q['symbol'] for q in payload['candidates']])
        self.assertNotIn('raw_fields', str(payload))

    def test_partial_batch_unavailable(self):
        raw = dict(symbol='2330', source='CAPITAL_SKCOM', exchange_time=self.at.isoformat(),
                   price=100, bid=99.9, ask=100.1, volume_shares=100000, open=99,high=102,low=98)
        batch = multi_snapshot(['2330','2454'], {'2330':raw}, {'2454':'test'}, self.at.isoformat(), self.at.isoformat(), self.at)
        self.assertEqual(batch['symbols_received'], 1)
        self.assertEqual(batch['symbols_unavailable'], 1)
        self.assertEqual(batch['quotes'][1]['quote_status'], 'UNAVAILABLE')

    def test_batch_one_connect_and_one_subscribe_despite_bad_symbol(self):
        audit = SimpleNamespace(event=Mock())
        market = MultiCapitalMarket(audit)
        market.connect = Mock()
        def lookup(m, symbol, stock):
            if symbol=='2454': raise OSError('fixture')
            return SimpleNamespace(nTradingLotFlag=1,nTradingDay=20261007,nDealTime=100000,
                sDecimal=2,bstrStockNo=symbol,bstrStockName='TEST',nClose=10000,nBid=9990,nAsk=10010,
                nRef=10000,nTQty=100000,nSimulate=0,nOpen=9900,nHigh=10200,nLow=9800),0
        quote=SimpleNamespace(SKQuoteLib_RequestStocksWithMarketNo=Mock(return_value=(1,0)),
                              SKQuoteLib_GetStockByMarketAndNo=Mock(side_effect=lookup))
        market.objects={'SKQuoteLib':quote}
        market.sk=SimpleNamespace(SKSTOCKLONG=lambda: SimpleNamespace())
        with patch('capital_multi_quote.time.monotonic',side_effect=[0,2]):
            raw,errors=market.fetch_batch(['2330','2454'],timeout=1)
        market.connect.assert_called_once()
        quote.SKQuoteLib_RequestStocksWithMarketNo.assert_called_once_with(1,5,'2330,2454')
        self.assertEqual(quote.SKQuoteLib_GetStockByMarketAndNo.call_count,2)
        self.assertIn('2330',raw)
        self.assertIn('2454',errors)

    def test_valid_selection(self):
        self.assertEqual(validate(self.answer(),self.candidates())['selected'][0]['decision'],'WAIT')

    def test_unknown_symbol_and_reference_rejected(self):
        for change in [dict(symbol='9999'),dict(reference_price=999),dict(confidence=True),dict(decision='SELL')]:
            answer=self.answer();answer['selected'][0].update(change)
            with self.assertRaises(ValueError):validate(answer,self.candidates())

    def test_stale_claim_and_risk_checked(self):
        a=self.answer();a['data_quality']='LIVE'
        with self.assertRaises(ValueError):validate(a,self.candidates())
        a=self.answer();a['selected'][0]['risks']=[]
        with self.assertRaises(ValueError):validate(a,self.candidates())

    def test_missing_or_duplicate_candidate_rejected(self):
        a=self.answer();a['selected']=[]
        with self.assertRaises(ValueError):validate(a,self.candidates())
        a=self.answer();a['rejected_candidates']=[dict(symbol='2330',reason='test')]
        with self.assertRaises(ValueError):validate(a,self.candidates())

    def test_empty_selection_allowed(self):
        a=self.answer();a['selected']=[];a['rejected_candidates']=[dict(symbol='2330',reason='資料不足')]
        self.assertEqual(validate(a,self.candidates())['selected'],[])

    def test_invalid_json_retained_and_no_invented_selection(self):
        c=self.candidates();s,r=analyze(c,build_request(c),provider=lambda _:('bad json',{}))
        self.assertEqual(s['selected'],[])
        self.assertEqual(r['raw_response'],'bad json')
        self.assertEqual(r['validation'],'ERROR')

    def test_sqlite_additive_persistence(self):
        batch=dict(quotes=[self.q]);scanned,ranked,_=scan_and_rank([self.q],self.rules,self.at)
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'db.sqlite3'
            for run_id in ['one','two']:
                result=scan_store.persist(p,{'run_id':run_id},batch,{},scanned,ranked,{}, {},self.answer())
                self.assertEqual(result['integrity_check'],'ok')
            import sqlite3
            con=sqlite3.connect(p)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM multi_market_snapshots').fetchone()[0],2)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM selection_analyses').fetchone()[0],2)
            con.close()

    def test_no_order_calls_in_new_modules(self):
        for name in ['capital_multi_quote.py','quant_scanner.py','candidate_analyzer.py','scan_store.py','multi_scan.py']:
            tree=ast.parse((ROOT/name).read_text(encoding='utf-8'))
            attrs={n.attr for n in ast.walk(tree) if isinstance(n,ast.Attribute)}
            self.assertFalse(any(n.startswith(('SendStock','CancelOrder','SendFuture','paper_fill')) for n in attrs))

    def test_all_unavailable_skips_openai_and_saves_run(self):
        import multi_scan
        fake = SimpleNamespace(fetch_batch=Mock(return_value=({}, {})), close=Mock())
        with tempfile.TemporaryDirectory() as tmp:
            with patch('sys.argv', ['multi_scan.py','--data-dir',tmp]), \
                 patch('multi_scan.MultiCapitalMarket', return_value=fake), \
                 patch('multi_scan.candidate_analyzer.analyze') as api, \
                 contextlib.redirect_stdout(io.StringIO()):
                code = multi_scan.main()
            self.assertEqual(code,2)
            api.assert_not_called()
            fake.fetch_batch.assert_called_once()
            manifest=json.loads((Path(tmp)/'latest_scan.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['symbols_received'],0)
            result=json.loads((Path(tmp)/'selection_decision.json').read_text(encoding='utf-8'))
            self.assertEqual(result['market_view'],'NO_VALID_MARKET_DATA')
            self.assertEqual(result['selected'],[])


if __name__=='__main__': unittest.main()
