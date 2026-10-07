from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import json
import uuid

from .account_guard import (
    cash_sellable_qty,
    realtime_cash_qty,
)
from .account_snapshot import capture_account_snapshot
from .capital_reply_session import (
    CapitalReplySession,
    build_live_readonly_session_from_project,
)
from .execution_store import ExecutionStore
from .models import Side
from .pipeline_hook import execute_after_advice_written


@dataclass(frozen=True)
class SyntheticSignalRequest:
    symbol: str
    side: Side
    qty: int
    price: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    confidence: float = 80.0
    bootstrap_ai_managed_qty: int = 0


def _run_id(side: Side) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"SYNTH-{side.value}-{stamp}-{uuid.uuid4().hex[:6].upper()}"


def _health_dict(session: CapitalReplySession) -> dict:
    h = session.ensure_ready()
    return {
        "ready": h.ready,
        "local_connected": h.local_connected,
        "reply_state": h.reply_state,
        "reply_connected": h.reply_connected,
        "account_selected": h.account_selected,
        "action": h.action,
        "message": h.message,
    }


def _build_payload(req: SyntheticSignalRequest, run_id: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    bid = req.bid if req.bid is not None else req.price
    ask = req.ask if req.ask is not None else req.price

    return {
        "interface_version": "1.0",
        "generated_at": now,
        "run_id": run_id,
        "run_status": "READY",
        "run_error": None,
        "llm_validation": "VALID",
        "llm_model": f"synthetic-llm-{req.side.value.lower()}-test",
        "real_order_sent": False,
        "market": {
            "quotes": [
                {
                    "symbol": req.symbol,
                    "source": "SYNTHETIC_LLM_TEST",
                    "quote_status": "LIVE",
                    "price": float(req.price),
                    "bid": float(bid),
                    "ask": float(ask),
                    "volume": None,
                    "exchange_time": now,
                    "received_at": now,
                    "callback_received_at": now,
                    "age_seconds": 0,
                    "retrieval_method": "synthetic_test_input",
                    "warnings": [],
                }
            ]
        },
        "market_view": f"Synthetic {req.side.value} signal for end-to-end execution test.",
        "recommendations": [
            {
                "decision_id": f"{run_id}:{req.symbol}",
                "symbol": req.symbol,
                "decision": req.side.value,
                "confidence": req.confidence,
                "reference_price": float(req.price),
                "suggested_price": float(ask if req.side == Side.BUY else bid),
                "suggested_qty": req.qty,
                "action_ratio": 0,
                "reason": f"Synthetic final-LLM {req.side.value} instruction.",
                "warnings": [],
                "analysis_source": f"SYNTHETIC_LLM_{req.side.value}_TEST",
                "ai_managed_qty": (
                    req.bootstrap_ai_managed_qty if req.side == Side.SELL else 0
                ),
                "quote_status": "LIVE",
                "quote_timestamp": now,
                "quote_age_seconds": 0,
                "evidence_ids": [f"synthetic-{req.side.value.lower()}-test"],
            }
        ],
    }


def run_synthetic_signal(
    req: SyntheticSignalRequest,
    *,
    project_root: str | Path,
    execute: bool,
    config_path: str | Path = r"config\execution_live.json",
    env_path: str | Path | None = None,
) -> dict:
    root = Path(project_root)
    symbol = req.symbol.strip().upper()

    if req.qty <= 0:
        raise ValueError("qty must be > 0")
    if req.price <= 0:
        raise ValueError("price must be > 0")
    if req.bootstrap_ai_managed_qty < 0:
        raise ValueError("bootstrap_ai_managed_qty must be >= 0")

    req = SyntheticSignalRequest(
        symbol=symbol,
        side=req.side,
        qty=req.qty,
        price=req.price,
        bid=req.bid,
        ask=req.ask,
        confidence=req.confidence,
        bootstrap_ai_managed_qty=req.bootstrap_ai_managed_qty,
    )

    run_id = _run_id(req.side)
    payload = _build_payload(req, run_id)

    out_dir = root / "data" / "analysis" / "synthetic_tests"
    out_dir.mkdir(parents=True, exist_ok=True)
    advice_path = out_dir / f"{run_id}.json"
    tmp = advice_path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(advice_path)

    report = {
        "run_id": run_id,
        "symbol": symbol,
        "side": req.side.value,
        "qty": req.qty,
        "price": str(req.price),
        "advice_file": str(advice_path),
        "execute_requested": execute,
        "session_health_before": None,
        "precheck": {},
        "execution": None,
    }

    if not execute:
        return report

    # ONE Capital session for the whole test process:
    # connect once -> health check -> account/inventory precheck ->
    # pass the SAME session into the production pipeline.
    session = build_live_readonly_session_from_project(
        root,
        env_path=env_path,
    )
    session.connect()
    report["session_health_before"] = _health_dict(session)

    store = ExecutionStore(
        root / "data" / "execution" / "execution.sqlite3"
    )

    # Capture account data ONCE and reuse the exact snapshot in production.
    # BUY test also asks for inventory so it can display CURRENT_HOLDING_QTY;
    # an inventory-query failure does NOT block a BUY if buying power is valid.
    account_snapshot = capture_account_snapshot(
        session,
        need_buying_power=(req.side == Side.BUY),
        need_inventory=True,
    )

    if req.side == Side.BUY:
        inv = account_snapshot.inventory or ()
        report["precheck"] = {
            "available_to_buy_twd": (
                str(account_snapshot.buying_power.available_to_buy_twd)
                if account_snapshot.buying_power is not None
                else None
            ),
            "buying_power_error": account_snapshot.buying_power_error,
            "current_holding_qty": (
                realtime_cash_qty(inv, symbol)
                if account_snapshot.inventory is not None
                else None
            ),
            "inventory_error": account_snapshot.inventory_error,
        }
    else:
        inv = account_snapshot.inventory or ()
        stored_ai_qty = store.get_ai_qty(symbol)
        effective_ai_qty = (
            stored_ai_qty
            if stored_ai_qty is not None
            else req.bootstrap_ai_managed_qty
        )
        broker_realtime = (
            realtime_cash_qty(inv, symbol)
            if account_snapshot.inventory is not None
            else None
        )
        broker_sellable = (
            cash_sellable_qty(inv, symbol)
            if account_snapshot.inventory is not None
            else None
        )
        report["precheck"] = {
            "broker_realtime_qty": broker_realtime,
            "broker_sellable_qty": broker_sellable,
            "inventory_error": account_snapshot.inventory_error,
            "stored_ai_managed_qty": stored_ai_qty,
            "effective_ai_managed_qty": effective_ai_qty,
            "would_allow_sell_by_qty": (
                broker_sellable is not None
                and broker_sellable >= req.qty
                and effective_ai_qty >= req.qty
            ),
        }

    config = Path(config_path)
    if not config.is_absolute():
        config = root / config

    # Critical point: REUSE the exact same connected session.
    execution_report = execute_after_advice_written(
        advice_path,
        project_root=root,
        config_path=config,
        env_path=env_path,
        session=session,
        account_snapshot=account_snapshot,
    )
    report["execution"] = execution_report
    report["session_health_after"] = _health_dict(session)
    return report
