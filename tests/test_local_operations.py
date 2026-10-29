"""Loopback writes and notification delivery tests; never sends real mail."""
from copy import deepcopy
from pathlib import Path
import json
import os
import smtplib
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from support_scout.contracts import DataError, timestamp
from support_scout.dashboard import DeskServer
from support_scout.fixed_universe import load_fixed_universe
from support_scout.notifications import configure, enqueue_report, email_status, flush, after_report
from support_scout.japan_demo import demo_bundle
from support_scout.pipeline import run
from support_scout.discovery import make_search_packet
from support_scout.portfolio import snapshot
from support_scout.decision import attach_decisions


def report():
    return {'synthetic':False,'as_of':'2026-10-28T16:00:00+09:00',
        'desk':{'mode':'openai','selected_symbols':['6857.T']},'securities':[
            {'symbol':'6857.T','trade_decision':{'conclusion':'WAIT','summary':'等待確認','reason_codes':['OBSERVATION_ONLY']},
             'patterns':[{'pattern_id':'H-001','stage':'EARLY_WATCH'}]}]}


class FixedPoolTests(unittest.TestCase):
    def test_failed_stock_manual_actions_do_not_break_search_snapshot_identity(self):
        bundle=demo_bundle()
        bundle['portfolio_snapshot']=snapshot({'schema_version':1,'fills':[]},bundle['as_of'])
        result=run(bundle)
        self.assertEqual(result['securities'][-1]['data_status'],'REJECTED')
        self.assertIn('manual_action',result['securities'][-1])
        make_search_packet(result,{'universe_id':'test','symbols':[s['symbol'] for s in bundle['securities']]})
        attach_decisions(result,bundle)
        self.assertNotIn('SNAPSHOT_INTEGRITY_FAILED',result['securities'][0]['trade_decision']['reason_codes'])

    def test_rejected_source_is_a_data_gap_not_a_false_snapshot_mismatch(self):
        bundle=demo_bundle()
        bundle['observation_only']=True
        for row in bundle['securities']:row['observation_only']=True
        bundle['securities'][-1]['bars']=[]
        result=run(bundle)
        rejected=result['securities'][-1]
        self.assertEqual(rejected['data_status'],'REJECTED')
        self.assertNotIn('SNAPSHOT_INTEGRITY_FAILED',rejected['trade_decision']['reason_codes'])
        rejected['reasons'].append('fabricated alteration')
        attach_decisions(result,bundle)
        self.assertIn('SNAPSHOT_INTEGRITY_FAILED',rejected['trade_decision']['reason_codes'])

    def test_flexible_pool_cap_and_normalized_codes(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'pool.json'
            path.write_text(json.dumps({'version':'user-paypay-v1','max_symbols':20,'symbols':['6857','130a.t']}))
            u=load_fixed_universe(path,now=timestamp('2026-10-28T15:00:00+09:00'))
            self.assertEqual(u['symbols'],['6857.T','130A.T'])
            self.assertEqual(u['protocol'],'jp-fixed-universe-v1')
            self.assertEqual(u['source']['type'],'USER_CONFIGURED_FIXED_POOL')
            for rows in (['6857','6857.T'],[str(x) for x in range(1000,1021)],[]):
                path.write_text(json.dumps({'version':'bad','symbols':rows}))
                with self.assertRaises(DataError): load_fixed_universe(path)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.config,self.db=self.root/'email.json',self.root/'events.sqlite3'
        configure('test@example.com',self.config)
    def tearDown(self): self.temp.cleanup()
    def test_same_decision_is_not_requeued_but_transition_is(self):
        r=report()
        self.assertEqual(len(enqueue_report(r,self.root/'report.html',self.db)['queued']),1)
        self.assertEqual(enqueue_report(r,self.root/'report.html',self.db)['queued'],[])
        new=deepcopy(r);new['as_of']='2026-10-29T16:00:00+09:00';new['securities'][0]['patterns'][0]['stage']='SUPPORT_STRENGTHENED'
        self.assertEqual(len(enqueue_report(new,self.root/'report.html',self.db)['queued']),1)
        self.assertEqual(enqueue_report(r,self.root/'report.html',self.db)['queued'],[])
    def test_missing_auth_keeps_pending_and_never_calls_sender(self):
        enqueue_report(report(),self.root/'report.html',self.db)
        with patch.dict(os.environ,{},clear=True):
            result=flush(self.config,self.db,sender=lambda *a:self.fail('must not send'))
        self.assertEqual(result['sent'],0)
        self.assertEqual(result['status'],'NEEDS_GMAIL_APP_PASSWORD')
        self.assertEqual(result['counts']['PENDING'],1)
    def test_success_and_delivery_ambiguity_do_not_duplicate(self):
        enqueue_report(report(),self.root/'report.html',self.db)
        calls=[]
        with patch.dict(os.environ,{'JP_DESK_GMAIL_APP_PASSWORD':'test-secret'},clear=True):
            self.assertEqual(flush(self.config,self.db,sender=lambda *a:calls.append(a))['sent'],1)
            self.assertEqual(flush(self.config,self.db,sender=lambda *a:self.fail('duplicate'))['sent'],0)
            r=report();r['as_of']='2026-10-29T16:00:00+09:00';r['securities'][0]['trade_decision']['conclusion']='CONSIDER'
            enqueue_report(r,self.root/'report.html',self.db)
            def uncertain(*a): raise TimeoutError('private transport detail')
            self.assertEqual(flush(self.config,self.db,sender=uncertain)['status'],'ERROR')
            self.assertEqual(flush(self.config,self.db,sender=lambda *a:self.fail('uncertain duplicate'))['sent'],0)
            self.assertEqual(email_status(self.config,self.db)['counts']['UNKNOWN'],1)
        self.assertEqual(len(calls),1)
    def test_auth_failure_is_retryable_without_losing_event(self):
        enqueue_report(report(),self.root/'report.html',self.db)
        def denied(*a): raise smtplib.SMTPAuthenticationError(535,b'secret response')
        with patch.dict(os.environ,{'JP_DESK_GMAIL_APP_PASSWORD':'test'},clear=True):
            self.assertEqual(flush(self.config,self.db,sender=denied)['errors'],['GMAIL_AUTHENTICATION_REQUIRED'])
            self.assertEqual(flush(self.config,self.db,sender=lambda *a:None)['sent'],1)
    def test_synthetic_and_reconstruction_never_flush_mail(self):
        for mode in ('SYNTHETIC','RETROSPECTIVE_REAL_PRICE_CASE_RECONSTRUCTION'):
            r=report()
            if mode=='SYNTHETIC':r['synthetic']=True
            else:r['analysis_context']={'mode':mode}
            with patch('support_scout.notifications.flush',side_effect=AssertionError('case sent mail')):
                result=after_report(r,self.root,config_path=self.config,db_path=self.db)
            self.assertEqual(result['delivery']['sent'],0)
    def test_header_injection_rejected(self):
        with self.assertRaises(DataError):configure('a@example.com\r\nBcc:other@example.com',self.config)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.server=DeskServer(0,data_root=self.root,output_root=self.root/'reports')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2);self.temp.cleanup()
    def call(self,path,data=None,token=True):
        headers={}
        if data is not None:
            headers={'Content-Type':'application/json','Origin':self.base}
            if token:headers['X-Desk-Token']=self.server.token
        request=Request(self.base+path,data=json.dumps(data).encode() if data is not None else None,headers=headers)
        with urlopen(request,timeout=3) as response:return json.loads(response.read())
    def fill(self,confirmed):
        return {'id':'test-fill-0001','symbol':'6857','side':'BUY','quantity':100,'price':30000,'fees':0,
            'filled_at':'2026-10-28T16:00:00+09:00','confirmed':confirmed}
    def test_unchecked_fill_never_becomes_a_holding(self):
        self.assertFalse(self.call('/api/fills',self.fill(False))['counted_as_holding'])
        self.assertEqual(self.call('/api/state')['portfolio']['positions'],{})
    def test_confirmed_fill_is_shared_as_structured_position(self):
        self.call('/api/fills',self.fill(True))
        position=self.call('/api/state')['portfolio']['positions']['6857.T']
        self.assertEqual((position['quantity'],position['average_cost']),(100,30000))
        bad=self.fill(True);bad.update(id='test-sell-0002',side='SELL',quantity=200)
        with self.assertRaises(HTTPError) as error:self.call('/api/fills',bad)
        self.assertEqual(error.exception.code,400)
    def test_foreign_origin_and_missing_token_cannot_write(self):
        with self.assertRaises(HTTPError) as error:self.call('/api/fills',self.fill(True),token=False)
        self.assertEqual(error.exception.code,403)
        self.assertFalse((self.root/'manual_fills.json').exists())
        request=Request(self.base+'/api/state',headers={'Host':'evil.example'})
        with self.assertRaises(HTTPError) as error:urlopen(request)
        self.assertEqual(error.exception.code,403)
    def test_ui_has_confirmation_controls_and_no_arbitrary_file_route(self):
        with urlopen(self.base) as response:html=response.read().decode()
        self.assertIn('我確認已實際成交',html)
        self.assertIn(self.server.token,html)
        with self.assertRaises(HTTPError) as error:self.call('/.env')
        self.assertEqual(error.exception.code,404)


if __name__=='__main__': unittest.main()
