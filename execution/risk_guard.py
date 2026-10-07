from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Mapping

from .models import AdviceIntent, Side


class RiskRejected(ValueError):
    """Raised when an advice intent must not become a broker order."""


def _d(value: Any, field: str, *, allow_none: bool = False) -> Decimal | None:
    if value is None or value == "":
        if allow_none:
            return None
        raise RiskRejected(f"{field} is required")
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise RiskRejected(f"{field} must be numeric: {value!r}") from exc


def _i(value: Any, field: str, *, allow_none: bool = False) -> int | None:
    if value is None or value == "":
        if allow_none:
            return None
        raise RiskRejected(f"{field} is required")
    try:
        return int(value)
    except Exception as exc:
        raise RiskRejected(f"{field} must be integer: {value!r}") from exc


@dataclass(frozen=True)
class RiskConfig:
    # Real execution should use fresh broker data only.
    require_quote_status: str = "LIVE"
    max_quote_age_seconds: Decimal = Decimal("120")

    # Leave strategy limits explicit and local to the execution layer.
    min_confidence_pct: Decimal = Decimal("0")
    max_order_twd: Decimal | None = None
    max_buy_qty: int | None = None

    # If True, BUY must have ask and SELL must have bid.
    require_executable_side_price: bool = True


@dataclass(frozen=True)
class PositionState:
    symbol: str
    user_qty: int = 0
    ai_managed_qty: int = 0


@dataclass(frozen=True)
class MarketQuote:
    symbol: str
    quote_status: str
    age_seconds: Decimal | None
    last_price: Decimal | None
    bid: Decimal | None
    ask: Decimal | None

    @classmethod
    def from_mapping(cls, symbol: str, row: Mapping[str, Any]) -> "MarketQuote":
        return cls(
            symbol=symbol.upper(),
            quote_status=str(
                row.get("quote_status", row.get("data_quality", "UNAVAILABLE"))
            ).strip().upper(),
            age_seconds=_d(
                row.get("age_seconds", row.get("quote_age_seconds")),
                f"{symbol}.age_seconds",
                allow_none=True,
            ),
            last_price=_d(
                row.get("price", row.get("last_price")),
                f"{symbol}.last_price",
                allow_none=True,
            ),
            bid=_d(row.get("bid"), f"{symbol}.bid", allow_none=True),
            ask=_d(row.get("ask"), f"{symbol}.ask", allow_none=True),
        )


@dataclass(frozen=True)
class OrderPlan:
    run_id: str
    decision_id: str
    symbol: str
    side: Side
    quantity: int
    limit_price: Decimal
    estimated_value_twd: Decimal
    confidence_pct: Decimal
    quote_age_seconds: Decimal | None
    quote_status: str
    reason: str

    # Audit trail: how sizing was selected.
    sizing_source: str


def quote_map_from_advice_payload(payload: Mapping[str, Any]) -> dict[str, MarketQuote]:
    """Build symbol->MarketQuote from advice_interface.py's market.quotes[]."""
    market = payload.get("market") or {}
    rows = market.get("quotes") or []
    if not isinstance(rows, list):
        raise RiskRejected("market.quotes must be a list")

    out: dict[str, MarketQuote] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("symbol", "")).strip().upper()
        if symbol:
            out[symbol] = MarketQuote.from_mapping(symbol, row)
    return out


def _validate_quote(intent: AdviceIntent, quote: MarketQuote, cfg: RiskConfig) -> None:
    if quote.symbol != intent.symbol:
        raise RiskRejected(
            f"{intent.symbol}: quote symbol mismatch ({quote.symbol})"
        )

    required = cfg.require_quote_status.strip().upper()
    if required and quote.quote_status != required:
        raise RiskRejected(
            f"{intent.symbol}: quote_status={quote.quote_status}, required={required}"
        )

    if quote.age_seconds is None:
        raise RiskRejected(f"{intent.symbol}: quote age is unavailable")
    if quote.age_seconds < 0:
        raise RiskRejected(f"{intent.symbol}: quote age cannot be negative")
    if quote.age_seconds > cfg.max_quote_age_seconds:
        raise RiskRejected(
            f"{intent.symbol}: quote too old "
            f"({quote.age_seconds}s > {cfg.max_quote_age_seconds}s)"
        )

    if intent.confidence_pct < cfg.min_confidence_pct:
        raise RiskRejected(
            f"{intent.symbol}: confidence {intent.confidence_pct}% "
            f"< minimum {cfg.min_confidence_pct}%"
        )


