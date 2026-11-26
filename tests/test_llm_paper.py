"""Matched-model unit, integration, leakage, portfolio and paper-safety tests."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from research.llm_paper.common import protocol, instant, costs, ROOT, digest
from research.llm_paper.engine import Book
from research.llm_paper.llm import build_request, candidates_for_book,Provider
from research.llm_paper.capital_feed import normalize
from research.llm_paper.journal import Journal,checkpoint
from research.llm_paper.live import live_frame
from research.llm_paper.runtime import next_session

SESSIONS=["2026-11-24","2026-11-25","2026-11-26","2026-11-27","2026-11-30"]


def quote(at="2026-11-24T10:00:01+08:00",**changes):
    r=dict(symbol="2330",name="測試",asset_type="STOCK",source="CAPITAL_SKCOM",quote_basis="capital_intraday_odd_lot",volume_unit="shares",
           quote_timestamp=at,received_at=at,depth_received_at=at,quote_status="LIVE",is_trial=False,depth_status="ODDLOT_CALLBACK_SHARES",
           last_price=100,open=99,high=103,low=98,bid=99.9,ask=100,volume=10000,bid_size_shares=100,ask_size_shares=100,
           limit_up=110,limit_down=90,evidence_id="e")
    r.update(changes);return r


def order(side="BUY",qty=10,price=100.5):
    return dict(symbol="2330",decision=side,suggested_qty=qty,suggested_price=price,asset_type="STOCK",strategy_id="test",
                target_price=110,invalidation_price=98,holding_days=5,reason="test")


class MatchedBookTests(unittest.TestCase):
    def setUp(self):self.p=protocol();self.b=Book(self.p)

    def buy(self):
        self.b.submit([order()],"2026-11-24T10:00:00+08:00","buy")
        self.b.advance([quote()],"2026-11-24T10:00:01+08:00",SESSIONS,strict_depth=True)

    def test_no_same_observation_fill(self):
        self.b.submit([order()],"2026-11-24T10:00:01+08:00","buy")
        self.b.advance([quote()],"2026-11-24T10:00:01+08:00",SESSIONS,strict_depth=True)
        self.assertEqual(self.b.quantity("2330"),0)

    def test_fees_cash_and_reservation(self):
        self.buy()
        self.assertEqual(self.b.quantity("2330"),10)
        self.assertEqual(self.b.cash,98975)
        self.assertEqual(self.b.basis("2330"),1025)
        self.assertEqual(self.b.reserved(),0)

    def test_roundtrip_cost_and_t_plus_two_cash(self):
        self.buy()
        self.b.submit([order("SELL",10,108.5)],"2026-11-25T10:00:00+08:00","sell")
        self.b.advance([quote("2026-11-25T10:00:01+08:00",bid=110,ask=110.5,limit_up=120)],"2026-11-25T10:00:01+08:00",SESSIONS,strict_depth=True)
        self.assertEqual(self.b.cash,98975)
        self.assertEqual(self.b.quantity("2330"),0)
        self.assertEqual(self.b.closed_trades[0]["net_pnl_twd"],46)
        self.assertEqual(self.b.metrics()["closed_trades"],1)
        self.b.advance([],"2026-11-26T10:00:00+08:00",SESSIONS,strict_depth=True)
        self.assertEqual(self.b.cash,98975)
        self.b.advance([],"2026-11-27T10:00:00+08:00",SESSIONS,strict_depth=True)
        self.assertEqual(self.b.cash,100046)

    def test_no_same_day_oddlot_resale(self):
        self.buy();self.b.submit([order("SELL",10,99)],"2026-11-24T10:01:00+08:00","sell")
        self.assertEqual(self.b.orders[-1]["reason"],"NO_SAME_DAY_ODDLOT_RESALE")

    def test_partial_fills_charge_one_aggregated_commission(self):
        self.b.submit([order()],"2026-11-24T10:00:00+08:00","buy")
        self.b.advance([quote(ask_size_shares=4)],"2026-11-24T10:00:01+08:00",SESSIONS,strict_depth=True)
        self.assertEqual(self.b.quantity("2330"),4)
        self.b.advance([quote("2026-11-24T10:00:02+08:00",ask_size_shares=6)],"2026-11-24T10:00:02+08:00",SESSIONS,strict_depth=True)
        self.assertEqual(self.b.quantity("2330"),10)
        self.assertEqual(sum(float(f["commission_twd"]) for f in self.b.fills),20)
        self.assertEqual(self.b.cash,98975)

    def test_depth_not_reused_without_new_callback(self):
        self.b.submit([order()],"2026-11-24T10:00:00+08:00","buy")
        q=quote(ask_size_shares=4)
        self.b.advance([q],"2026-11-24T10:00:01+08:00",SESSIONS,strict_depth=True)
        self.b.advance([q],"2026-11-24T10:00:02+08:00",SESSIONS,strict_depth=True)
        self.assertEqual(self.b.quantity("2330"),4)

    def test_paper_required_and_no_shorting(self):
        with self.assertRaises(ValueError):Book(dict(self.p,mode="LIVE"))
        self.b.submit([order("SELL",10,99)],"2026-11-24T10:00:00+08:00","short")
        self.assertFalse(self.b.pending)

    def test_stale_future_trial_and_yahoo_cannot_fill(self):
        for changes in (dict(is_trial=True),dict(source="Yahoo"),dict(depth_status="UNKNOWN"),
                        dict(quote_timestamp="2026-11-24T10:01:00+08:00"),dict(ask_size_shares=None)):
            with self.subTest(changes=changes):
                b=Book(self.p);b.submit([order()],"2026-11-24T10:00:00+08:00","b")
                b.advance([quote(**changes)],"2026-11-24T10:00:01+08:00",SESSIONS,strict_depth=True)
                self.assertFalse(b.fills)

    def test_proxy_model_is_explicit_and_identical_between_books(self):
        a,b=Book(self.p),Book(self.p)
        for book in (a,b):
            book.submit([order()],"2026-11-24T10:00:00+08:00","same")
            book.advance([quote(ask_size_shares=None)],"2026-11-24T10:00:01+08:00",SESSIONS,strict_depth=False)
        self.assertEqual(a.state(),b.state())
        self.assertIn("PROXY",a.fills[0]["fill_model"])

    def test_rod_expires_and_does_not_fill_next_day(self):
        self.b.submit([order()],"2026-11-24T10:00:00+08:00","b")
        self.b.advance([quote("2026-11-25T10:00:00+08:00")],"2026-11-25T10:00:00+08:00",SESSIONS,strict_depth=False)
        self.assertFalse(self.b.fills);self.assertEqual(self.b.orders[0]["status"],"EXPIRED_ROD")

    def test_off_tick_and_aggregate_budget(self):
        self.b.submit([order(price=100.1)],"2026-11-24T10:00:00+08:00","b")
        self.assertEqual(self.b.orders[-1]["reason"],"OFF_TICK_LIMIT")
        self.b.submit([order(qty=999)],"2026-11-24T10:00:00+08:00","b2")
        self.assertEqual(self.b.orders[-1]["reason"],"CASH_OR_DAILY_CAP")

    def test_restore_preserves_cash_pending_and_partial_state(self):
        self.buy();restored=Book(self.p,self.b.state())
        self.assertEqual(restored.state(),self.b.state())


class ControlledPipelineTests(unittest.TestCase):
    def frame(self):
        q=quote("2026-11-24T10:00:00+08:00")
        c=dict(q,quant_rank=1,quant_score=1,eligible_strategies=[],strategy_evidence={},market_history=[],daily_bars=[],
               daily_history_status={},position=dict(ai_managed_qty=999),paper_position=dict(qty=999,cost_cents=1),analysis_source="TOP10")
        return dict(id="f",at="2026-11-24T10:00:00+08:00",quotes=[q],candidates=[c])

    def test_real_account_context_is_removed(self):
        c=candidates_for_book(self.frame(),Book(protocol()))[0]
        self.assertEqual(c["paper_position"]["qty"],0);self.assertEqual(c["max_sell_qty"],0)
        self.assertEqual(c["position"]["source"],"ISOLATED_PAPER_BOOK")

    def test_requests_share_model_market_capital_costs_schema_and_no_search(self):
        frame=self.frame();book=Book(protocol())
        c,a=build_request(frame,book,"正式策略","formal_baseline")
        _,b=build_request(frame,book,"正式策略","formal_with_skill")
        self.assertEqual(a["input"],b["input"]);self.assertEqual(a["model"],b["model"])
        self.assertEqual(a["text"],b["text"]);self.assertEqual(a["tools"],[])
        self.assertNotEqual(a["instructions"],b["instructions"])
        self.assertEqual(json.loads(a["input"])["trading_calendar"]["as_of_date"],"2026-11-24")

    def test_price_levels_do_not_use_future_daily_bar(self):
        f=self.frame();f["candidates"][0]["daily_bars"]=[dict(date="2026-11-25",high=1000,low=1,close=500,complete=True,source="TWSE_DAILY")]
        c=candidates_for_book(f,Book(protocol()))[0]
        self.assertFalse(any(x["price"]==1000 for x in c["price_levels"]))


class CapitalNormalizationTests(unittest.TestCase):
    def stock(self):
        names=("nStockIdx","sDecimal","sTypeNo","bstrMarketNo","bstrStockNo","nOpen","nHigh","nLow","nClose","nRef","nBid","nBc","nAsk","nAc","nTQty","nUp","nDown","nSimulate","nTradingDay","nTradingLotFlag","nDealTime")
        d={k:0 for k in names};d.update(sDecimal=2,bstrStockNo="2330",bstrStockName="test",nTradingLotFlag=1,nTradingDay=20261125,nDealTime=100000,nClose=10000,nBid=9990,nAsk=10000,nUp=11000,nDown=9000)
        return SimpleNamespace(**d)

    def test_actual_limits_and_oddlot_depth_shares(self):
        r=normalize(self.stock(),5,"2026-11-25T10:00:00+08:00",dict(market_number=5,bid=9990,ask=10000,bid_qty=12,ask_qty=13,is_trial=False),"2026-11-25T10:00:00+08:00")
        self.assertEqual(r["limit_up"],110);self.assertEqual(r["limit_down"],90)
        self.assertEqual(r["ask_size_shares"],13)
        self.assertEqual(r["quote_status"],"LAST_KNOWN")

    def test_mismatched_or_trial_depth_not_accepted(self):
        for d in (dict(market_number=0,bid=9990,ask=10000,bid_qty=1000,ask_qty=1000,is_trial=False),dict(market_number=5,bid=9990,ask=10000,bid_qty=1,ask_qty=1,is_trial=True)):
            self.assertIsNone(normalize(self.stock(),5,"2026-11-25T10:00:00+08:00",d)["ask_size_shares"])


class JournalAndForwardSafetyTests(unittest.TestCase):
    def setUp(self):
        (ROOT/"research-runs").mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/"research-runs")
        self.journal=Journal(Path(self.tmp.name)/"paper.sqlite3")

    def tearDown(self):self.tmp.cleanup()

    def test_append_only_hash_chain_and_duplicate_protection(self):
        import sqlite3
        self.journal.append("a",dict(x=1),event_id="first")
        self.journal.append("b",dict(x=2),event_id="second")
        self.assertEqual(self.journal.verify(),2)
        with self.assertRaises(sqlite3.IntegrityError):self.journal.append("a",dict(x=3),event_id="first")
        with self.journal.connect() as db:
            with self.assertRaises(sqlite3.IntegrityError):db.execute("DELETE FROM events")
        self.assertEqual(self.journal.latest("a"),dict(x=1))

    def test_journal_refuses_formal_execution_path(self):
        with self.assertRaises(ValueError):Journal(ROOT/"data/execution/live_execution.sqlite3")

    def test_bounded_checkpoint_preserves_nav_and_drawdown(self):
        p=protocol();b=Book(p)
        b.cash=90000;b.mark("2026-11-24T10:00:00+08:00")
        b.cash=95000;b.mark("2026-11-25T10:00:00+08:00")
        state=checkpoint(dict(a=b))["a"];restored=Book(p,state)
        self.assertEqual(len(state["equity"]),1)
        self.assertEqual(restored.metrics()["maximum_drawdown"],.1)
        self.assertEqual(restored.metrics()["net_return"],-.050000000000000044)

    def test_after_close_next_session_and_rule_effective_date(self):
        sources=dict(sessions=["2026-11-25","2026-11-26","2026-12-07"])
        self.assertEqual(next_session(sources,"2026-11-25T14:00:00+08:00"),"2026-11-26T09:10:00+08:00")
        self.assertEqual(next_session(sources,"2026-12-07T08:00:00+08:00"),"2026-12-07T09:00:00+08:00")

    def test_future_official_source_cannot_become_live_frame(self):
        sources=dict(calendar_valid_year=2026,sessions=SESSIONS,at="2027-01-01T00:00:00Z")
        with self.assertRaises(ValueError):live_frame(dict(quotes=[]),sources,{},"2026-11-24T10:00:00+08:00")

    def test_unverified_future_year_cannot_trade(self):
        sources=dict(calendar_valid_year=2026,sessions=["2027-01-04"],at="2026-11-25T00:00:00Z")
        with self.assertRaises(ValueError):live_frame(dict(quotes=[]),sources,{},"2027-01-04T10:00:00+08:00")

    def test_empty_timeout_message_is_preserved_and_accounted_as_failure(self):
        import threading
        provider=Provider.__new__(Provider)
        provider.output=Path(self.tmp.name)/"calls";provider.output.mkdir()
        provider.config=protocol();provider.lock=threading.Lock();provider.calls=0;provider.input_tokens=0;provider.records=[]
        provider.client=SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs:(_ for _ in ()).throw(TimeoutError())))
        provider._invoke=lambda request,folder:(_ for _ in ()).throw(TimeoutError())
        with self.assertRaises(TimeoutError):provider(dict(model=provider.config["model"],tools=[],input="{}"))
        result=json.loads((provider.output/"call-0001/result.json").read_text())
        self.assertEqual(result["error_type"],"TimeoutError")
        self.assertEqual(result["status"],"FAILED")
        self.assertNotIn("usage",result)
        self.assertEqual(provider.calls,1)

    def test_hard_model_deadline_terminates_only_owned_process(self):
        import subprocess
        provider=Provider.__new__(Provider);provider.config=protocol();provider.transport_file=Path(self.tmp.name)/"codex_cli.py"
        folder=Path(self.tmp.name);(folder/"request.json").write_text("{}")
        process=SimpleNamespace(pid=123456,wait=lambda **kwargs:(_ for _ in ()).throw(subprocess.TimeoutExpired('worker',150)))
        with patch("research.llm_paper.llm.subprocess.Popen",return_value=process),patch.object(provider,"_terminate_owned_process") as terminate:
            with self.assertRaisesRegex(TimeoutError,"MODEL_SUBPROCESS_WALL_TIMEOUT"):provider._invoke({},folder)
            terminate.assert_called_once_with(process)


if __name__=="__main__":unittest.main()
