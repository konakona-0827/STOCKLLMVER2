from __future__ import annotations

import argparse
from pathlib import Path

from execution.advice_sell_runner import (
    execute_one_planned_sell,
    load_and_plan_sell_orders,
)
from execution.capital_reply_session import build_live_readonly_session_from_project
from execution.risk_guard import RiskConfig, RiskRejected


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Consume advice_interface JSON and execute SELL recommendations."
    )
    parser.add_argument(
        "--advice",
        default=r"data\analysis\advice_latest.json",
        help="stable advice interface JSON",
    )
    parser.add_argument("--symbol", help="optional single symbol filter")
    parser.add_argument("--observe-seconds", type=float, default=20.0)
    parser.add_argument("--env")
    parser.add_argument(
        "--send-real",
        action="store_true",
        help="required before any REAL sell can be submitted",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    advice_path = root / args.advice if not Path(args.advice).is_absolute() else Path(args.advice)

    try:
        _, planned = load_and_plan_sell_orders(advice_path, config=RiskConfig())
    except (OSError, ValueError, RiskRejected) as exc:
        print(f"BLOCKED: {type(exc).__name__}: {exc}")
        print("REAL_ORDER_SENT=NO")
        return 2

    if args.symbol:
        wanted = args.symbol.strip().upper()
        planned = [x for x in planned if x.plan.symbol == wanted]

    if not planned:
        print("No executable SELL recommendation found.")
        print("REAL_ORDER_SENT=NO")
        return 0

    print("========== SELL PLANS ==========")
    for item in planned:
        p = item.plan
        print(
            f"{p.symbol} SELL qty={p.quantity} "
            f"limit={p.limit_price} confidence={p.confidence_pct}% "
            f"source={p.sizing_source}"
        )

    if not args.send_real:
        print("DRY RUN ONLY. Add --send-real to allow a real sell.")
        print("REAL_ORDER_SENT=NO")
        return 0

    if len(planned) != 1:
        print(
            "Safety block: this first real runner executes exactly one SELL at a time. "
            "Use --symbol to select one."
        )
        print("REAL_ORDER_SENT=NO")
        return 2

    item = planned[0]
    p = item.plan
    expected = f"EXECUTE {p.symbol} SELL {p.quantity}"
    typed = input(f'Type exactly "{expected}" to continue: ').strip()
    if typed != expected:
        print("Confirmation mismatch. Nothing sent.")
        print("REAL_ORDER_SENT=NO")
        return 2

    session = build_live_readonly_session_from_project(root, env_path=args.env)
    session.connect()

    result = execute_one_planned_sell(
        item,
        session=session,
        observe_seconds=args.observe_seconds,
    )

    print()
    print("========== RESULT ==========")
    print(f"SYMBOL={result.symbol}")
    print(f"SIDE={result.side.value}")
    print(f"QTY={result.quantity}")
    print(f"PRICE={result.price}")
    print(f"SEQ13={result.seq13 or '<none>'}")
    print(f"FINAL_STATUS={result.status.value}")
    print(f"ACCEPTED_EVENT_COUNT={len(result.crosscheck.accepted_rows)}")
    print(f"DEAL_EVENT_COUNT={len(result.crosscheck.fill_rows)}")
    print(f"CANCEL_EVENT_COUNT={len(result.crosscheck.cancel_rows)}")
    print("POSITION_UPDATED=NO")
    print("AUTOMATIC_RETRY=NO")
    print("REAL_ORDER_SENT=YES")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
