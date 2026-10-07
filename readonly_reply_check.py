from __future__ import annotations

import argparse
from pathlib import Path

from execution.capital_reply_session import (
    build_live_readonly_session_from_project,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="READ-ONLY Capital SKReply connection/replay check. Sends no order."
    )
    parser.add_argument(
        "--project-root",
        default=str(Path(__file__).resolve().parent),
    )
    parser.add_argument("--env")
    parser.add_argument("--seq", help="optional exact 13-digit broker sequence")
    parser.add_argument(
        "--live-readonly",
        action="store_true",
        help="required safety flag before connecting to Capital API",
    )
    args = parser.parse_args()

    if not args.live_readonly:
        print("Not connected. Add --live-readonly to perform a READ-ONLY broker check.")
        print("BROKER_ORDER_SENT=NO")
        return 0

    session = build_live_readonly_session_from_project(
        args.project_root,
        env_path=args.env,
    )
    snapshot = session.connect()

    print("MODE=READ_ONLY")
    print(f"REPLAY_COMPLETE={snapshot.replay_complete}")
    print(f"TC_EVENT_COUNT={len(snapshot.tc_rows)}")
    print(f"ACCOUNT_COUNT={len(snapshot.accounts)}")

    if args.seq:
        result = session.crosscheck_seq13(args.seq)
        print(f"SEQ13={args.seq}")
        print(f"CROSSCHECK_STATUS={result.status.value}")
        print(f"ACCEPTED_EVENT_COUNT={len(result.accepted_rows)}")
        print(f"FILL_EVENT_COUNT={len(result.fill_rows)}")
        print(f"CANCEL_EVENT_COUNT={len(result.cancel_rows)}")
        print(f"MESSAGE={result.message}")

    print("BROKER_ORDER_SENT=NO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
