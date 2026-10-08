from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import json
from typing import Any

from .account_guard import (
    AccountCheckError,
    cash_sellable_qty,
    query_buying_power,
    realtime_cash_qty,
)
from .account_snapshot import (
    ExecutionAccountSnapshot,
    capture_account_snapshot,
)
from .advice_adapter import load_advice_file
from .capital_executor import CapitalOddLotExecutor, SendResult
from .capital_reply_session import CapitalReplySession, build_live_readonly_session_from_project
from .execution_store import ExecutionStore
from .models import AdviceIntent, Side
from .risk_guard import (
    MarketQuote,
    PositionState,
    RiskConfig,
    RiskRejected,
    build_order_plan,
    quote_map_from_advice_payload,
)


@dataclass(frozen=True)
class LiveExecutionConfig:
    enabled: bool = False
    max_quote_age_seconds: Decimal = Decimal("120")
    min_confidence_pct: Decimal = Decimal("0")
    max_order_twd: Decimal | None = None
    max_buy_qty: int | None = 999
    buy_cash_buffer_rate: Decimal = Decimal("0.01")
    min_available_to_buy_twd: Decimal = Decimal("10000")
    min_buy_fee_reserve_twd: Decimal = Decimal("20")
    max_daily_buy_twd: Decimal = Decimal("20000")
    observe_seconds: float = 20.0

    @classmethod
    def load(cls, path: str | Path) -> "LiveExecutionConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))

        def dec(name, default=None):
            value = data.get(name, default)
            return None if value is None else Decimal(str(value))

        return cls(
            enabled=bool(data.get("enabled", False)),
            max_quote_age_seconds=dec("max_quote_age_seconds", 120),
            min_confidence_pct=dec("min_confidence_pct", 0),
            max_order_twd=dec("max_order_twd"),
            max_buy_qty=data.get("max_buy_qty", 999),
            buy_cash_buffer_rate=dec("buy_cash_buffer_rate", "0.01"),
            min_available_to_buy_twd=dec("min_available_to_buy_twd", 10000),
            min_buy_fee_reserve_twd=dec("min_buy_fee_reserve_twd", 20),
            max_daily_buy_twd=dec("max_daily_buy_twd", 20000),
            observe_seconds=float(data.get("observe_seconds", 20)),
        )

    def risk_config(self) -> RiskConfig:
        return RiskConfig(
            require_quote_status="LIVE",
            max_quote_age_seconds=self.max_quote_age_seconds,
            min_confidence_pct=self.min_confidence_pct,
            max_order_twd=self.max_order_twd,
            max_buy_qty=self.max_buy_qty,
            require_executable_side_price=True,
        )


def _runtime_age(timestamp: str | None) -> Decimal | None:
    if not timestamp:
        return None
    try:
        ts = datetime.fromisoformat(timestamp)
        if ts.tzinfo is None:
            return None
        return Decimal(
            str(max(0.0, (datetime.now(ts.tzinfo) - ts).total_seconds()))
        )
    except Exception:
        return None


def _refresh_quote(intent: AdviceIntent, quote: MarketQuote) -> MarketQuote:
    age = _runtime_age(intent.quote_timestamp)
    return MarketQuote(
        symbol=quote.symbol,
        quote_status=quote.quote_status,
        # A copied age in advice JSON cannot prove freshness at send time.
        # Missing/invalid timestamps must fail the risk guard.
        age_seconds=age,
        last_price=quote.last_price,
        bid=quote.bid,
        ask=quote.ask,
    )


