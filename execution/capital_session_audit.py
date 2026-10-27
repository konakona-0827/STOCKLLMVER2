"""Append non-secret Capital COM lifecycle diagnostics for each process."""
from __future__ import annotations

import csv
import os
from pathlib import Path
from threading import Lock, get_ident

from config import now


_FIELDS = (
    "timestamp",
    "pid",
    "thread_id",
    "call_site",
    "event",
    "session_id",
    "object_type",
    "object_id",
    "api_name",
    "login_set_quote_flag",
    "return_code",
)
_LOCK = Lock()


def record_capital_event(
    project_root: str | Path,
    *,
    session_id: str,
    call_site: str,
    event: str,
    object_type: str = "",
    object_id: str | int = "",
    api_name: str = "",
    login_set_quote_flag: str = "N/A",
    return_code: str | int = "",
) -> None:
    """Append one row; never let diagnostics break a broker operation."""
    path = Path(project_root) / "data" / "health" / "capital_com_events.csv"
    row = {
        "timestamp": now().isoformat(),
        "pid": os.getpid(),
        "thread_id": get_ident(),
        "call_site": call_site,
        "event": event,
        "session_id": session_id,
        "object_type": object_type,
        "object_id": object_id,
        "api_name": api_name,
        "login_set_quote_flag": login_set_quote_flag,
        "return_code": return_code,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, path.open("a", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=_FIELDS)
            if stream.tell() == 0:
                writer.writeheader()
            writer.writerow(row)
            stream.flush()
    except OSError as exc:
        # Audit output must not change whether a quote or order operation runs.
        print(f"CAPITAL_AUDIT_WRITE_FAILED | {type(exc).__name__}", flush=True)
