"""Realistic manual workflow with fabricated fills in isolated temporary folders."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from support_scout.dashboard import DeskServer
from support_scout.manual_desk import save_draft,save_fill,desk_state,confirmation_id,historical_proposals
from support_scout.portfolio import load_fills
from tests.test_sizing import settings,report

NOW='2026-11-02T16:00:00+09:00'


class ManualWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        (self.root/'sizing_settings.json').write_text(json.dumps(settings()))
    def tearDown(self):self.tmp.cleanup()
    def draft(self):
        return save_draft(self.root,{'id':'reported-buy-0001','symbol':'9001','amount_jpy':20000,
            'reported_executed':True,'actual_price':6000},now='2026-11-02T15:00:00+09:00')
    def fill(self,draft,confirmed=True):
        return {'id':draft['fill_id'],'draft_id':draft['id'],'symbol':'9001','side':'BUY','quantity':3,'price':6000,
            'fees':0,'filled_at':'2026-11-02T14:00:00+09:00','confirmed':confirmed}

    def test_amount_only_report_reserves_cash_without_inventing_position_or_time(self):
        d=self.draft();s=desk_state(self.root,None,NOW)
        self.assertEqual(s['funding']['available_cash_estimate_jpy'],180000)
        self.assertEqual(s['portfolio']['positions'],{})
        self.assertEqual(s['drafts'][0]['actual_price'],6000)
        self.assertIsNone(d['quantity']);self.assertIsNone(d['filled_at'])

    def test_confirmation_replaces_reservation_and_is_idempotent(self):
        d=self.draft();value=self.fill(d)
        save_fill(self.root,value,now=NOW);save_fill(self.root,value,now=NOW)
        s=desk_state(self.root,None,NOW)
        self.assertEqual(s['funding']['available_cash_estimate_jpy'],182000)
        self.assertEqual(s['funding']['reserved_cash_jpy'],0)
        self.assertEqual(s['portfolio']['positions']['9001.T']['quantity'],3)
        self.assertEqual(len(s['fill_history']),1);self.assertEqual(s['drafts'],[])

    def test_unchecked_draft_stays_reserved_until_explicit_confirmation(self):
        d=self.draft();value=self.fill(d,False);save_fill(self.root,value,now=NOW)
        self.assertEqual(desk_state(self.root,None,NOW)['funding']['available_cash_estimate_jpy'],180000)
        value.update(id=confirmation_id(d['fill_id']),confirmed=True);save_fill(self.root,value,now=NOW)
        s=desk_state(self.root,None,NOW)
        self.assertEqual(s['funding']['available_cash_estimate_jpy'],182000)
        self.assertEqual(s['portfolio']['confirmed_fill_count'],1)
        self.assertTrue(s['fill_history'][0]['confirmed_later'])

    def test_live_budget_uses_current_cash_without_mutating_archived_research(self):
        d=self.draft();r=report(6);r['as_of']=NOW;before=deepcopy(r)
        s=desk_state(self.root,r,NOW)
        self.assertEqual(r,before)
        self.assertEqual(s['funding']['plans'][0]['suggested_amount_jpy'],0)
        self.assertEqual(s['funding']['plans'][0]['sizing_status'],'PENDING_EXECUTION_DETAILS_NO_ADD')
        self.assertLessEqual(s['funding']['proposed_total_jpy'],180000)

    def test_pending_report_is_visible_before_confirmation_cutoff(self):
        d=self.draft();save_fill(self.root,self.fill(d),now=NOW)
        old=desk_state(self.root,None,'2026-11-02T15:30:00+09:00')
        self.assertEqual(old['funding']['reserved_cash_jpy'],20000)
        self.assertEqual(old['portfolio']['positions'],{})

    def test_confirmation_cannot_change_symbol_and_clear_a_different_reservation(self):
        from support_scout.contracts import DataError
        d=self.draft();value=self.fill(d,False);save_fill(self.root,value,now=NOW)
        value.pop('draft_id');value.update(id=confirmation_id(d['fill_id']),symbol='9002',confirmed=True)
        with self.assertRaises(DataError):save_fill(self.root,value,now=NOW)
        self.assertEqual(desk_state(self.root,None,NOW)['funding']['reserved_cash_jpy'],20000)

    def test_unreported_sell_draft_does_not_invent_a_cash_amount(self):
        d=save_draft(self.root,{'symbol':'9001','amount_jpy':None,'reported_executed':False})
        self.assertIsNone(d['amount_jpy']);self.assertEqual(desk_state(self.root,None,NOW)['funding']['reserved_cash_jpy'],0)

    def test_old_buy_proposal_survives_new_watch_report(self):
        db=sqlite3.connect(self.root/'notifications.sqlite3')
        db.execute('CREATE TABLE events(id TEXT,as_of TEXT,status TEXT,payload TEXT)')
        plan={'suggested_amount_jpy':20000,'entry_reference':100,'invalidation_reference':98,'target_reference':102}
        for i,action in enumerate(['PROBE_BUY_REVIEW','WAIT']):
            db.execute('INSERT INTO events VALUES(?,?,?,?)',(str(i)*64,'2026-11-02T14:00:00+09:00','SENT',json.dumps({'symbol':'9001.T','action':action,'decision_brief':plan})))
        db.commit();db.close()
        saved=historical_proposals(self.root,{'as_of':NOW})
        self.assertEqual(len(saved),1);self.assertFalse(saved[0]['is_current'])

    def test_pending_buy_is_excluded_from_new_entry_search_and_given_zero_sizing(self):
        from support_scout.japan_demo import demo_bundle
        from support_scout.pipeline import run
        from support_scout.discovery import make_search_packet
        from support_scout.opportunity import load_policy
        b=demo_bundle();symbol=b['securities'][0]['symbol']
        b['securities'][0]['observation_blockers']=['USER_REPORTED_POSITION_DETAILS_PENDING']
        b['sizing_policy']=settings();b['opportunity_policy']=load_policy()
        b['cash_reservations']=[{'id':'pending-1','symbol':symbol,'amount_jpy':20000,'reported_at':b['as_of']}]
        r=run(b);item=r['securities'][0]
        self.assertIn('USER_REPORTED_POSITION_DETAILS_PENDING',item['context_blockers'])
        self.assertIn('USER_REPORTED_POSITION_DETAILS_PENDING',item['opportunity_decision']['reason_codes'])
        self.assertEqual(item['execution_plan']['suggested_amount_jpy'],0)
        p=make_search_packet(r,{'version':'fixture','symbols':[s['symbol'] for s in b['securities']]})
        self.assertNotIn(symbol,[c['symbol'] for c in p['core']['candidates']])
        p=make_search_packet(r,{'version':'fixture','symbols':[s['symbol'] for s in b['securities'] if s['symbol']!=symbol]})
        self.assertNotIn(symbol,[c['symbol'] for c in p['core']['candidates']])


class ManualWorkflowHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        (self.root/'sizing_settings.json').write_text(json.dumps(settings()))
        self.server=DeskServer(0,data_root=self.root,output_root=self.root/'reports')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join(2);self.tmp.cleanup()
    def post(self,path,value):
        req=Request(self.base+path,data=json.dumps(value).encode(),headers={'Origin':self.base,'Content-Type':'application/json','X-Desk-Token':self.server.token})
        with urlopen(req) as r:return json.load(r)
    def test_save_returns_updated_position_and_cash_immediately(self):
        d=self.post('/api/fill-drafts',{'symbol':'9001','amount_jpy':20000,'reported_executed':True})
        value={'id':d['fill_id'],'draft_id':d['id'],'symbol':'9001','side':'BUY','quantity':3,'price':6000,'fees':0,'filled_at':NOW,'confirmed':True}
        result=self.post('/api/fills',value)
        self.assertTrue(result['recorded']);self.assertIn('成功儲存',result['message'])
        self.assertEqual(result['manual_desk']['portfolio']['positions']['9001.T']['quantity'],3)
        self.assertEqual(result['manual_desk']['funding']['available_cash_estimate_jpy'],182000)
    def test_invalid_time_has_visible_error_and_does_not_write_a_fill(self):
        value={'id':'invalid-time-1','symbol':'9001','side':'BUY','quantity':3,'price':6000,'fees':0,'filled_at':'2026-11-02T14:00:00:00+09:00','confirmed':True}
        with self.assertRaises(HTTPError) as e:self.post('/api/fills',value)
        error=json.load(e.exception)
        self.assertIn('日期與時間',error['message']);self.assertEqual(load_fills(self.root/'manual_fills.json')['fills'],[])
        feedback=json.loads((self.root/'fill_feedback.json').read_text(encoding='utf-8'))
        self.assertEqual(feedback[-1]['status'],'REJECTED')

    def test_auxiliary_refresh_failure_does_not_claim_the_saved_fill_failed(self):
        from unittest.mock import patch
        value={'id':'saved-fill-0001','symbol':'9001','side':'BUY','quantity':3,'price':6000,'fees':0,'filled_at':NOW,'confirmed':True}
        with patch('support_scout.dashboard.desk_state',side_effect=RuntimeError('fixture refresh failure')):
            response=self.post('/api/fills',value)
        self.assertTrue(response['recorded']);self.assertIn('已成功保存',response['message'])
        self.assertEqual(len(load_fills(self.root/'manual_fills.json')['fills']),1)

    def test_lost_http_response_keeps_success_audit_and_the_confirmed_fill(self):
        from unittest.mock import patch
        from http.client import RemoteDisconnected
        value={'id':'lost-response-01','symbol':'9001','side':'BUY','quantity':3,'price':6000,'fees':0,'filled_at':NOW,'confirmed':True}
        with patch('support_scout.dashboard.Handler.reply',side_effect=BrokenPipeError):
            with self.assertRaises(RemoteDisconnected):self.post('/api/fills',value)
        self.assertEqual(len(load_fills(self.root/'manual_fills.json')['fills']),1)
        feedback=json.loads((self.root/'fill_feedback.json').read_text(encoding='utf-8'))
        self.assertEqual(feedback[-1]['status'],'SAVED')


if __name__=='__main__':unittest.main()
