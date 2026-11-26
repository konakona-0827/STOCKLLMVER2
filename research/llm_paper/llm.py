"""Use the production request builder, validator and independent trade reviewer."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import threading
import time
import os
import signal
import subprocess
import sys
from unittest.mock import patch

from .common import ROOT, decimal, instant, costs, digest, save, stamp


class Provider:
    def __init__(self, transport, output, config):
        self.output, self.config = Path(output), config
        self.output.mkdir(parents=True, exist_ok=True)
        transport = Path(transport)
        spec = importlib.util.spec_from_file_location("formal_paper_cli", transport)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if module.metadata().get("api_fallback") is not False or module.metadata().get("billing_source") != "chatgpt-plan":
            raise ValueError("PINNED_CHATGPT_ONLY_TRANSPORT_REQUIRED")
        self.client = module.CodexClient(timeout=config["transport_timeout_seconds"])
        self.transport_file=transport.resolve()
        self.lock, self.calls, self.input_tokens = threading.Lock(), 0, 0
        self.records = []

    def _terminate_owned_process(self,process):
        if os.name=="nt":
            try:
                subprocess.run(["taskkill","/PID",str(process.pid),"/T","/F"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                               timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
            except (subprocess.TimeoutExpired,OSError):process.kill()
        else:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill()

    def _invoke(self,request,folder):
        result_path=folder/"transport-result.json"
        env={k:v for k,v in os.environ.items() if not k.upper().startswith(("CAPITAL_","OPENAI_","AZURE_OPENAI_","CODEX_API_","CODEX_ACCESS_TOKEN"))}
        env.pop("CODEX_MODEL",None)
        options=dict(cwd=ROOT,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL)
        if os.name=="nt":options["creationflags"]=subprocess.CREATE_NO_WINDOW
        else:options["start_new_session"]=True
        command=[sys.executable,"-X","utf8","-m","research.llm_paper.model_worker","--transport",str(self.transport_file),
                 "--request",str((folder/"request.json").resolve()),"--result",str(result_path.resolve()),"--timeout",str(self.config["transport_timeout_seconds"])]
        # No stdout pipe: descendants cannot keep communicate() waiting forever.
        with (folder/"worker.stderr.txt").open("wb") as errors:
            process=subprocess.Popen(command,stderr=errors,**options)
            try:code=process.wait(timeout=self.config["transport_timeout_seconds"])
            except subprocess.TimeoutExpired:
                self._terminate_owned_process(process)
                raise TimeoutError("MODEL_SUBPROCESS_WALL_TIMEOUT") from None
        if code or not result_path.exists():
            diagnostic=(folder/"worker.stderr.txt").read_text(encoding="utf-8",errors="replace")
            for label in ("CODEX_QUOTA_EXHAUSTED","CODEX_CHATGPT_LOGIN_REQUIRED","CODEX_MODEL_UNAVAILABLE"):
                if label in diagnostic:raise RuntimeError(label+"_NO_FALLBACK")
            raise RuntimeError("MODEL_WORKER_FAILED_NO_FALLBACK")
        return json.loads(result_path.read_text(encoding="utf-8"))

    def __call__(self, request):
        if request.get("tools") or request["model"] != self.config["model"]:
            raise ValueError("UNCONTROLLED_MODEL_OR_TOOLS")
        with self.lock:
            if self.calls >= self.config["maximum_model_calls_replay"] or self.input_tokens >= self.config["maximum_input_tokens_replay"]:
                raise RuntimeError("MODEL_BUDGET_EXHAUSTED_NO_FALLBACK")
            self.calls += 1
            number = self.calls
        folder = self.output / f"call-{number:04d}"
        folder.mkdir(exist_ok=False)
        save(folder / "request.json", request)
        started = time.monotonic()
        record = dict(number=number, request_sha256=digest(request), model=request["model"], status="FAILED", started_at=stamp())
        try:
            response = self._invoke(request,folder)
            if response["model"] != self.config["model"]:
                raise ValueError("MODEL_OVERRIDE_REFUSED")
            usage = response["usage"]
            record.update(status="COMPLETE", usage=usage, raw=response["raw"], response_id=response["response_id"],
                          elapsed_seconds=time.monotonic()-started, billing_source="chatgpt-plan", api_billed_usd=0,
                          plan_credit_cost_unknown=True)
            with self.lock:
                self.input_tokens += usage["input_tokens"]
            return response["raw"], dict(model=response["model"], usage=usage, response_id=response["response_id"],
                       elapsed_seconds=record["elapsed_seconds"], billing_source="chatgpt-plan", api_billed_usd=0)
        except Exception as exc:
            message=str(exc).splitlines()
            record.update(error_type=type(exc).__name__,error=(message[0] if message else type(exc).__name__)[:180],elapsed_seconds=time.monotonic()-started)
            raise
        finally:
            save(folder / "result.json", record)
            with self.lock:
                self.records.append(record)


def candidates_for_book(frame, book):
    candidates = [deepcopy(c) for c in frame["candidates"] if c.get("quant_rank") is not None or str(c.get("analysis_source", "")).startswith("TOP10") or c["symbol"] in book.positions]
    # Held symbols that left the selected pool are retained when the common
    # quote tape and previously received evidence exist. No other arm's account
    # is leaked into this arm's input.
    symbols = {c["symbol"] for c in candidates}
    qmap = {q["symbol"]:q for q in frame["quotes"]}
    for symbol in book.positions:
        if symbol not in symbols and symbol in qmap:
            q = qmap[symbol]
            candidates.append(dict(q, eligible_strategies=[], strategy_evidence={}, daily_bars=[],
                daily_history_status=dict(status="NOT_IN_CURRENT_FROZEN_SELECTION"), market_history=[], warnings=[]))
    day = frame["at"][:10]
    available = book.available(day)
    for c in candidates:
        symbol = c["symbol"]
        c["analysis_source"] = "FROZEN_QUANT_SELECTION_OR_THIS_PAPER_POSITION"
        qty, basis = book.quantity(symbol), book.basis(symbol)
        cost = basis / qty if qty else None
        c["position"] = dict(ai_managed_qty=qty, manual_qty=0, ai_average_cost=None if cost is None else float(cost), source="ISOLATED_PAPER_BOOK")
        c["paper_position"] = dict(symbol=symbol, qty=qty, cost_cents=int(basis*100))
        c["ai_cost_basis"] = dict(available=bool(qty), average_unit_cost_twd=None if cost is None else str(cost), quantity=qty,
                                  estimated=False, excludes_fees_and_tax=False, source="SIMULATED_FILLS_INCLUDING_COMMISSION")
        c["ai_trade_dates"] = dict(timezone="Asia/Taipei", records=[dict(side=x["side"], date=x["at"][:10], quantity=x["qty"], date_basis="PAPER_SIMULATED_FILL_DATE") for x in book.fills if x["symbol"]==symbol][-24:])
        c["buy_plans"] = dict(available=bool(qty), lots=deepcopy(book.positions.get(symbol,{}).get("lots",[])))
        c["max_sell_qty"] = max(0, book.sellable(symbol,day) - sum(x["remaining_qty"] for x in book.pending if x["symbol"]==symbol and x["side"]=="SELL"))
        c["sellable_context"] = dict(source="PAPER_T_PLUS_ONE_ODDLOT_SELLABILITY", ai_sellable_limit=c["max_sell_qty"])
        price = c.get("ask") or c.get("last_price")
        cap = 0
        if price and c.get("quote_status")=="LIVE" and c.get("is_trial") is False:
            cost_model = costs(book.config,c.get("asset_type","STOCK"))
            cap = min(book.config["max_order_quantity"],int(available/decimal(price)))
            while cap and book.funding_required(price,cap,c.get('asset_type','STOCK')) > available:
                cap-=1
        if len(book.positions)>=book.config["max_positions"] and symbol not in book.positions or any(o["symbol"]==symbol for o in book.pending):
            cap=0
        if c.get("paper_buy_allowed") is False:
            cap=0
        c.update(max_buy_qty=cap,budget_price=price)
        from pretrade_policy import price_levels
        c["price_levels"] = price_levels(c)
    return candidates


def build_request(frame, book, strategy, arm):
    import position_analyzer
    candidates = candidates_for_book(frame,book)
    day = frame["at"][:10]
    available = book.available(day)
    account = dict(source="ISOLATED_PAIRED_PAPER_REPLAY", capital_limit_twd=float(decimal(book.config.get('strategy_cap_twd',book.config["initial_cash_twd"]))),
                   available_cash_twd=float(available), available_cash_cents=int(available*100), positions=list(book.positions))
    with patch.object(position_analyzer,"now",return_value=instant(frame["at"])), patch.object(position_analyzer,"enable_search",side_effect=lambda request:request):
        request=position_analyzer.build_request(candidates,account,strategy,book.config["model"])
    payload=json.loads(request["input"])
    payload["paper_simulation_rules"].update(commission_rate=book.config["commission_rate"], minimum_commission_twd=book.config["minimum_commission_twd"],
        commission_rounding="CEILING_TWD_PER_ORDER_AGGREGATED_PARTIALS",sell_tax_stock=book.config["sell_tax_stock"],sell_tax_etf=book.config["sell_tax_etf"],
        slippage_bps=book.config["slippage_bps"],fill_price="NEXT_POST_DECISION_CROSSING_BID_ASK_WITH_ADVERSE_SLIPPAGE",max_positions=book.config["max_positions"])
    if book.config.get('funding_mode')=='LATEST_BRANCH_CUMULATIVE':
        payload['paper_simulation_rules'].update({key:book.config[key] for key in ('funding_mode','strategy_cap_twd','min_available_to_buy_twd',
            'max_daily_buy_twd','max_order_twd','buy_cash_buffer_rate','min_buy_fee_reserve_twd')})
        payload['paper_simulation_rules']['funding_formula']='min(wallet_cash-floor-pending, AI_cap-committed-pending, daily_cap-accepted_BUY_reserves); new BUY reserve=gross+max(gross*buffer_rate,min_fee_reserve), not commission'
    payload["replay_scope"]=dict(as_of=frame["at"], historical_web_search_disabled=True, today_news_prohibited=True,
                                legacy_real_account_context_removed=True, exogenous_frame_id=frame["id"])
    request["input"]=json.dumps(payload,ensure_ascii=False,allow_nan=False)
    request["tools"]=[]
    request["instructions"] += "\n本輪為隔離 Paper 實驗。只能使用提供的凍結資料與本 Paper 帳戶，不能搜尋今天新聞或沿用舊真實持倉。成本及成交模型以 paper_simulation_rules 為準，沒有新聞時明確保留未知。"
    request["instructions"] += f"\nWAIT/HOLD 的 reason 請控制在 {book.config['non_action_reason_max_chars']} 個中文字內，保留主要依據與反證；BUY/SELL 仍完整保留原策略要求的計算與證據。"
    if arm=="formal_with_skill":
        request["instructions"] += "\n\n"+(ROOT/"skills/tw-paper-decision/SKILL.md").read_text(encoding="utf-8")
    elif arm!="formal_baseline":
        raise ValueError("UNKNOWN_ARM")
    return candidates, request


def decide(candidates, request, provider):
    import position_analyzer
    started=time.monotonic()
    result, meta=position_analyzer.analyze(candidates,request,provider)
    if meta["validation"]!="VALID":
        raise RuntimeError("FORMAL_PIPELINE_FAILED: "+str((meta.get("api") or {}).get("error","UNKNOWN")))
    return dict(decision=result,meta=meta,elapsed_seconds=time.monotonic()-started)
