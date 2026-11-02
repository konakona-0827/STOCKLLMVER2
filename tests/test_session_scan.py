"""Fabricated quote, schedule and email contracts; no real network or email."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from support_scout.contracts import DataError, timestamp
from support_scout.session_scan import market_open,run_session,validate_quote,attach_quotes
from support_scout.notifications import actionable,configure,enqueue_report,flush,decision_email_text
from support_scout.opportunity import load_policy,attach_opportunities,_reference_plan
from support_scout.communication import load_policy as wire_policy,review_wire,search_wire
from support_scout.pipeline import run
from support_scout.japan_demo import demo_bundle
from support_scout.discovery import make_search_packet


class SessionTests(unittest.TestCase):
    def test_lunch_holiday_weekend_and_session_boundaries(self):
        for s,expected in [('2026-11-02T09:00:00+09:00',True),('2026-11-02T11:29:59+09:00',True),
            ('2026-11-02T11:30:00+09:00',False),('2026-11-02T12:29:59+09:00',False),
            ('2026-11-02T12:30:00+09:00',True),('2026-11-02T15:30:00+09:00',False),
            ('2026-11-01T10:00:00+09:00',False),('2026-11-03T10:00:00+09:00',False)]:
            with self.subTest(s=s):self.assertEqual(market_open(timestamp(s)),expected)

    def test_closed_session_has_zero_calls_and_does_not_start_observer(self):
        with tempfile.TemporaryDirectory() as d:
            r=run_session(now=timestamp('2026-11-02T12:00:00+09:00'),data_root=d,observer=lambda **k:self.fail('paid call'))
            self.assertEqual(r['status'],'SKIPPED_MARKET_CLOSED');self.assertEqual(r['model_calls'],0)
            self.assertFalse((Path(d)/'session_scan_state.json').exists())

    def test_same_half_hour_cannot_incur_duplicate_calls(self):
        calls=[]
        def observe(**kwargs):
            calls.append(kwargs)
            return {'discovery':{'total_call_count':3}},Path('fabricated-output'),0
        with tempfile.TemporaryDirectory() as d:
            first=run_session(now=timestamp('2026-11-02T09:01:00+09:00'),data_root=d,observer=observe)
            second=run_session(now=timestamp('2026-11-02T09:20:00+09:00'),data_root=d,observer=observe)
            third=run_session(now=timestamp('2026-11-02T09:30:00+09:00'),data_root=d,observer=observe)
        self.assertEqual(len(calls),2);self.assertTrue(calls[0]['session_prices'])
        self.assertEqual(first['status'],'COMPLETE');self.assertEqual(second['status'],'SKIPPED_DUPLICATE_SLOT')
        self.assertEqual(third['status'],'COMPLETE')

    def quote(self):
        return {'symbol':'6857.T','currency':'JPY','price':100,'quoted_at':'2026-11-02T09:45:00+09:00','fetched_at':'2026-11-02T10:00:00+09:00'}

    def test_stale_and_future_quotes_cannot_be_usable(self):
        q=self.quote();v=validate_quote(q,'6857.T','2026-11-02T10:00:00+09:00')
        self.assertTrue(v['usable']);self.assertFalse(v['is_realtime']);self.assertEqual(v['age_seconds'],900)
        self.assertFalse(validate_quote({**q,'usable':True},'6857.T','2026-11-02T11:00:00+09:00')['usable'])
        for changes in [{'price':float('nan')},{'price':True},{'currency':'USD'},{'symbol':'6027.T'},
            {'quoted_at':'2026-11-02T10:01:00+09:00'}]:
            with self.subTest(changes=changes),self.assertRaises(DataError):validate_quote({**q,**changes},'6857.T','2026-11-02T10:00:00+09:00')

    def test_archived_quote_requires_matching_source_digest(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'quote.json'
            raw=json.dumps({'chart':{'result':[{'meta':{'symbol':'6857.T','currency':'JPY','regularMarketPrice':100,
                'regularMarketTime':timestamp('2026-11-02T09:45:00+09:00').timestamp()}}]}})
            path.write_text(json.dumps({'raw_json':raw}),encoding='utf-8')
            source={'cache_file':str(path),'sha256':hashlib.sha256(raw.encode()).hexdigest(),
                'url':'https://query1.finance.yahoo.com/v8/finance/chart/6857.T?interval=1d','fetched_at':'2026-11-02T10:00:00+09:00'}
            b={'as_of':source['fetched_at'],'securities':[{'symbol':'6857.T'}],'collection':{'sources':[source]}}
            self.assertTrue(attach_quotes(deepcopy(b))['securities'][0]['market_quote']['usable'])
            source['sha256']='invalid'
            self.assertIsNone(attach_quotes(deepcopy(b))['securities'][0]['market_quote'])

    def test_quote_is_bound_and_visible_to_search_and_both_reviewers(self):
        b=demo_bundle();b['opportunity_policy']=load_policy();b['communication_policy']=wire_policy();b['session_scan']={'version':'fixture'}
        symbol=b['securities'][0]['symbol'];asof=b['as_of']
        b['securities'][0]['market_quote']={'symbol':symbol,'currency':'JPY','price':100,'quoted_at':asof,'fetched_at':asof}
        r=run(b);p=next(p for p in r['all_packets'] if p['core']['symbol']==symbol)
        v,aliases,_=review_wire(p)
        self.assertEqual(v['market_quote']['price'],100)
        self.assertTrue(any(e['kind']=='DELAYED_QUOTE' for e in p['core']['evidence']))
        sp=make_search_packet(r,{'version':'fixture','symbols':[s['symbol'] for s in b['securities']]})
        wire,_,_=search_wire(sp)
        self.assertTrue(any({**wire.get('shared_market_quote_fields',{}),**x['market_quote']}.get('price')==100
            for x in wire['candidates'] if isinstance(x.get('market_quote'),dict)))

    def test_session_wire_shares_defaults_and_plans_losslessly(self):
        from support_scout.communication import factor_session_defaults
        original={'candidates':[{'market_quote':{'price':n,'currency':'JPY','usable':True},
            'support_model_boundaries':{'technical':'strict','patterns':'independent'},
            'opportunity_reference_plans':{'H-004':{'entry_reference':n,'target_reference':n*1.02,'trigger':None}}} for n in (100,200)]}
        value=deepcopy(original);factor_session_defaults(value)
        restored=[]
        for c in value['candidates']:
            q=deepcopy(c['market_quote']);q.pop('defaults_ref')
            plans={pid:dict(zip(value['plan_layouts'][p['layout']],p['values'])) for pid,p in c['opportunity_reference_plans'].items()}
            restored.append({**c,'market_quote':{**value['shared_market_quote_fields'],**q},
                'support_model_boundaries':value['shared_support_model_boundaries'],'opportunity_reference_plans':plans})
        self.assertEqual(restored,original['candidates'])

    def test_missing_quote_blocks_an_otherwise_positive_consensus(self):
        from tests.test_opportunity import fixture
        def update(core):core.update(session_scan={'version':'fixture'},market_quote=None)
        r,b,_=fixture(core_mutation=update)
        o=attach_opportunities(r,b)['securities'][0]['opportunity_decision']
        self.assertEqual(o['action'],'WAIT');self.assertIn('FRESH_DELAYED_QUOTE_REQUIRED',o['reason_codes'])

    def test_ready_setup_reprices_from_quote_and_caps_entry_below_its_target(self):
        from tests.test_short_cycle import momentum,definition
        from support_scout.short_cycle import detect_short_cycle
        s=momentum();p=detect_short_cycle(s,timestamp(s.bars[-1].available_at),definition('MOMENTUM_CONTINUATION'),{})
        core={'technical':{'latest_close':120},'visible_series':{'price_basis':s.price_basis,'bars':[]},
            'market_quote':{'usable':True,'price':100},'session_scan':{'version':'fixture'}}
        plan=_reference_plan(core,p,load_policy())
        self.assertEqual(plan['entry_reference'],100);self.assertEqual(plan['target_reference'],102)
        self.assertAlmostEqual(plan['entry_ceiling_reference'],101.5)
        self.assertLess(plan['entry_ceiling_reference'],plan['target_reference'])
        self.assertAlmostEqual(plan['target_reference_at_entry_ceiling'],103.53)


class ActionMailTests(unittest.TestCase):
    def value(self,action='PROBE_BUY_REVIEW'):
        return {'action':action,'decision_brief':{'action':action,'suggested_amount_jpy':1000,
            'entry_reference':100,'invalidation_reference':98,'target_reference':102}}

    def test_watch_with_detailed_prices_and_hold_are_silent(self):
        for action in ['WATCH','WAIT','AVOID','HOLD','REVIEW','NO_TRADE']:
            self.assertFalse(actionable({**self.value(action),'position':{'quantity':1}}))
        self.assertTrue(actionable(self.value()))
        self.assertFalse(actionable({**self.value(),'decision_brief':{**self.value()['decision_brief'],'suggested_amount_jpy':0}}))
        self.assertTrue(actionable({**self.value(),'action':'ENTRY_REVIEW'}))

    def test_exit_and_reduce_require_confirmed_holdings(self):
        for action in ['EXIT_REVIEW','REDUCE_REVIEW']:
            self.assertFalse(actionable({'action':action}))
            self.assertTrue(actionable({'action':action,'position':{'quantity':.5}}))

    def test_old_pending_watch_cannot_be_sent_after_policy_change(self):
        from support_scout.notifications import _connect
        with tempfile.TemporaryDirectory() as d:
            db=Path(d)/'events.sqlite3';cfg=Path(d)/'mail.json';configure('test@example.com',cfg)
            con=_connect(db)
            con.execute('DROP TRIGGER actionable_events_only')  # Simulate a queued pre-upgrade WATCH.
            con.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?)',('old','6857.T','2026-11-02T09:00:00+09:00',json.dumps(self.value('WAIT')),'PENDING',None,None))
            con.commit();con.close()
            with patch.dict(os.environ,{'JP_DESK_GMAIL_APP_PASSWORD':'fake'},clear=True):
                r=flush(cfg,db,sender=lambda *a:self.fail('WATCH sent'))
            self.assertEqual(r['sent'],0)
            con=sqlite3.connect(db);self.assertEqual(con.execute('SELECT status FROM events').fetchone()[0],'SUPPRESSED');con.close()

    def test_database_rejects_watch_from_an_old_dashboard_process(self):
        from support_scout.notifications import _connect
        with tempfile.TemporaryDirectory() as d:
            con=_connect(Path(d)/'outbox.sqlite3')
            con.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?)',('old','6857.T','2026-11-02T09:00:00+09:00',json.dumps(self.value('WATCH')),'PENDING',None,None))
            con.commit();self.assertEqual(con.execute('SELECT count(*) FROM events').fetchone()[0],0);con.close()

    def test_email_contains_delay_timestamp_and_entry_band(self):
        v=self.value();v.update(symbol='6857.T',market_quote={'price':99,'quoted_at':'2026-11-02T09:45:00+09:00'})
        v['decision_brief'].update(entry_trigger_reference=100,entry_ceiling_reference=101)
        t=decision_email_text(v)
        self.assertIn('09:45',t);self.assertIn('100.00 ～ 101.00',t);self.assertIn('不是即時可成交價',t)


if __name__=='__main__':unittest.main()
