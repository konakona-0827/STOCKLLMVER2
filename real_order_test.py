from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from execution.capital_executor import CapitalOddLotExecutor
from execution.capital_reply_session import build_live_readonly_session_from_project
from execution.models import ExecutionStatus, Side


def print_result(result) -> int:
    """Report broker evidence without treating an attempted send as acceptance."""
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
    print(f"SEND_ATTEMPTED={'YES' if result.send_attempted else 'NO'}")
    sent = {True: 'YES', False: 'NO', None: 'UNKNOWN'}[result.broker_order_sent]
    print(f"REAL_ORDER_SENT={sent}")
    print(f"CROSSCHECK_PERFORMED={'YES' if result.crosscheck_performed else 'NO'}")
    print(f"FILLED_QUANTITY={result.filled_quantity}")
    accepted = result.broker_order_sent is True and result.crosscheck_performed and result.status in (
        ExecutionStatus.ACKNOWLEDGED, ExecutionStatus.PARTIALLY_FILLED,
        ExecutionStatus.FILLED, ExecutionStatus.CANCELLED,
    )
    print(f"BROKER_ACCEPTANCE_VERIFIED={'YES' if accepted else 'NO'}")
    print("POSITION_UPDATED=NO")
    print("AUTOMATIC_RETRY=NO")
    print("============================")
    return 0 if accepted else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MANUAL REAL intraday odd-lot order test with SKReply cross-check."
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--side", required=True, choices=["BUY", "SELL"])
    parser.add_argument("--qty", required=True, type=int)
    parser.add_argument("--price", required=True)
    parser.add_argument("--observe-seconds", type=float, default=20.0)
    parser.add_argument("--env")
    parser.add_argument(
        "--send-real",
        action="store_true",
        help="required before a REAL broker order can be submitted",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    expected = f"SEND {args.symbol.upper()} {args.side} {args.qty}"

    print("========== REAL ORDER TEST ==========")
    print(f"SYMBOL={args.symbol.upper()}")
    print(f"SIDE={args.side}")
    print(f"QTY={args.qty}")
    print(f"PRICE={args.price}")
    print("THIS MAY PLACE A REAL BROKER ORDER.")
    print("No automatic retry. No automatic position update.")

    if not args.send_real:
        print("Blocked: add --send-real if you intentionally want a real order.")
        print("REAL_ORDER_SENT=NO")
        return 2

    typed = input(f'Type exactly "{expected}" to continue: ').strip()
    if typed != expected:
        print("Confirmation mismatch. Nothing sent.")
        print("REAL_ORDER_SENT=NO")
        return 2

    session = build_live_readonly_session_from_project(root, env_path=args.env)
    session.connect()

    executor = CapitalOddLotExecutor(session)
    result = executor.send_limit_order(
        symbol=args.symbol,
        side=Side(args.side),
        quantity=args.qty,
        price=Decimal(args.price),
        observe_seconds=args.observe_seconds,
    )

    return print_result(result)


if __name__ == "__main__":
    raise SystemExit(main())
