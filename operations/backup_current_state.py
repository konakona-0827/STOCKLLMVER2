"""Create a timestamped, consistent read-only snapshot before live changes."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import json
import shutil
import sqlite3

from config import ROOT, TAIPEI


def main() -> Path:
    stamp = datetime.now(TAIPEI).strftime("%Y%m%dT%H%M%S")
    destination = ROOT / "data" / "backups" / f"pre-health-{stamp}"
    destination.mkdir(parents=True, exist_ok=False)
    files = (
        "data/analysis/market_history.sqlite3",
        "data/analysis/latest_dashboard.json",
        "data/analysis/latest_advice.json",
        "data/execution/execution.sqlite3",
        "data/execution/latest_execution.json",
        "data/execution/live_execution.sqlite3",
        "data/execution/latest_live_execution.json",
        "data/execution/synthetic_execution.sqlite3",
        "data/execution/latest_synthetic_execution.json",
        "config/execution_live.json",
    )
    copied = []
    for name in files:
        source = ROOT / name
        if not source.exists():
            continue
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix == ".sqlite3":
            with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro",
                                         uri=True)) as src:
                with closing(sqlite3.connect(target)) as dst:
                    src.backup(dst)
                    dst.commit()
        else:
            shutil.copy2(source, target)
        copied.append(dict(path=name, size=target.stat().st_size,
                           sha256=sha256(target.read_bytes()).hexdigest()))
    (destination / "manifest.json").write_text(
        json.dumps(dict(created_at=datetime.now(TAIPEI).isoformat(),
                        files=copied), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return destination


if __name__ == "__main__":
    print(main())
