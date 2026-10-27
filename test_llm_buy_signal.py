from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from execution.models import Side
from execution.synthetic_llm_test_runner import (
    SyntheticSignalRequest,
    run_synthetic_signal,
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Synthetic final-LLM BUY signal test using one reusable Capital session."
    )
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--qty", required=True, type=int)
    ap.add_argument("--price", required=True)
    ap.add_argument("--bid")
    ap.add_argument("--ask")
    ap.add_argument("--confidence", type=float, default=80.0)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--config", default=r"config\execution_live.json")
    ap.add_argument("--env")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent
    request = SyntheticSignalRequest(
        symbol=args.symbol,
        side=Side.BUY,
        qty=args.qty,
        price=Decimal(str(args.price)),
        bid=None if args.bid is None else Decimal(str(args.bid)),
        ask=None if args.ask is None else Decimal(str(args.ask)),
        confidence=args.confidence,
    )

    report = run_synthetic_signal(
        request,
        project_root=root,
        execute=args.execute,
        config_path=args.config,
        env_path=args.env,
    )

    print("========== SYNTHETIC LLM BUY ==========")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    execution = report.get("execution") or {}
    for item in execution.get("results", []):
        print(
            f"RESULT symbol={item.get('symbol')} "
            f"status={item.get('status')} "
            f"BROKER_ORDER_SENT={item.get('broker_order_sent', False)} "
            f"CROSSCHECK={item.get('crosscheck_status', '<not-run>')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
