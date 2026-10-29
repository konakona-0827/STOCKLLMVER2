"""Keep the interactive Dashboard alive independently of its launching shell.

Run this from a Windows scheduled task with an interactive user token. A normal
Dashboard window close ends the supervisor; an unlogged exit is recorded and
restarted with a bounded retry policy.
"""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time


CODE_ROOT = Path(__file__).resolve().parents[1]
TAIPEI = timezone(timedelta(hours=8))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def record(path: Path, event: str, **detail) -> None:
    stamp = utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "time_utc": stamp.isoformat(timespec="milliseconds"),
        "time_taipei": stamp.astimezone(TAIPEI).isoformat(timespec="milliseconds"),
        "event": event,
        **detail,
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def lifecycle_exit(path: Path, pid: int, launched_at: datetime) -> dict:
    """Read only events from this process launch, ignoring recycled PIDs."""
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, ValueError):
        return {"run_id": None, "last_event": None, "normal_close": False}
    start = None
    for index, row in enumerate(rows):
        if row.get("pid") != pid or row.get("event") != "START":
            continue
        try:
            stamp = datetime.fromisoformat(row["time_utc"])
        except (KeyError, ValueError, TypeError):
            continue
        if stamp >= launched_at - timedelta(seconds=3):
            start = index
    if start is None:
        return {"run_id": None, "last_event": None, "normal_close": False}
    run_id = rows[start].get("run_id")
    events = [row for row in rows[start:] if row.get("run_id") == run_id]
    final = events[-1] if events else {}
    return {
        "run_id": run_id,
        "last_event": final.get("event"),
        "last_event_at": final.get("time_taipei"),
        "normal_close": final.get("event") == "EXIT"
        and (final.get("detail") or {}).get("reason") == "NORMAL_CLOSE",
    }


def supervise(state_root: Path, *, mode: str, fallback_strategy: Path | None,
              max_restarts: int, restart_window_seconds: int = 600) -> int:
    health_dir = state_root / "data" / "health"
    supervisor_log = health_dir / "dashboard_supervisor.jsonl"
    lifecycle_log = health_dir / "dashboard_lifecycle.jsonl"
    child_log = health_dir / "dashboard_child_output.log"
    recent_exits: deque[float] = deque()
    record(supervisor_log, "SUPERVISOR_START", supervisor_pid=os.getpid(),
           code_root=str(CODE_ROOT), state_root=str(state_root), mode=mode)
    while True:
        env = os.environ.copy()
        env["TRADING_STATE_ROOT"] = str(state_root)
        env["TRADING_MODE"] = mode
        # A later selection made in the GUI takes precedence after restart.
        env.pop("TRADING_STRATEGY_PATH", None)
        if (fallback_strategy is not None
                and not (state_root / "data" / "analysis" / "live_strategy.json").is_file()):
            env["TRADING_STRATEGY_PATH"] = str(fallback_strategy)
        launched_at = utc_now()
        with child_log.open("ab") as output:
            child = subprocess.Popen(
                [sys.executable, "-X", "faulthandler", str(CODE_ROOT / "dashboard.py"),
                 "--resume-auto"],
                cwd=CODE_ROOT, env=env, stdout=output, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            record(supervisor_log, "DASHBOARD_STARTED", child_pid=child.pid,
                   strategy_override=env.get("TRADING_STRATEGY_PATH"))
            exit_code = child.wait()
        life = lifecycle_exit(lifecycle_log, child.pid, launched_at)
        if life["normal_close"]:
            record(supervisor_log, "NORMAL_CLOSE", child_pid=child.pid,
                   exit_code=exit_code, **life)
            return 0
        record(supervisor_log, "UNEXPECTED_EXIT", child_pid=child.pid,
               exit_code=exit_code, **life)
        moment = time.monotonic()
        recent_exits.append(moment)
        while recent_exits and moment - recent_exits[0] > restart_window_seconds:
            recent_exits.popleft()
        if len(recent_exits) > max_restarts:
            record(supervisor_log, "RESTART_LIMIT_REACHED", attempts=len(recent_exits),
                   window_seconds=restart_window_seconds)
            return 1
        time.sleep(3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, default=CODE_ROOT)
    parser.add_argument("--mode", choices=("agent", "llm"), default="agent")
    parser.add_argument("--fallback-strategy", type=Path)
    parser.add_argument("--max-restarts", type=int, default=3)
    args = parser.parse_args()
    if args.max_restarts < 0:
        parser.error("--max-restarts must be nonnegative")
    return supervise(args.state_root.resolve(), mode=args.mode,
                     fallback_strategy=(args.fallback_strategy.resolve()
                                        if args.fallback_strategy else None),
                     max_restarts=args.max_restarts)


if __name__ == "__main__":
    raise SystemExit(main())
