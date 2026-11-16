"""MIS event gaps retain real trade time and cannot bypass execution evidence."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from opportunity_flow.common import policy, stamp
from opportunity_flow.data import load_dataset, validate_quote, validate_reference
from opportunity_flow.demo import create
from opportunity_flow.pipeline import run_cycle
from opportunity_flow.public_quotes import fetch, parse
from test_opportunity_flow import FakeRunner


class PublicQuoteRecoveryTests(unittest.TestCase):
    def row(self, **updates):
        return dict(ex='tse', c='1111', d='20260930', t='10:01:00', z='-',
                    tlong='9999999999999', y='99', pz='103',
                    trade=dict(z='104.2', t='10:00:00', v=12), **updates)

    def parse_row(self, row):
        return parse(dict(rtcode='0000', msgArray=[row]), [('TWSE', '1111')])

    def test_missing_event_prices_use_the_official_last_trade_fields(self):
        for value in ('-', '', None, 'bad'):
            with self.subTest(event_price=value):
                row=self.row();row['z']=value
                quotes, errors=self.parse_row(row)
                self.assertFalse(errors)
                self.assertEqual(quotes[0]['last_price'], 104.2)
                self.assertEqual(quotes[0]['quote_timestamp'], '2026-09-30T10:00:00+08:00')
                self.assertEqual(quotes[0]['price_source_field'], 'trade.z')
                self.assertTrue(quotes[0]['last_trade_fallback'])
        row=self.row();row.pop('z')
        self.assertEqual(self.parse_row(row)[0][0]['last_price'], 104.2)

    def test_a_current_trade_is_preferred_over_the_older_nested_trade(self):
        row=self.row();row.update(z='105',t='10:01:00')
        quote=self.parse_row(row)[0][0]
        self.assertEqual(quote['last_price'],105)
        self.assertEqual(quote['quote_timestamp'],'2026-09-30T10:01:00+08:00')
        self.assertFalse(quote['last_trade_fallback'])

    def test_recovered_trade_does_not_get_a_fresh_event_or_query_timestamp(self):
        row=self.row();row['trade']['t']='09:55:00'
        quote=self.parse_row(row)[0][0]
        self.assertIsNone(validate_reference(quote,'1111','TWSE',stamp('2026-09-30T10:01:00+08:00'),120))
        row['trade']['t']='10:02:00'
        quote=self.parse_row(row)[0][0]
        self.assertIsNone(validate_reference(quote,'1111','TWSE',stamp('2026-09-30T10:01:00+08:00'),120))

    def test_missing_real_trade_never_uses_previous_close_or_book_prices(self):
        for trade in (None, {}, '-', dict(z='-',t='10:00:00'), dict(z='0',t='10:00:00'),
                      dict(z='NaN',t='10:00:00')):
            with self.subTest(trade=trade):
                row=self.row();row.update(trade=trade,a='105_106_',b='104_103_')
                quotes,errors=self.parse_row(row)
                self.assertFalse(quotes)
                self.assertEqual([e['reason'] for e in errors],['PUBLIC_LAST_TRADE_UNAVAILABLE'])

    def test_fallback_without_a_real_trade_time_is_not_relabelled(self):
        for when in (None,'','-','25:01:00'):
            with self.subTest(trade_time=when):
                row=self.row();row['trade']['t']=when
                quotes,errors=self.parse_row(row)
                self.assertFalse(quotes)
                self.assertEqual(errors[0]['reason'],'PUBLIC_LAST_TRADE_TIME_INVALID')

    def test_fallback_remains_public_reference_and_never_oddlot_evidence(self):
        quote=self.parse_row(self.row())[0][0]
        at=stamp('2026-09-30T10:01:00+08:00')
        self.assertIsNotNone(validate_reference(quote,'1111','TWSE',at,120))
        verified,error=validate_quote(quote,'1111','TWSE',at,120)
        self.assertIsNone(verified)
        self.assertEqual(error,'LIVE_NON_TRIAL_QUOTE_REQUIRED')

    def test_batch_keeps_good_rows_and_counts_unavailable_trade_once(self):
        good=self.row();bad=self.row();bad.update(c='3333',trade=None)
        quotes,errors=parse(dict(rtcode='0000',msgArray=[good,bad]),[('TWSE','1111'),('TWSE','3333')])
        self.assertEqual([q['symbol'] for q in quotes],['1111'])
        self.assertEqual(len(errors),1)
        self.assertEqual(errors[0]['symbol'],'3333')

    def test_fetch_records_fallback_count_and_raw_source_hash(self):
        raw=json.dumps(dict(rtcode='0000',msgArray=[self.row()])).encode()
        with patch('opportunity_flow.public_quotes.urlopen',return_value=io.BytesIO(raw)) as http,\
             patch('opportunity_flow.public_quotes.time.sleep'):
            result=fetch([('TWSE','1111')])
        self.assertEqual(result['last_trade_fallback_count'],1)
        self.assertEqual(result['source_sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(http.call_args.args[0].get_header('Referer'),'https://mis.twse.com.tw/stock/')

    def test_recovered_public_reference_enters_both_research_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);at=stamp(create(root/'dataset')).replace(hour=10)
            ds=load_dataset(root/'dataset');row=self.row()
            row.update(d=at.strftime('%Y%m%d'))
            row['trade']['z']=str(ds['grouped']['1111'][-1]['close'])
            quote=self.parse_row(row)[0][0]
            runner=FakeRunner()
            result=run_cycle(ds,at,policy(),root/'research',quotes={('TWSE','1111'):quote},
                account_context={'1111':dict(available_cash_twd=3373,held_qty=0,
                    minimum_fee_reserve_twd=20,buy_cash_buffer_rate=.01)},runner=runner,use_llm=True)
            candidate=next(c for c in result['candidates'] if c['symbol']=='1111')
            self.assertEqual(candidate['state'],'RESEARCH_READY')
            self.assertEqual([role for packets,role in runner.calls],['researcher','challenger'])
            self.assertEqual(result['summary']['orders_sent'],0)
            self.assertFalse(candidate['quote'])
            self.assertTrue(candidate['hypothetical_reference'])


if __name__=='__main__':
    unittest.main()
