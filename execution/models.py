from __future__ import annotations

from dataclasses import dataclass, field, asdict
from decimal import Decimal
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ExecutionStatus(str, Enum):
    RECEIVED = "RECEIVED"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    READY_TO_SEND = "READY_TO_SEND"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    UNCONFIRMED = "UNCONFIRMED"


@dataclass(frozen=True)
class AdviceIntent:
    """Normalized handoff from advice_interface v1.0 into the execution layer.

    This object is NOT yet an order.  It still must pass risk_guard.py.
    """
    run_id: str
    decision_id: str
    interface_version: str
    symbol: str
    side: Side

    # Advice information
    confidence_pct: Decimal
    reason: str
    analysis_source: str | None
    warnings: tuple[str, ...]

    # Market / timing information copied from the stable interface
    quote_status: str
    quote_timestamp: str | None
    quote_age_seconds: Decimal | None
    reference_price: Decimal | None
    suggested_price: Decimal | None

    # Sizing hints from upstream. risk_guard.py remains authoritative.
    suggested_qty: int | None
    action_ratio: Decimal
    ai_managed_qty: int | None

    # Optional future fields. Current advice_interface.py may not provide them.
    evidence_ids: tuple[str, ...] = ()
    amount_twd: Decimal | None = None


@dataclass
class ExecutionResult:
    execution_id: str
    run_id: str
    decision_id: str
    symbol: str
    side: Side
    status: ExecutionStatus

    requested_quantity: int | None = None
    submitted_quantity: int | None = None
    filled_quantity: int = 0
    order_price: Decimal | None = None

    broker_seq13: str | None = None
    broker_return_code: int | None = None
    message: str = ""

    evidence: dict[str, Any] = field(default_factory=dict)
    do_not_retry_automatically: bool = False

    @property
    def changes_position(self) -> bool:
        return (
            self.filled_quantity > 0
            and self.status
            in {ExecutionStatus.PARTIALLY_FILLED, ExecutionStatus.FILLED}
        )

    def to_jsonable(self) -> dict[str, Any]:
        data = asdict(self)
        data["side"] = self.side.value
        data["status"] = self.status.value
        if data["order_price"] is not None:
            data["order_price"] = str(data["order_price"])
        return data
