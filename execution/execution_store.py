from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
from typing import Any

from .models import Side


class ExecutionStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def _init(self):
        with self._connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS execution_attempts (
                    decision_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    detail_json TEXT
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS execution_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id TEXT NOT NULL,
                    event_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail_json TEXT
                )
            """)
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_execution_events_decision
                ON execution_events(decision_id, id)
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS ai_positions (
                    symbol TEXT PRIMARY KEY,
                    qty INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

    def append_event(
        self,
        decision_id: str,
        status: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute(
                """INSERT INTO execution_events
                   (decision_id, event_at, status, detail_json)
                   VALUES (?, ?, ?, ?)""",
                (
                    decision_id,
                    now,
                    status,
                    json.dumps(detail or {}, ensure_ascii=False, default=str),
                ),
            )

    def claim(self, decision_id: str, run_id: str, symbol: str, side: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._connect() as con:
                con.execute(
                    """INSERT INTO execution_attempts
                       (decision_id, run_id, symbol, side, started_at, status)
                       VALUES (?, ?, ?, ?, ?, 'STARTED')""",
                    (decision_id, run_id, symbol, side, now),
                )
        except sqlite3.IntegrityError:
            return False

        self.append_event(
            decision_id,
            "STARTED",
            {"run_id": run_id, "symbol": symbol, "side": side},
        )
        return True

    def finish(self, decision_id: str, status: str, detail: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute(
                """UPDATE execution_attempts
                   SET finished_at=?, status=?, detail_json=?
                   WHERE decision_id=?""",
                (
                    now,
                    status,
                    json.dumps(detail, ensure_ascii=False, default=str),
                    decision_id,
                ),
            )
        self.append_event(decision_id, status, detail)

    def events_for(self, decision_id: str) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                """SELECT event_at, status, detail_json
                   FROM execution_events
                   WHERE decision_id=?
                   ORDER BY id""",
                (decision_id,),
            ).fetchall()

        out = []
        for event_at, status, detail_json in rows:
            try:
                detail = json.loads(detail_json or "{}")
            except json.JSONDecodeError:
                detail = {"raw": detail_json}
            out.append(
                {"event_at": event_at, "status": status, "detail": detail}
            )
        return out

    def get_ai_qty(self, symbol: str) -> int | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT qty FROM ai_positions WHERE symbol=?",
                (symbol.upper(),),
            ).fetchone()
        return None if row is None else int(row[0])

    def bootstrap_ai_qty(self, symbol: str, qty: int) -> int:
        symbol = symbol.upper()
        qty = max(0, int(qty))
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute(
                """INSERT OR IGNORE INTO ai_positions(symbol, qty, updated_at)
                   VALUES (?, ?, ?)""",
                (symbol, qty, now),
            )
            row = con.execute(
                "SELECT qty FROM ai_positions WHERE symbol=?",
                (symbol,),
            ).fetchone()
        return int(row[0])

    def apply_fill(self, symbol: str, side: Side, qty: int) -> int:
        symbol = symbol.upper()
        qty = max(0, int(qty))
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            row = con.execute(
                "SELECT qty FROM ai_positions WHERE symbol=?",
                (symbol,),
            ).fetchone()
            current = int(row[0]) if row else 0
            new_qty = current + qty if side == Side.BUY else max(0, current - qty)
            con.execute(
                """INSERT INTO ai_positions(symbol, qty, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(symbol) DO UPDATE
                   SET qty=excluded.qty, updated_at=excluded.updated_at""",
                (symbol, new_qty, now),
            )
        return new_qty
