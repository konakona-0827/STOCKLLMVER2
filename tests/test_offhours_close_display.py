"""Official daily close references stay entirely inside the display projection."""
from copy import deepcopy
import csv
from datetime import datetime
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock,patch

from opportunity_flow.common import TAIPEI,read,save
from opportunity_flow.dashboard_service import FlowViewService
from opportunity_flow.data import load_dataset


class OffhoursCloseDisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.dataset=self.root/'dataset';self.dataset.mkdir()
        self.state=self.root/'flow';self.at=datetime(2026,11,13,17,0,tzinfo=TAIPEI)
        self.write_dataset()
        save(self.root/'config/execution_live.json',dict(enabled=True))
        import position_store
        self.db_path=self.root/'data/analysis/market_history.sqlite3'
        position_store.save_manual(self.db_path,'0050',2,3,90,92)
        self.quote=dict(symbol='0050',name='ETF',quote_status='LIVE',quote_basis='capital_intraday_odd_lot',
            quote_timestamp=self.at.isoformat(),is_trial=False,last_price=99,bid=98.9,ask=99)
        self.snapshot=dict(checked_at=self.at.isoformat(),status='OK',errors=[],broker_inventory_verified=True,
            budget_status='READY',current_buy_budget_twd='1000',
            inventory_comparison=[dict(symbol='0050',broker_cash_qty=5,ai_qty=3,status='MATCH')],
            position_quotes=[self.quote])
        self.controller=SimpleNamespace(coordinator=None,status='STOPPED',error='',start=Mock(),
            refresh_account=Mock(return_value=self.snapshot),test_health_order=Mock())
        self.args=SimpleNamespace(state=str(self.state),dataset=str(self.dataset),read_only=False)
        self.view=FlowViewService(self.root,self.args,self.controller)

    def clock(self):
        at=self.at
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return at.astimezone(tz) if tz else at.astimezone().replace(tzinfo=None)
        return Clock

    def write_dataset(self,close=97,source='TWSE_DAILY',last_day='2026-11-13'):
        fields=('symbol','exchange','date','open','high','low','close','volume','turnover_twd','source','source_sha256','retrieved_at')
        with (self.dataset/'bars.csv').open('w',encoding='utf-8',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
            for day,price in [('2026-11-12',95),(last_day,close)]:
                writer.writerow(dict(symbol='0050',exchange='TWSE',date=day,open=price,high=price+1,low=price-1,
                    close=price,volume=1000000,turnover_twd=price*1000000,source=source,
                    source_sha256='fixture-source-hash',retrieved_at='2026-11-13T16:00:00+08:00'))
        (self.dataset/'sessions.csv').write_text('date\n2026-11-12\n'+last_day+'\n',encoding='utf-8')
        save(self.dataset/'manifest.json',dict(symbols=['0050'],end=last_day,files={name:hashlib.sha256((self.dataset/name).read_bytes()).hexdigest()
            for name in ('bars.csv','sessions.csv')}))
        save(self.dataset/'master.json',{'0050':dict(name='ETF',exchange='TWSE')})

    def refresh(self):
        with patch('opportunity_flow.dashboard_service.datetime',self.clock()),patch(
            'execution.health_monitor.latest_refresh_snapshot',return_value=self.snapshot):
            return self.view.refresh()

    def test_offhours_uses_official_close_even_when_stored_live_quote_is_recent(self):
        before=deepcopy(self.snapshot);db_before=self.db_path.read_bytes()
        result,positions,health=self.refresh();quote=result['batch']['quotes'][0]
        self.assertEqual(quote['last_price'],97);self.assertEqual(quote['price_as_of'],'2026-11-13')
        self.assertEqual(quote['quote_status'],'DAILY_CLOSE_REFERENCE')
        self.assertEqual(quote['quote_basis'],'OFFICIAL_DAILY_CLOSE_NOT_EXECUTABLE')
        self.assertTrue(quote['display_only']);self.assertFalse(quote['execution_usable'])
        self.assertEqual((quote['bid'],quote['ask'],quote['quote_timestamp']),(None,None,None))
        self.assertEqual(quote['source'],'TWSE_DAILY');self.assertEqual(result['batch']['symbols_received'],1)
        self.assertEqual(positions[0]['current_price'],97);self.assertEqual(positions[0]['price_as_of'],'2026-11-13')
        self.assertEqual(positions[0]['total_unrealized_pnl'],29)
        self.assertEqual(positions[0]['source'],'USER_CONFIGURED_NOT_BROKER_VERIFIED')
        self.assertEqual(health,before);self.assertEqual(self.snapshot,before)
        self.assertEqual(self.db_path.read_bytes(),db_before)
        self.controller.refresh_account.assert_not_called();self.controller.test_health_order.assert_not_called()
        self.controller.start.assert_not_called();self.assertTrue(result['display_only'])

    def test_bulk_collector_official_source_is_accepted(self):
        self.write_dataset(source='TWSE_OFFICIAL_DAILY_BULK')
        with patch('opportunity_flow.dashboard_service.datetime',self.clock()):
            quotes=self.view._daily_close_quotes({'0050'})
        self.assertEqual(quotes['0050']['source'],'TWSE_OFFICIAL_DAILY_BULK')
        self.assertEqual(quotes['0050']['quote_status'],'DAILY_CLOSE_REFERENCE')

    def test_configured_holding_without_broker_quote_can_still_display_close(self):
        self.snapshot['position_quotes']=[];self.snapshot['inventory_comparison']=[]
        result,positions,_=self.refresh()
        self.assertEqual(result['batch']['quotes'][0]['last_price'],97)
        self.assertEqual(positions[0]['current_price'],97)

    def test_intraday_keeps_live_prices_and_does_not_read_daily_close_dataset(self):
        self.at=self.at.replace(hour=10);self.quote['quote_timestamp']=self.at.isoformat()
        with patch('opportunity_flow.data.load_dataset',side_effect=AssertionError('Display must use live quotes intraday')):
            result,positions,health=self.refresh()
        self.assertEqual(result['batch']['quotes'][0]['quote_status'],'LIVE')
        self.assertEqual(positions[0]['current_price'],99);self.assertEqual(health,self.snapshot)

    def test_daily_cache_and_unchanged_view_do_not_repeat_dataset_or_account_reads(self):
        with patch('opportunity_flow.dashboard_service.datetime',self.clock()),patch(
            'execution.health_monitor.latest_refresh_snapshot',return_value=self.snapshot) as account,patch(
            'opportunity_flow.data.load_dataset',wraps=load_dataset) as dataset:
            first=self.view.refresh();second=self.view.refresh()
            self.view._daily_close_quotes(['0050']);self.view._daily_close_quotes(['0050'])
        self.assertIs(first,second);dataset.assert_called_once();account.assert_called_once()

    def test_daily_file_change_invalidates_display_and_cached_close(self):
        with patch('opportunity_flow.data.load_dataset',wraps=load_dataset) as dataset:
            first=self.refresh()[0]
            self.write_dataset(close=98)
            second=self.refresh()[0]
            self.assertEqual(second['batch']['quotes'][0]['last_price'],98)
            self.assertNotEqual(first['_flow_view_revision'],second['_flow_view_revision'])
            # Even a sessions-only version change invalidates both caches.
            path=self.dataset/'sessions.csv';stat=path.stat();os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns+2_000_000))
            third=self.refresh()[0]
        self.assertNotEqual(second['_flow_view_revision'],third['_flow_view_revision'])
        self.assertEqual(dataset.call_count,3)

    def test_market_session_change_replaces_cached_live_quote_with_close(self):
        self.at=self.at.replace(hour=10);self.quote['quote_timestamp']=self.at.isoformat()
        first=self.refresh()[0];self.assertEqual(first['batch']['quotes'][0]['quote_status'],'LIVE')
        self.at=self.at.replace(hour=17)
        second=self.refresh()[0]
        self.assertEqual(second['batch']['quotes'][0]['quote_status'],'DAILY_CLOSE_REFERENCE')
        self.assertNotEqual(first['_flow_view_revision'],second['_flow_view_revision'])

    def test_missing_corrupt_unofficial_and_future_daily_data_never_invent_a_price(self):
        for kind in ('missing','hash_mismatch','unofficial','future'):
            with self.subTest(kind=kind):
                self.write_dataset(source='SYNTHETIC_CONTROL' if kind=='unofficial' else 'TWSE_DAILY',
                    last_day='2026-11-16' if kind=='future' else '2026-11-13')
                if kind=='missing':(self.dataset/'bars.csv').unlink()
                if kind=='hash_mismatch':
                    with (self.dataset/'bars.csv').open('a',encoding='utf-8') as stream:stream.write('\n')
                result,positions,_=self.refresh();quote=result['batch']['quotes'][0]
                if kind=='future':
                    self.assertEqual(quote['price_as_of'],'2026-11-12');self.assertEqual(quote['last_price'],95)
                else:
                    self.assertEqual(quote['quote_status'],'UNAVAILABLE');self.assertIsNone(positions[0]['current_price'])

    def test_health_checks_receive_only_display_references_offhours(self):
        self.controller.test_health_order.return_value=({'status':'BLOCKED','new_send_attempted':False},self.snapshot)
        with patch('opportunity_flow.dashboard_service.datetime',self.clock()),patch(
            'opportunity_flow.health.inspect_health',side_effect=lambda *a,**k:dict(status='PASS',checks=[])) as inspect:
            self.view.check_health()
            self.assertEqual(inspect.call_args.kwargs['reference_quotes']['0050']['price_as_of'],'2026-11-13')
            self.view.test_health_order(confirmed=True)
            self.assertFalse(inspect.call_args.kwargs['reference_quotes']['0050']['execution_usable'])
        self.at=self.at.replace(hour=10)
        with patch('opportunity_flow.dashboard_service.datetime',self.clock()),patch(
            'opportunity_flow.health.inspect_health',return_value=dict(status='PASS',checks=[])) as inspect:
            self.view.check_health()
            self.assertIsNone(inspect.call_args.kwargs['reference_quotes'])


if __name__=='__main__':unittest.main()