def _choose_limit_price(intent: AdviceIntent, quote: MarketQuote, cfg: RiskConfig) -> Decimal:
    if intent.side == Side.BUY:
        side_price = quote.ask
        field = "ask"
    else:
        side_price = quote.bid
        field = "bid"

    if side_price is not None and side_price > 0:
        return side_price

    if cfg.require_executable_side_price:
        raise RiskRejected(f"{intent.symbol}: {field} is unavailable")

    # Fallback only if explicitly configured.
    for candidate in (intent.suggested_price, intent.reference_price, quote.last_price):
        if candidate is not None and candidate > 0:
            return candidate

    raise RiskRejected(f"{intent.symbol}: no usable price")


def _buy_quantity(intent: AdviceIntent, price: Decimal, cfg: RiskConfig) -> tuple[int, str]:
    if intent.suggested_qty is not None and intent.suggested_qty > 0:
        qty = intent.suggested_qty
        source = "suggested_qty"
    elif intent.amount_twd is not None and intent.amount_twd > 0:
        qty = int((intent.amount_twd / price).to_integral_value(rounding=ROUND_FLOOR))
        source = "amount_twd"
    else:
        raise RiskRejected(
            f"{intent.symbol}: BUY requires suggested_qty or amount_twd"
        )

    if qty <= 0:
        raise RiskRejected(f"{intent.symbol}: BUY quantity resolved to 0")

    if cfg.max_buy_qty is not None and qty > cfg.max_buy_qty:
        raise RiskRejected(
            f"{intent.symbol}: BUY qty {qty} exceeds max_buy_qty={cfg.max_buy_qty}"
        )

    estimated = price * qty
    if cfg.max_order_twd is not None and estimated > cfg.max_order_twd:
        raise RiskRejected(
            f"{intent.symbol}: order value {estimated} exceeds "
            f"max_order_twd={cfg.max_order_twd}"
        )

    return qty, source


def _sell_quantity(
    intent: AdviceIntent,
    position: PositionState,
) -> tuple[int, str]:
    if position.ai_managed_qty < 0:
        raise RiskRejected(f"{intent.symbol}: ai_managed_qty cannot be negative")

    if position.ai_managed_qty == 0:
        raise RiskRejected(f"{intent.symbol}: no AI-managed shares available to sell")

    if intent.suggested_qty is not None and intent.suggested_qty > 0:
        qty = intent.suggested_qty
        source = "suggested_qty"
    elif intent.action_ratio > 0:
        qty = int(
            (Decimal(position.ai_managed_qty) * intent.action_ratio)
            .to_integral_value(rounding=ROUND_FLOOR)
        )
        source = "action_ratio"
    else:
        raise RiskRejected(
            f"{intent.symbol}: SELL requires suggested_qty or action_ratio"
        )

    if qty <= 0:
        raise RiskRejected(f"{intent.symbol}: SELL quantity resolved to 0")

    # Authoritative rule: user_qty is never available to the AI execution layer.
    if qty > position.ai_managed_qty:
        raise RiskRejected(
            f"{intent.symbol}: SELL qty {qty} exceeds "
            f"ai_managed_qty={position.ai_managed_qty}"
        )

    return qty, source


def build_order_plan(
    intent: AdviceIntent,
    *,
    quote: MarketQuote,
    position: PositionState | None = None,
    config: RiskConfig | None = None,
) -> OrderPlan:
    """Turn one AdviceIntent into a deterministic, broker-ready order plan.

    Still NO broker call.  capital_executor.py will consume OrderPlan later.
    """
    cfg = config or RiskConfig()
    _validate_quote(intent, quote, cfg)
    price = _choose_limit_price(intent, quote, cfg)

    if intent.side == Side.BUY:
        qty, sizing_source = _buy_quantity(intent, price, cfg)
    else:
        if position is None:
            raise RiskRejected(f"{intent.symbol}: SELL requires PositionState")
        if position.symbol.strip().upper() != intent.symbol:
            raise RiskRejected(
                f"{intent.symbol}: position symbol mismatch ({position.symbol})"
            )
        qty, sizing_source = _sell_quantity(intent, position)

    estimated = price * qty
    if cfg.max_order_twd is not None and estimated > cfg.max_order_twd:
        raise RiskRejected(
            f"{intent.symbol}: order value {estimated} exceeds "
            f"max_order_twd={cfg.max_order_twd}"
        )

    return OrderPlan(
        run_id=intent.run_id,
        decision_id=intent.decision_id,
        symbol=intent.symbol,
        side=intent.side,
        quantity=qty,
        limit_price=price,
        estimated_value_twd=estimated,
        confidence_pct=intent.confidence_pct,
        quote_age_seconds=quote.age_seconds,
        quote_status=quote.quote_status,
        reason=intent.reason,
        sizing_source=sizing_source,
    )
