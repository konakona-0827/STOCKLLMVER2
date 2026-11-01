from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import Mock

from config import TAIPEI
from capital import normalize_stock
from quant_research.paper_runtime import PaperRuntime, market_open, next_open, future_sessions


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'config').mkdir()
        (self.root/'config/universe_tw.json').write_text(json.dumps(dict(symbols=['0050', '2330'])))
        self.config = self.root/'paper.json'
        self.settings = dict(mode='PAPER_ONLY', quote_interval_seconds=30, ranking_interval_seconds=300,
                             history_bars=250, experiment_directory='data/planned_paper/test', initial_twd_per_strategy=100000)
        self.config.write_text(json.dumps(self.settings))
        self.at = datetime(2026, 11, 2, 10, tzinfo=TAIPEI)
        self.quote = dict(symbol='2330', quote_status='LIVE', quote_timestamp=self.at.isoformat(),
                          last_price=100, bid=99.9, ask=100, bid_size=20, ask_size=30,
                          lot_type='INTRADAY_ODD_LOT', book_size_unit='shares')
        self.reader = Mock(return_value=dict(quotes=[self.quote]))
        self.history = Mock(return_value={'2330': dict(bars=[], status=dict(coverage='UNAVAILABLE'))})

    def runtime(self):
        return PaperRuntime(root=self.root, config_path=self.config,
                            quote_reader=self.reader, history_reader=self.history)

    def test_weekend_waits_without_broker_calls(self):
        rt = self.runtime()
        rt.set_auto(True)
        state = rt.step(datetime(2026, 11, 1, 22, tzinfo=TAIPEI))
        self.reader.assert_not_called()
        self.assertEqual(state['status'], 'WAIT_SESSION')
        self.assertEqual(state['next_market_open_taipei'], '2026-11-02T09:00:00+08:00')
        self.assertTrue(state['auto_enabled'])
        self.assertFalse(state['live_enabled'])

    def test_auto_off_does_not_scan(self):
        rt = self.runtime()
        rt.step(self.at)
        self.reader.assert_not_called()
        self.history.assert_not_called()

    def test_market_steps_prepare_once_and_rank_only_on_schedule(self):
        rt = self.runtime()
        rt.set_auto(True)
        first = rt.step(self.at)
        self.assertEqual(first['cycles'], 1)
        self.assertTrue(first['summary']['A']['ranking_updated'])
        self.assertEqual(first['live_quote_count'], 1)
        self.quote['quote_timestamp'] = (self.at+timedelta(seconds=30)).isoformat()
        second = rt.step(self.at+timedelta(seconds=30))
        self.assertFalse(second['summary']['A']['ranking_updated'])
        self.assertIn('A', second['last_ranking_summary'])
        self.assertFalse(second['accounts']['A']['positions'])
        self.history.assert_called_once()

    def test_quote_failure_is_visible_and_does_not_trade(self):
        rt = self.runtime()
        self.reader.side_effect = RuntimeError('BROKER_DISCONNECTED')
        rt.set_auto(True)
        s = rt.step(self.at)
        self.assertEqual(s['status'], 'ERROR_RETRY_PENDING')
        self.assertTrue(s['errors'])
        self.assertFalse(s['accounts']['A']['positions'])
        self.assertTrue(s['auto_enabled'])

    def test_missing_depth_does_not_claim_tradable_quotes(self):
        self.quote.pop('ask_size')
        rt = self.runtime()
        rt.set_auto(True)
        s = rt.step(self.at)
        self.assertEqual(s['status'], 'WAIT_VALID_QUOTES')
        self.assertEqual(s['live_quote_count'], 0)

    def test_fail_closed_mode_and_directory(self):
        for changes in (dict(mode='LIVE'), dict(experiment_directory='../execution'), dict(quote_interval_seconds=1)):
            self.config.write_text(json.dumps(dict(self.settings, **changes)))
            with self.assertRaises(ValueError):
                self.runtime()

    def test_no_next_year_calendar_is_invented(self):
        self.assertEqual(future_sessions(datetime(2027, 1, 1, tzinfo=TAIPEI)), [])
        self.assertFalse(market_open(datetime(2026, 11, 1, 10, tzinfo=TAIPEI)))
        self.assertFalse(market_open(self.at.replace(hour=14)))

    def test_sdk_oddlot_book_depth_is_shares_not_cumulative_volume(self):
        stock = SimpleNamespace(nTradingLotFlag=1, nTradingDay=20261102, nDealTime=100000,
            sDecimal=2, bstrStockNo='2330', bstrStockName='TEST', nClose=10000, nBid=9990,
            nAsk=10000, nRef=9900, nTQty=99999, nSimulate=0, nBc=20, nAc=30)
        q = normalize_stock(stock, self.at)
        self.assertEqual(q['bid_size'], 20)
        self.assertEqual(q['ask_size'], 30)
        self.assertEqual(q['volume_shares'], 99999)
        self.assertEqual(q['book_size_unit'], 'shares')

    def test_month_rollover_refreshes_incomplete_previous_month_cache(self):
        from unittest.mock import patch
        from io import BytesIO
        import daily_history
        cache = self.root/'cache'
        cache.mkdir()
        (cache/'2330_202610.json').write_text(json.dumps(dict(symbol='2330', month='202610',
            fetched_at='2026-10-29T18:00:00+08:00', bars=[dict(date='2026-10-29')])))
        payload = dict(stat='OK', title='2330', fields=['日期', '成交股數', '開盤價', '最高價', '最低價', '收盤價'],
                       data=[['115/10/30', '1000', '100', '101', '99', '100']])
        with patch('daily_history.urlopen', return_value=BytesIO(json.dumps(payload).encode())) as fetch:
            bars, error = daily_history._fetch_month('2330', datetime(2026,10,1).date(), cache,
                datetime(2026,11,1).date(), minimum_latest='2026-10-30')
        self.assertIsNone(error)
        self.assertEqual(bars[-1]['date'], '2026-10-30')
        fetch.assert_called_once()

    def test_stale_history_retries_after_15_minutes(self):
        self.history.return_value = {'2330': dict(bars=[], status=dict(
            latest_date='2026-10-29', expected_latest_date='2026-10-30', coverage='STALE'))}
        rt = self.runtime()
        rt.set_auto(True)
        rt.step(self.at)
        self.assertEqual(rt.stale_symbols(), ['2330'])
        self.history.return_value = {'2330': dict(bars=[], status=dict(
            latest_date='2026-10-30', expected_latest_date='2026-10-30', coverage='CURRENT'))}
        at = self.at+timedelta(minutes=15)
        self.quote['quote_timestamp'] = at.isoformat()
        rt.step(at)
        self.assertEqual(self.history.call_count, 2)
        self.assertEqual(rt.stale_symbols(), [])

    def test_stop_during_quote_request_does_not_execute_a_cycle(self):
        stopping = []
        def read(symbols):
            stopping.append(True)
            return dict(quotes=[self.quote])
        rt = PaperRuntime(root=self.root, config_path=self.config, quote_reader=read,
                          history_reader=self.history, stop_requested=lambda: bool(stopping))
        rt.set_auto(True)
        state = rt.step(self.at)
        self.assertEqual(state['status'], 'STOPPED')
        self.assertEqual(state['cycles'], 0)
        self.assertFalse(state['auto_enabled'])
