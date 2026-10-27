from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from .advice_adapter import load_advice_file
from .capital_executor import CapitalOddLotExecutor, RealOrderResult
from .capital_reply_session import CapitalReplySession
from .models import AdviceIntent, Side
from .risk_guard import (
    MarketQuote,
    OrderPlan,
    PositionState,
    RiskConfig,
    RiskRejected,
    build_order_plan,
    quote_map_from_advice_payload,
)


class AdviceSellRunnerError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlannedSell:
    intent: AdviceIntent
    plan: OrderPlan


def _runtime_quote_age_seconds(timestamp: str | None) -> Decimal | None:
    """Recompute age NOW; never trust only the age captured when JSON was written."""
    if not timestamp:
        return None
    try:
        ts = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        return None
    now = datetime.now(ts.tzinfo)
    return Decimal(str(max(0.0, (now - ts).total_seconds())))


def _refresh_quote_age(intent: AdviceIntent, quote: MarketQuote) -> MarketQuote:
    runtime_age = _runtime_quote_age_seconds(intent.quote_timestamp)
    if runtime_age is None:
        # If timestamp cannot be independently verified, keep the existing field.
        runtime_age = quote.age_seconds
    return MarketQuote(
        symbol=quote.symbol,
        quote_status=quote.quote_status,
        age_seconds=runtime_age,
        last_price=quote.last_price,
        bid=quote.bid,
        ask=quote.ask,
    )


def plan_sell_orders_from_advice(
    advice_payload: Mapping[str, Any],
    intents: list[AdviceIntent],
    *,
    config: RiskConfig | None = None,
) -> list[PlannedSell]:
    """Create SELL-only OrderPlans from the stable advice handoff.

    BUY recommendations are ignored by this runner.
    SELL may use only ai_managed_qty supplied by the advice interface.
    """
    quotes = quote_map_from_advice_payload(advice_payload)
    cfg = config or RiskConfig()

    planned: list[PlannedSell] = []
    for intent in intents:
        if intent.side != Side.SELL:
            continue

        quote = quotes.get(intent.symbol)
        if quote is None:
            raise RiskRejected(f"{intent.symbol}: market quote not found in advice payload")
        quote = _refresh_quote_age(intent, quote)

        ai_qty = intent.ai_managed_qty or 0
        position = PositionState(
            symbol=intent.symbol,
            user_qty=0,              # execution layer never assumes user shares are sellable
            ai_managed_qty=ai_qty,
        )

        plan = build_order_plan(
            intent,
            quote=quote,
            position=position,
            config=cfg,
        )
        planned.append(PlannedSell(intent=intent, plan=plan))

    return planned


def load_and_plan_sell_orders(
    path: str | Path,
    *,
    config: RiskConfig | None = None,
) -> tuple[dict[str, Any], list[PlannedSell]]:
    payload, intents = load_advice_file(path)
    return payload, plan_sell_orders_from_advice(payload, intents, config=config)


def execute_one_planned_sell(
    planned: PlannedSell,
    *,
    session: CapitalReplySession,
    observe_seconds: float = 20.0,
) -> RealOrderResult:
    """Execute exactly one already-risk-checked SELL plan."""
    executor = CapitalOddLotExecutor(session)
    return executor.send_limit_order(
        symbol=planned.plan.symbol,
        side=Side.SELL,
        quantity=planned.plan.quantity,
        price=planned.plan.limit_price,
        observe_seconds=observe_seconds,
    )
