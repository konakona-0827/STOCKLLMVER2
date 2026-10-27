from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .account_guard import (
    BuyingPower,
    InventoryRow,
    query_buying_power,
    query_inventory,
)
from .capital_reply_session import CapitalReplySession


@dataclass(frozen=True)
class ExecutionAccountSnapshot:
    captured_at: str
    buying_power: BuyingPower | None = None
    inventory: tuple[InventoryRow, ...] | None = None
    buying_power_error: str | None = None
    inventory_error: str | None = None

    @property
    def has_buying_power(self) -> bool:
        return self.buying_power is not None

    @property
    def has_inventory(self) -> bool:
        return self.inventory is not None


def capture_account_snapshot(
    session: CapitalReplySession,
    *,
    need_buying_power: bool,
    need_inventory: bool,
) -> ExecutionAccountSnapshot:
    """Capture each requested broker account query at most once.

    Errors are stored independently so a failed inventory read does not block
    BUY-only actions, and a failed balance read does not block SELL-only actions.
    """
    buying_power = None
    inventory = None
    buying_power_error = None
    inventory_error = None

    if need_buying_power:
        try:
            buying_power = query_buying_power(session)
        except Exception as exc:
            buying_power_error = f"{type(exc).__name__}: {exc}"

    if need_inventory:
        try:
            inventory = tuple(query_inventory(session))
        except Exception as exc:
            inventory_error = f"{type(exc).__name__}: {exc}"

    return ExecutionAccountSnapshot(
        captured_at=datetime.now(timezone.utc).isoformat(),
        buying_power=buying_power,
        inventory=inventory,
        buying_power_error=buying_power_error,
        inventory_error=inventory_error,
    )