def execute_advice_file(
    advice_path: str | Path,
    *,
    project_root: str | Path,
    config_path: str | Path,
    env_path: str | Path | None = None,
    session: CapitalReplySession | None = None,
    account_snapshot: ExecutionAccountSnapshot | None = None,
    execution_db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Execute every BUY/SELL intent in one final advice JSON sequentially.

    One failed action does not stop the others.
    Every broker send gets an immediate SUBMITTED event and then its own cross-check.
    """
    root = Path(project_root)
    config = LiveExecutionConfig.load(config_path)
    if (config.min_available_to_buy_twd < 0 or config.min_buy_fee_reserve_twd < 0
            or config.max_daily_buy_twd <= 0
            or config.buy_cash_buffer_rate < 0):
        raise ValueError("BUY cash guards must be nonnegative")

    report: dict[str, Any] = {
        "enabled": config.enabled,
        "advice_path": str(advice_path),
        "results": [],
    }
    if not config.enabled:
        report["status"] = "DISABLED"
        return report

    payload, intents = load_advice_file(advice_path)
    report["trade_intent_count"] = len(intents)

    if not intents:
        report["status"] = "NO_TRADE_ACTION"
        return report

    quotes = quote_map_from_advice_payload(payload)
    store = ExecutionStore(Path(execution_db_path) if execution_db_path else
                           root / "data" / "execution" / "live_execution.sqlite3",
                           must_exist=True)

    if session is None:
        session = build_live_readonly_session_from_project(root, env_path=env_path)
        session.connect()
    elif not session.connected:
        session.connect()

    broker = CapitalOddLotExecutor(session)
    risk_cfg = config.risk_config()

    # One broker account snapshot for this final advice batch.
    # If a caller (e.g. synthetic test) already performed the precheck,
    # reuse that exact snapshot instead of immediately querying the broker again.
    need_buying_power = any(x.side == Side.BUY for x in intents)
    need_inventory = any(x.side == Side.SELL for x in intents)

    if account_snapshot is None:
        account_snapshot = capture_account_snapshot(
            session,
            need_buying_power=need_buying_power,
            need_inventory=need_inventory,
        )

    report["account_snapshot"] = {
        "captured_at": account_snapshot.captured_at,
        "buying_power_available": account_snapshot.has_buying_power,
        "inventory_available": account_snapshot.has_inventory,
        "buying_power_error": account_snapshot.buying_power_error,
        "inventory_error": account_snapshot.inventory_error,
    }

    initial_buying_power = (
        account_snapshot.buying_power.available_to_buy_twd
        if account_snapshot.buying_power is not None
        else None
    )

    initial_sellable: dict[str, int] = {}
    inventory = account_snapshot.inventory or ()
    for intent in intents:
        if intent.side == Side.SELL:
            initial_sellable[intent.symbol] = cash_sellable_qty(
                inventory,
                intent.symbol,
            )

    reserved_buy_twd = Decimal("0")
    reserved_sell_qty: dict[str, int] = {}

    for intent in intents:
        item: dict[str, Any] = {
            "decision_id": intent.decision_id,
            "run_id": intent.run_id,
            "symbol": intent.symbol,
            "side": intent.side.value,
            "broker_order_sent": False,
            "crosscheck_performed": False,
        }

        if not store.claim(
            intent.decision_id,
            intent.run_id,
            intent.symbol,
            intent.side.value,
        ):
            item.update(
                status="SKIPPED_DUPLICATE",
                reason="decision_id already attempted",
            )
            report["results"].append(item)
            continue

        try:
            health = session.ensure_ready()
            item["session_health"] = {
                "ready": health.ready,
                "reply_state": health.reply_state,
                "reply_connected": health.reply_connected,
                "action": health.action,
            }

            quote = quotes.get(intent.symbol)
            if quote is None:
                raise RiskRejected(f"{intent.symbol}: no market quote")
            quote = _refresh_quote(intent, quote)

            if (intent.side == Side.SELL
                    and intent.symbol in store.pending_reconciliation_symbols(
                        exclude_decision_id=intent.decision_id)):
                raise AccountCheckError(
                    f"{intent.symbol}: earlier order may have late fills; "
                    "reconcile confirmed fills before another AI SELL"
                )

            existing = store.get_ai_qty(intent.symbol)
            if existing is None:
                # Advice may contain a user-entered, stale AI quantity. Only
                # bootstrap a pre-existing SELL holding when the independent
                # broker snapshot has at least that many cash shares. A BUY
                # starts from zero and adds only confirmed fills.
                seed_qty = 0
                if intent.side == Side.SELL:
                    if account_snapshot.inventory is None:
                        raise AccountCheckError("inventory unavailable for AI position bootstrap")
                    seed_qty = int(intent.ai_managed_qty or 0)
                    broker_qty = realtime_cash_qty(account_snapshot.inventory,
                                                   intent.symbol)
                    if seed_qty > broker_qty:
                        raise AccountCheckError(
                            f"{intent.symbol}: unverified AI seed {seed_qty} "
                            f"exceeds broker cash inventory {broker_qty}"
                        )
                ai_qty = store.bootstrap_ai_qty(
                    intent.symbol,
                    seed_qty,
                )
            else:
                ai_qty = existing
            if intent.side == Side.SELL and account_snapshot.inventory is not None:
                broker_qty = realtime_cash_qty(account_snapshot.inventory,
                                               intent.symbol)
                if ai_qty > broker_qty:
                    raise AccountCheckError(
                        f"{intent.symbol}: AI ledger {ai_qty} exceeds "
                        f"broker cash inventory {broker_qty}; reconcile first"
                    )

            position = PositionState(
                symbol=intent.symbol,
                user_qty=0,
                ai_managed_qty=ai_qty,
            )

            plan = build_order_plan(
                intent,
                quote=quote,
                position=position if intent.side == Side.SELL else None,
                config=risk_cfg,
            )

            reserved_this_order_twd = Decimal("0")

            if intent.side == Side.BUY:
                fee_reserve = max(
                    plan.estimated_value_twd * config.buy_cash_buffer_rate,
                    config.min_buy_fee_reserve_twd,
                )
                required = plan.estimated_value_twd + fee_reserve
                if initial_buying_power is None:
                    raise AccountCheckError(
                        "buying power unavailable: "
                        + (account_snapshot.buying_power_error or "unknown query failure")
                    )

                # Re-read broker buying power for every BUY. The local
                # reservation remains authoritative if broker balance updates
                # lag behind accepted orders; the smaller value wins.
                fresh_buying_power = query_buying_power(session)
                fresh_available = fresh_buying_power.available_to_buy_twd
                if fresh_available is None:
                    raise AccountCheckError("fresh broker buying power is unavailable")

                effective_available = min(
                    initial_buying_power - reserved_buy_twd,
                    fresh_available,
                )

                item["verified_buying_power_twd"] = str(initial_buying_power)
                item["fresh_broker_buying_power_twd"] = str(fresh_available)
                item["batch_reserved_buy_twd_before"] = str(reserved_buy_twd)
                item["effective_buying_power_twd"] = str(effective_available)
                item["required_twd_with_buffer"] = str(required)
                item["fee_reserve_twd"] = str(fee_reserve)
                item["min_available_to_buy_twd"] = str(config.min_available_to_buy_twd)
                daily_reserved = store.daily_reserved_buy_twd()
                item["daily_reserved_buy_twd_before"] = str(daily_reserved)
                item["max_daily_buy_twd"] = str(config.max_daily_buy_twd)

                if daily_reserved + required > config.max_daily_buy_twd:
                    raise AccountCheckError(
                        f"daily BUY cap: reserved={daily_reserved}, "
                        f"required={required}, max={config.max_daily_buy_twd}"
                    )

                if effective_available - required < config.min_available_to_buy_twd:
                    raise AccountCheckError(
                        f"BUY would cross minimum broker buying power: "
                        f"effective_available={effective_available}, "
                        f"required_with_buffer={required}, "
                        f"minimum={config.min_available_to_buy_twd}"
                    )
                reserved_this_order_twd = required

            else:
                if account_snapshot.inventory is None:
                    raise AccountCheckError(
                        "inventory unavailable: "
                        + (account_snapshot.inventory_error or "unknown query failure")
                    )

                broker_initial = initial_sellable.get(intent.symbol, 0)
                already_reserved = reserved_sell_qty.get(intent.symbol, 0)
                effective_sellable = max(0, broker_initial - already_reserved)

                item["broker_sellable_qty_snapshot"] = broker_initial
                item["batch_reserved_sell_qty_before"] = already_reserved
                item["effective_broker_sellable_qty"] = effective_sellable
                item["ai_managed_qty"] = ai_qty

                if effective_sellable <= 0:
                    raise AccountCheckError(
                        f"{intent.symbol}: target stock not found as available "
                        f"sellable cash inventory in this batch"
                    )
                if effective_sellable < plan.quantity:
                    raise AccountCheckError(
                        f"{intent.symbol}: effective broker sellable qty "
                        f"{effective_sellable} < requested {plan.quantity}"
                    )

            def on_submitted(sent: SendResult) -> None:
                item["broker_order_sent"] = True
                item["send_return_code"] = sent.return_code
                item["seq13"] = sent.seq13
                store.mark_submitted(
                    intent.decision_id,
                    {
                        "symbol": intent.symbol,
                        "side": intent.side.value,
                        "quantity": plan.quantity,
                        "limit_price": str(plan.limit_price),
                        "required_twd_with_buffer": str(reserved_this_order_twd),
                        "return_code": sent.return_code,
                        "seq13": sent.seq13,
                    },
                )

            store.mark_send_pending(intent.decision_id, {
                "symbol": plan.symbol, "side": plan.side.value,
                "quantity": plan.quantity, "limit_price": str(plan.limit_price),
                "required_twd_with_buffer": str(reserved_this_order_twd),
            }, daily_limit_twd=(config.max_daily_buy_twd if intent.side == Side.BUY
                                else None))
            item["send_attempted"] = True
            result = broker.send_limit_order(
                symbol=plan.symbol,
                side=plan.side,
                quantity=plan.quantity,
                price=plan.limit_price,
                observe_seconds=config.observe_seconds,
                on_submitted=on_submitted,
            )

            item.update(
                status=result.status.value,
                quantity=result.quantity,
                limit_price=str(result.price),
                send_return_code=result.send_return_code,
                seq13=result.seq13,
                filled_quantity=result.filled_quantity,
                broker_order_sent=result.broker_order_sent,
                send_attempted=result.send_attempted,
                crosscheck_performed=result.crosscheck_performed,
                crosscheck_status=result.crosscheck.status.value,
                accepted_event_count=len(result.crosscheck.accepted_rows),
                deal_event_count=len(result.crosscheck.fill_rows),
                cancel_event_count=len(result.crosscheck.cancel_rows),
                used_fallback=result.crosscheck.used_fallback,
                automatic_retry=False,
            )

            # Reserve the submitted quantity/value for the rest of this batch
            # even when final observation is ambiguous. This is deliberately
            # conservative and prevents duplicate spending/selling.
            if result.send_attempted and result.send_return_code in (0, None):
                if intent.side == Side.BUY:
                    reserved_buy_twd += reserved_this_order_twd
                else:
                    reserved_sell_qty[intent.symbol] = (
                        reserved_sell_qty.get(intent.symbol, 0)
                        + plan.quantity
                    )

            if result.changes_position:
                new_ai_qty = store.finish_with_fill(
                    intent.decision_id, result.status.value, item,
                    result.symbol, result.side, result.filled_quantity,
                )
                item["ai_managed_qty_after_fill"] = new_ai_qty
            else:
                store.finish(intent.decision_id, result.status.value, item)

        except (RiskRejected, AccountCheckError) as exc:
            item.update(
                status="SKIPPED_CHECK_FAILED",
                reason=str(exc),
                broker_order_sent=False,
                crosscheck_performed=False,
            )
            store.finish(
                intent.decision_id,
                "SKIPPED_CHECK_FAILED",
                item,
            )

        except Exception as exc:
            uncertain = bool(item.get("send_attempted"))
            item.update(
                status="UNCONFIRMED" if uncertain else "FAILED",
                reason=f"{type(exc).__name__}: {exc}",
                automatic_retry=False,
            )
            if uncertain and not item.get("broker_order_sent"):
                item["broker_order_sent"] = None
            try:
                store.finish(intent.decision_id, item["status"], item)
            except Exception:
                # Keep the durable SEND_PENDING/SUBMITTED marker for manual
                # reconciliation if the database is unavailable.
                pass

        report["results"].append(item)

    report["processed_count"] = len(report["results"])
    report["status"] = "COMPLETE"
    return report
