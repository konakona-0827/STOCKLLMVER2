from __future__ import annotations

from pathlib import Path
import json

from .auto_advice_executor import execute_advice_file
from .account_snapshot import ExecutionAccountSnapshot
from .capital_reply_session import CapitalReplySession


def execute_after_advice_written(
    advice_path: str | Path,
    *,
    project_root: str | Path,
    config_path: str | Path | None = None,
    env_path: str | Path | None = None,
    session: CapitalReplySession | None = None,
    account_snapshot: ExecutionAccountSnapshot | None = None,
) -> dict:
    """Call this immediately after advice_interface.py finishes writing JSON."""
    root = Path(project_root)
    cfg = Path(config_path) if config_path else root / "config" / "execution_live.json"

    report = execute_advice_file(
        advice_path,
        project_root=root,
        config_path=cfg,
        env_path=env_path,
        session=session,
        account_snapshot=account_snapshot,
    )

    out_dir = root / "data" / "execution"
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = out_dir / "latest_execution.json"
    tmp = latest.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    tmp.replace(latest)
    return report
