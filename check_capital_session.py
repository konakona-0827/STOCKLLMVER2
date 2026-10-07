from __future__ import annotations

from pathlib import Path

from execution.capital_reply_session import build_live_readonly_session_from_project


def main() -> int:
    root = Path(__file__).resolve().parent
    session = build_live_readonly_session_from_project(root)
    session.connect()

    h1 = session.health_check()
    print("========== CAPITAL SESSION ==========")
    print(f"READY={h1.ready}")
    print(f"LOCAL_CONNECTED={h1.local_connected}")
    print(f"REPLY_STATE={h1.reply_state}")
    print(f"REPLY_CONNECTED={h1.reply_connected}")
    print(f"ACCOUNT_SELECTED={h1.account_selected}")
    print(f"ACTION={h1.action}")
    print(f"MESSAGE={h1.message}")

    h2 = session.ensure_ready()
    print("========== ENSURE READY ==========")
    print(f"READY={h2.ready}")
    print(f"REPLY_STATE={h2.reply_state}")
    print(f"ACTION={h2.action}")
    print("REAL_ORDER_SENT=NO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
