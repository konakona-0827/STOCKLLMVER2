import copy
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import threading
import io
from contextlib import redirect_stdout
from unittest.mock import patch

from opportunity_flow.common import canonical,digest,policy,save,stamp
from opportunity_flow.data import load_dataset,validate_quote,validate_reference
from opportunity_flow.demo import create
from opportunity_flow.engine import build,economics
from opportunity_flow.pipeline import run_cycle,execution_readiness
from opportunity_flow.review import combine,validate,verify_packet
from opportunity_flow.state import Ledger
from opportunity_flow.collector import parse_daily,master_table
from opportunity_flow.public_quotes import parse as parse_public
from opportunity_flow.__main__ import refresh_due


class FakeRunner:
    def __init__(self,verdict="RESEARCH",fail=False,invalid=False):
        self.calls=[]
        self.verdict,self.fail,self.invalid=verdict,fail,invalid

    def __call__(self,packets,role):
        self.calls.append((packets,role))
        if self.fail:
            raise RuntimeError("SIMULATED_QUOTA_EXHAUSTED")
        rows=[]
        for p in packets:
            row=dict(symbol=p["symbol"],packet_id=p["packet_id"],verdict=self.verdict,
                selected_route_id=p["selected_routes"][0] if self.verdict=="RESEARCH" else None,
                evidence_ids=["E_FEATURES","E_ECONOMICS"],thesis="已給定合格研究路徑。",
                counterevidence="失效價跌破則假說失效。",wait_conditions=["補齊即時零股報價及事件來源。"],
                execution_missing=["ODDLOT_QUOTE"])
            if self.invalid:
                row["evidence_ids"]=["INVENTED"]
            rows.append(row)
        return dict(answer=dict(role=role,opinions=rows),usage=dict(input_tokens=100,output_tokens=20),
                    elapsed_seconds=.01,prompt_characters=1000,model="test")


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.at=create(self.root/"dataset")
        self.ds=load_dataset(self.root/"dataset")
        self.p=policy()
        self.c=build("1111",self.ds,self.at,self.p)

    def tearDown(self):
        self.tmp.cleanup()

    def test_synthetic_positive_negative_and_gap_remain_visible(self):
        r=run_cycle(self.ds,self.at,self.p,self.root/"state")
        self.assertEqual(r["summary"]["symbols_observed"],3)
        self.assertEqual(r["summary"]["states"],dict(REVIEW_PENDING=1,DATA_BLOCKED=1,ECONOMIC_BLOCKED=1))
        self.assertEqual(r["summary"]["llm_calls"],0)
        self.assertEqual(r["summary"]["orders_sent"],0)

    def test_future_prices_do_not_change_features_routes_or_packet(self):
        ds=copy.deepcopy(self.ds)
        future={**ds["grouped"]["1111"][-1],"date":self.at[:10],"close":9000,"high":10000}
        ds["grouped"]["1111"].append(future)
        result=build("1111",ds,self.at,self.p)
        self.assertEqual(self.c["packet"],result["packet"])

    def test_missing_recent_session_is_not_silently_compressed(self):
        c=build("2222",self.ds,self.at,self.p)
        self.assertEqual(c["state"],"DATA_BLOCKED")
        self.assertIn("RECENT_HISTORY_GAP_OR_INVALID_BAR",c["reasons"])

    def test_calendar_cannot_be_reused_two_weeks_later(self):
        at=stamp(self.at)+timedelta(days=14)
        c=build("1111",self.ds,at,self.p)
        self.assertEqual(c["reasons"],["CALENDAR_COVERAGE_NOT_CURRENT"])

    def test_an_old_gap_does_not_create_a_false_adjacent_pivot(self):
        ds=copy.deepcopy(self.ds)
        missing=ds["grouped"]["1111"].pop(15)["date"]
        c=build("1111",ds,self.at,self.p)
        self.assertIn(missing,c["packet"]["evidence"][3]["facts"]["older_history_gaps"])
        for level in c["packet"]["evidence"][1]["facts"]["supports"]:
            self.assertGreater(level["date"],missing)

    def test_dataset_tampering_fails_hash(self):
        with (self.root/"dataset/bars.csv").open("a",encoding="utf-8") as f:
            f.write("tampered")
        with self.assertRaisesRegex(ValueError,"HASH_MISMATCH"):
            load_dataset(self.root/"dataset")

    def test_dataset_update_lock_is_visible(self):
        (self.root/"dataset/.collecting").write_text("updating")
        with self.assertRaisesRegex(ValueError,"UPDATE_IN_PROGRESS"):
            load_dataset(self.root/"dataset")

    def test_packet_tampering_is_refused(self):
        p=copy.deepcopy(self.c["packet"])
        p["recent_bars"][0]["close"]+=10
        with self.assertRaisesRegex(ValueError,"HASH_MISMATCH"):
            verify_packet(p)

    def test_horizon_longer_than_history_is_an_invalid_policy(self):
        p={**self.p,'holding_sessions':100}
        save(self.root/'bad_policy.json',p)
        with self.assertRaisesRegex(ValueError,'INVALID_POLICY'):
            policy(self.root/'bad_policy.json')

    def test_fees_tax_and_slippage_reduce_returns(self):
        e=economics(100,98,110,20,.2,self.p)
        self.assertLess(e["net_profit_twd"],200)
        self.assertGreater(e["modeled_loss_twd"],40)
        self.assertGreater(e["invested_twd"],2000)

    def test_all_eligible_routes_satisfy_frozen_economic_floors(self):
        self.assertEqual(self.c["state"],"REVIEW_PENDING")
        for r in self.c["packet"]["evidence"][2]["facts"]:
            if not r["eligible"]:
                continue
            e=r["economics"]
            self.assertLessEqual(e["modeled_loss_twd"],self.p["capital_twd"]*.005)
            self.assertLessEqual(e["invested_twd"],self.p["capital_twd"]*.5)
            self.assertGreaterEqual(e["net_return"],.02)
            self.assertGreaterEqual(e["reward_risk"],1.5)
            self.assertGreaterEqual(e["net_profit_twd"],50)

    def test_two_independent_reviews_receive_same_frozen_packet(self):
        fake=FakeRunner()
        r=run_cycle(self.ds,self.at,self.p,self.root/"state",runner=fake,use_llm=True)
        self.assertEqual([x[1] for x in fake.calls],["researcher","challenger"])
        self.assertEqual(fake.calls[0][0],fake.calls[1][0])
        self.assertNotIn("researcher",canonical(fake.calls[1][0]))
        c=next(c for c in r["candidates"] if c["symbol"]=="1111")
        self.assertEqual(c["state"],"RESEARCH_READY")
        self.assertEqual(c["execution"]["state"],"EXECUTION_DATA_NEEDED")

    def test_unchanged_facts_are_not_sent_to_model_again(self):
        fake=FakeRunner()
        run_cycle(self.ds,self.at,self.p,self.root/"state",runner=fake,use_llm=True)
        r=run_cycle(self.ds,stamp(self.at)+timedelta(seconds=60),self.p,self.root/"state",runner=fake,use_llm=True)
        self.assertEqual(len(fake.calls),2)
        self.assertEqual(r["summary"]["cached_theses"],1)
        self.assertEqual(r["summary"]["llm_calls"],0)

    def test_policy_change_requires_new_model_review(self):
        fake=FakeRunner()
        run_cycle(self.ds,self.at,self.p,self.root/"state",runner=fake,use_llm=True)
        p={**self.p,"minimum_net_profit_twd":51}
        run_cycle(self.ds,self.at,p,self.root/"state",runner=fake,use_llm=True)
        self.assertEqual(len(fake.calls),4)

    def test_quota_failure_is_an_error_not_market_wait(self):
        fake=FakeRunner(fail=True)
        r=run_cycle(self.ds,self.at,self.p,self.root/"state",runner=fake,use_llm=True)
        self.assertEqual(r["summary"]["states"].get("REVIEW_ERROR"),1)
        self.assertEqual(r["summary"]["usage_unknown_calls"],1)
        run_cycle(self.ds,stamp(self.at)+timedelta(seconds=60),self.p,self.root/"state",runner=fake,use_llm=True)
        self.assertEqual(len(fake.calls),1)

    def test_invalid_evidence_is_not_an_approved_opinion(self):
        fake=FakeRunner(invalid=True)
        r=run_cycle(self.ds,self.at,self.p,self.root/"state",runner=fake,use_llm=True)
        self.assertEqual(r["summary"]["states"].get("REVIEW_ERROR"),1)
        self.assertEqual(r["summary"]["usage"]["input_tokens"],100)

    def test_wait_requires_a_recheck_condition(self):
        p=self.c["packet"]
        answer=FakeRunner(verdict="WATCH")([p],"researcher")["answer"]
        answer["opinions"][0]["wait_conditions"]=[]
        with self.assertRaisesRegex(ValueError,"RECHECK_CONDITION"):
            validate(answer,[p],"researcher")

    def test_invented_route_cannot_override_python(self):
        p=self.c["packet"]
        answer=FakeRunner()([p],"researcher")["answer"]
        answer["opinions"][0]["selected_route_id"]="invented"
        with self.assertRaisesRegex(ValueError,"INELIGIBLE_ROUTE"):
            validate(answer,[p],"researcher")

    def test_independent_reject_is_preserved(self):
        p=self.c["packet"]
        a=FakeRunner()([p],"researcher")["answer"]["opinions"][0]
        b=FakeRunner(verdict="REJECT")([p],"challenger")["answer"]["opinions"][0]
        self.assertEqual(combine(p,a,b),("RESEARCH_REJECT",None))

    def test_quote_wrong_exchange_stale_trial_and_regular_lot_are_rejected(self):
        q=dict(symbol="1111",exchange="TWSE",quote_status="LIVE",is_trial=False,
            quote_basis="capital_intraday_odd_lot",quote_timestamp=self.at,bid=104,ask=104.5,last_price=104.5)
        self.assertIsNone(validate_quote(q,"1111","TWSE",self.at,120)[1])
        for changed in ({"exchange":"TPEX"},{"is_trial":True},{"quote_basis":"regular_lot"},
                        {"quote_timestamp":(stamp(self.at)-timedelta(seconds=121)).isoformat()}):
            self.assertIsNotNone(validate_quote({**q,**changed},"1111","TWSE",self.at,120)[1])

    def test_concurrent_processes_cannot_both_claim_same_model_work(self):
        a,b=Ledger(self.root/"state"),Ledger(self.root/"state")
        try:
            self.assertTrue(a.claim("same",1,self.p))
            self.assertFalse(b.claim("same",2,self.p))
        finally:
            a.close();b.close()

    def test_tpex_empty_managed_table_does_not_break_mainboard_parser(self):
        fields=["代號","開盤","最高","最低","收盤","成交股數","成交金額(元)"]
        payload=dict(date="20260929",stat="ok",tables=[dict(title="上櫃股票行情",fields=fields,
            data=[["1111","100","102","99","101","10000","1000000"]]),
            dict(title="管理股票",fields=fields,data=[])])
        rows,errors=parse_daily("TPEX",payload,"2026-09-29",{"1111":{}},dict(sha256="a",retrieved_at=self.at))
        self.assertEqual(rows[0]["volume"],10000)
        self.assertFalse(errors)

    def test_public_daily_wrong_date_is_not_silently_relabelled(self):
        with self.assertRaisesRegex(ValueError,"DATE_OR_STATUS"):
            parse_daily("TWSE",dict(date="20260928",stat="OK"),"2026-09-29",{}, {})

    def test_imports_do_not_load_legacy_account_or_broker_modules(self):
        import subprocess,sys
        result=subprocess.run([sys.executable,"-c",
            "import opportunity_flow.__main__,sys; assert not ({'config','capital','agent_decision','execution','dotenv','openai','agents'} & set(sys.modules))"],
            capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_public_reference_is_not_accepted_as_oddlot_evidence(self):
        q=dict(symbol="1111",exchange="TWSE",quote_status="PUBLIC_REFERENCE",is_trial=False,
            quote_basis="TWSE_MIS_BOARD_LOT_NOT_ODDLOT",quote_timestamp=self.at,last_price=104.2)
        self.assertIsNotNone(validate_quote(q,"1111","TWSE",self.at,120)[1])
        self.assertEqual(validate_reference(q,"1111","TWSE",self.at,120)["price"],104.2)

    def test_public_parser_uses_trade_time_not_later_feed_stamp(self):
        payload=dict(rtcode="0000",msgArray=[dict(ex="tse",c="1111",d="20260930",t="13:30:00",z="104.2",tlong="9999999999999")])
        quotes,errors=parse_public(payload,[("TWSE","1111")])
        self.assertEqual(quotes[0]["quote_timestamp"],"2026-09-30T13:30:00+08:00")
        self.assertFalse(errors)

    def test_public_invalid_price_is_counted_once(self):
        payload=dict(rtcode="0000",msgArray=[dict(ex="tse",c="1111",d="20260930",t="13:30:00",z="-")])
        quotes,errors=parse_public(payload,[("TWSE","1111")])
        self.assertFalse(quotes)
        self.assertEqual(len(errors),1)

    def test_intraday_quote_does_not_change_completed_features(self):
        q=dict(symbol="1111",exchange="TWSE",quote_status="PUBLIC_REFERENCE",is_trial=False,
            quote_basis="TWSE_MIS_BOARD_LOT_NOT_ODDLOT",quote_timestamp=self.at,last_price=105)
        c=build("1111",self.ds,self.at,self.p,q)
        self.assertEqual(c["packet"]["evidence"][0],self.c["packet"]["evidence"][0])
        self.assertTrue(c["hypothetical_reference"])
        self.assertEqual(c["packet"]["evidence"][3]["facts"]["price_reference_basis"],"PUBLIC_BOARD_LOT_REFERENCE")

    def test_review_expires_instead_of_living_forever(self):
        fake=FakeRunner()
        run_cycle(self.ds,self.at,self.p,self.root/"state",runner=fake,use_llm=True)
        run_cycle(self.ds,stamp(self.at)+timedelta(seconds=14401),self.p,self.root/"state",runner=fake,use_llm=True)
        self.assertEqual(len(fake.calls),4)

    def test_daily_refresh_is_checked_at_open_and_after_close(self):
        self.assertEqual(refresh_due(stamp("2026-11-12T08:30:00+08:00"),{"end":"2026-11-10"}),"2026-11-11")
        self.assertEqual(refresh_due(stamp("2026-11-12T18:00:00+08:00"),{"end":"2026-11-11"}),"2026-11-12")
        self.assertIsNone(refresh_due(stamp("2026-11-12T17:00:00+08:00"),{"end":"2026-11-11"}))

    def test_crossing_a_named_level_triggers_review_below_point_three_percent(self):
        level=self.c["packet"]["evidence"][0]["facts"]["ma5"]
        def candidate(price):
            q=dict(symbol="1111",exchange="TWSE",quote_status="PUBLIC_REFERENCE",is_trial=False,
                quote_basis="TWSE_MIS_BOARD_LOT_NOT_ODDLOT",quote_timestamp=self.at,last_price=price)
            return build("1111",self.ds,self.at,self.p,q)
        a,b=candidate(level-.001),candidate(level+.001)
        self.assertEqual(a["packet"]["trigger_states"]["ma5"],"BELOW")
        self.assertEqual(b["packet"]["trigger_states"]["ma5"],"ABOVE")
        self.assertNotEqual(a["material_key"],b["material_key"])

    def test_slow_review_does_not_block_watch_polling_or_overwrite_latest(self):
        from opportunity_flow.__main__ import main
        started=threading.Event()
        class SlowRunner(FakeRunner):
            def __init__(self,*args,stop_event=None,**kwargs):
                super().__init__();self.stop=stop_event
            def __call__(self,packets,role):
                started.set()
                if not self.stop.wait(2):
                    raise RuntimeError("test model should have been interrupted")
                raise RuntimeError("CODEX_REVIEW_STOPPED")
        def poll(_):
            self.assertTrue(started.wait(1),"model worker should be running while polling")
            raise KeyboardInterrupt()
        with patch('opportunity_flow.__main__.datetime') as clock,patch('opportunity_flow.review.CodexRunner',SlowRunner),\
             patch('opportunity_flow.__main__.time.sleep',side_effect=poll),redirect_stdout(io.StringIO()):
            clock.now.return_value=stamp(self.at)
            main(['watch','--dataset',str(self.root/'dataset'),'--state',str(self.root/'watch'),'--llm','--poll-seconds','1'])
        latest=json.loads((self.root/'watch/latest.json').read_text(encoding='utf-8'))
        self.assertEqual(latest['summary']['llm_calls'],0)
        self.assertEqual(len(list((self.root/'watch/cycles').iterdir())),2)

    def test_spread_change_cannot_relax_reviewed_plan_economics(self):
        p=copy.deepcopy(self.c['packet'])
        route=next(r for r in p['evidence'][2]['facts'] if r['eligible'])
        q=dict(bid=route['entry']-.8,ask=route['entry'],quote_timestamp=self.at)
        c={**self.c,'quote':q}
        ready=execution_readiness(c,p,route['route_id'],self.at,None,self.p)
        self.assertIn('FROZEN_PLAN_ECONOMICS_CHANGED_REQUIRES_REVIEW',ready['missing'])

    def test_bid_below_stop_is_invalidated_before_economic_arithmetic(self):
        p=copy.deepcopy(self.c['packet'])
        route=next(r for r in p['evidence'][2]['facts'] if r['eligible'])
        q=dict(bid=1,ask=route['entry'],quote_timestamp=self.at)
        ready=execution_readiness({**self.c,'quote':q},p,route['route_id'],self.at,None,self.p)
        self.assertEqual(ready['state'],'INVALIDATED')


if __name__=="__main__":
    unittest.main()
