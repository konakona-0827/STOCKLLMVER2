from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from execution.capital_executor import CapitalOddLotExecutor
from execution.capital_reply_session import build_live_readonly_session_from_project
from execution.models import Side


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MANUAL REAL intraday odd-lot SELL test."
    )
    parser.add_argument("--symbol", default="2891")
    parser.add_argument("--qty", type=int, default=1)
    parser.add_argument("--price", required=True, help="limit price; choose it yourself")
    parser.add_argument("--observe-seconds", type=float, default=20.0)
    parser.add_argument("--env")
    parser.add_argument(
        "--send-real",
        action="store_true",
        help="required before a REAL sell order can be submitted",
    )
    args = parser.parse_args()

    symbol = args.symbol.strip().upper()
    expected = f"SELL {symbol} {args.qty}"

    print("========== REAL SELL TEST ==========")
    print(f"SYMBOL={symbol}")
    print(f"SIDE=SELL")
    print(f"QTY={args.qty}")
    print(f"LIMIT_PRICE={args.price}")
    print("This may place a REAL broker SELL order.")
    print("No automatic retry. No automatic position update.")

    if not args.send_real:
        print("Blocked: add --send-real if you intentionally want a real SELL order.")
        print("REAL_ORDER_SENT=NO")
        return 2

    typed = input(f'Type exactly "{expected}" to continue: ').strip()
    if typed != expected:
        print("Confirmation mismatch. Nothing sent.")
        print("REAL_ORDER_SENT=NO")
        return 2

    root = Path(__file__).resolve().parent
    session = build_live_readonly_session_from_project(root, env_path=args.env)
    session.connect()

    executor = CapitalOddLotExecutor(session)
    result = executor.send_limit_order(
        symbol=symbol,
        side=Side.SELL,
        quantity=args.qty,
        price=Decimal(args.price),
        observe_seconds=args.observe_seconds,
    )

    print()
    print("========== RESULT ==========")
    print(f"SEND_RETURN_CODE={result.send_return_code}")
    print(f"SEQ13={result.seq13 or '<none>'}")
    print(f"CROSSCHECK_STATUS={result.crosscheck.status.value}")
    print(f"FINAL_STATUS={result.status.value}")
    print(f"ACCEPTED_EVENT_COUNT={len(result.crosscheck.accepted_rows)}")
    print(f"DEAL_EVENT_COUNT={len(result.crosscheck.fill_rows)}")
    print(f"CANCEL_EVENT_COUNT={len(result.crosscheck.cancel_rows)}")
    print(f"USED_FALLBACK={result.crosscheck.used_fallback}")
    print(f"MESSAGE={result.crosscheck.message}")
    print("POSITION_UPDATED=NO")
    print("AUTOMATIC_RETRY=NO")
    print("REAL_ORDER_SENT=YES")
    print("============================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
