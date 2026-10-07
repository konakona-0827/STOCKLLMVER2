from __future__ import annotations

from pathlib import Path

from execution.account_guard import (
    AccountCheckError,
    cash_sellable_qty,
    query_buying_power,
    query_inventory,
)
from execution.capital_reply_session import build_live_readonly_session_from_project


def main() -> int:
    root = Path(__file__).resolve().parent
    session = build_live_readonly_session_from_project(root)
    session.connect()

    print("MODE=READ_ONLY_ACCOUNT_CHECK")

    try:
        power = query_buying_power(session)
        print(f"BUYING_POWER_VERIFIED={power.verified}")
        print(f"ONE_ACCOUNT={power.one_account}")
        print(f"BALANCE_TWD={power.balance_twd}")
        print(f"WITHDRAWABLE_TWD={power.withdrawable_twd}")
        print(f"AVAILABLE_TO_BUY_TWD={power.available_to_buy_twd}")
    except AccountCheckError as exc:
        print(f"BUYING_POWER_VERIFIED=False")
        print(f"BUYING_POWER_ERROR={exc}")

    try:
        rows = query_inventory(session)
        print(f"INVENTORY_ROW_COUNT={len(rows)}")
        for row in rows:
            if row.inventory_type == "T":
                print(
                    f"HOLDING symbol={row.symbol} "
                    f"realtime_qty={row.realtime_qty} "
                    f"sellable_qty={row.sellable_qty}"
                )
    except AccountCheckError as exc:
        print(f"INVENTORY_VERIFIED=False")
        print(f"INVENTORY_ERROR={exc}")

    print("REAL_ORDER_SENT=NO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
