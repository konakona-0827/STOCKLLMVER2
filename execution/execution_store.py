from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing, contextmanager
from decimal import Decimal
from pathlib import Path
import json
import sqlite3
from typing import Any

from .models import Side
from config import TAIPEI


class ExecutionStore:
    def __init__(self, path: str | Path, *, must_exist: bool = False) -> None:
        self.path = Path(path)
        if must_exist:
            self._validate_existing()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _validate_existing(self):
        if not self.path.is_file():
            raise RuntimeError(f"execution DB missing; restore a backup: {self.path}")
        with closing(sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro",
                                     uri=True, timeout=10)) as con:
            if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("execution DB failed SQLite quick_check")
            names = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            required = {"execution_attempts", "execution_events", "ai_positions"}
            if not required <= names:
                raise RuntimeError("execution DB is missing expected tables; restore a backup")

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

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
                self._insert_event(con, decision_id, "STARTED", now,
                                   {"run_id": run_id, "symbol": symbol, "side": side})
        except sqlite3.IntegrityError:
            return False
        return True

    @staticmethod
    def _insert_event(con, decision_id, status, now, detail):
        con.execute(
            """INSERT INTO execution_events(decision_id, event_at, status, detail_json)
               VALUES (?, ?, ?, ?)""",
            (decision_id, now, status,
             json.dumps(detail, ensure_ascii=False, default=str)),
        )

    @staticmethod
    def _daily_reserved(con, local_day) -> Decimal:
        total = Decimal("0")
        for started_at, status, detail_json in con.execute(
            """SELECT started_at, status, detail_json FROM execution_attempts
               WHERE side='BUY' AND status NOT IN ('STARTED','SKIPPED_CHECK_FAILED')"""
        ):
            if datetime.fromisoformat(started_at).astimezone(TAIPEI).date() != local_day:
                continue
            detail = json.loads(detail_json or "{}")
            if status == "FAILED" and detail.get("send_return_code") not in (0, None):
                continue
            if status == "FAILED" and not detail.get("send_attempted"):
                continue
            required = detail.get("required_twd_with_buffer")
            if required is None:
                raise RuntimeError("BUY attempt has no durable cash reservation; reconcile first")
            total += Decimal(str(required))
        return total

    def daily_reserved_buy_twd(self) -> Decimal:
        with self._connect() as con:
            return self._daily_reserved(con, datetime.now(TAIPEI).date())

    def mark_send_pending(self, decision_id: str, detail: dict[str, Any],
                          *, daily_limit_twd: Decimal | None = None) -> None:
        """Durable no-retry marker immediately before the broker call."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if daily_limit_twd is not None:
                required = Decimal(str(detail["required_twd_with_buffer"]))
                reserved = self._daily_reserved(con, datetime.now(TAIPEI).date())
                if reserved + required > daily_limit_twd:
                    raise RuntimeError(
                        f"daily BUY cap reached: {reserved}+{required}>{daily_limit_twd}"
                    )
            changed = con.execute(
                """UPDATE execution_attempts SET status='SEND_PENDING', detail_json=?
                   WHERE decision_id=? AND status='STARTED' AND finished_at IS NULL""",
                (json.dumps(detail, ensure_ascii=False, default=str), decision_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError(f"cannot mark send pending: {decision_id}")
            self._insert_event(con, decision_id, "SEND_PENDING", now, detail)

    def mark_submitted(self, decision_id: str, detail: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            changed = con.execute(
                """UPDATE execution_attempts SET status='SUBMITTED', detail_json=?
                   WHERE decision_id=? AND status='SEND_PENDING' AND finished_at IS NULL""",
                (json.dumps(detail, ensure_ascii=False, default=str), decision_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError(f"cannot mark submitted: {decision_id}")
            self._insert_event(con, decision_id, "SUBMITTED", now, detail)

    def finish(self, decision_id: str, status: str, detail: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            changed = con.execute(
                """UPDATE execution_attempts
                   SET finished_at=?, status=?, detail_json=?
                   WHERE decision_id=? AND finished_at IS NULL""",
                (
                    now,
                    status,
                    json.dumps(detail, ensure_ascii=False, default=str),
                    decision_id,
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError(f"attempt already finished or missing: {decision_id}")
            self._insert_event(con, decision_id, status, now, detail)

    def finish_with_fill(self, decision_id: str, status: str,
                         detail: dict[str, Any], symbol: str,
                         side: Side, qty: int) -> int:
        """Commit confirmed fill, final attempt and event in one transaction."""
        qty = int(qty)
        if qty <= 0 or status not in ("FILLED", "PARTIALLY_FILLED"):
            raise ValueError("only a confirmed positive fill can change ai_positions")
        symbol = symbol.upper()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            attempt = con.execute(
                "SELECT status, finished_at FROM execution_attempts WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if attempt is None or attempt[1] is not None or attempt[0] != "SUBMITTED":
                raise RuntimeError(f"attempt is not an open submitted order: {decision_id}")
            row = con.execute(
                "SELECT qty FROM ai_positions WHERE symbol=?", (symbol,),
            ).fetchone()
            current = int(row[0]) if row else 0
            if side == Side.SELL and current < qty:
                raise RuntimeError(f"confirmed SELL fill exceeds AI shares: {symbol}")
            new_qty = current + qty if side == Side.BUY else current - qty
            con.execute(
                """INSERT INTO ai_positions(symbol, qty, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(symbol) DO UPDATE
                   SET qty=excluded.qty, updated_at=excluded.updated_at""",
                (symbol, new_qty, now),
            )
            full_detail = dict(detail, ai_managed_qty_after_fill=new_qty)
            con.execute(
                """UPDATE execution_attempts SET finished_at=?, status=?, detail_json=?
                   WHERE decision_id=?""",
                (now, status,
                 json.dumps(full_detail, ensure_ascii=False, default=str), decision_id),
            )
            self._insert_event(con, decision_id, status, now, full_detail)
        return new_qty

    def reconcile_broker_fill(
        self, decision_id: str, seq13: str, broker_filled_qty: int,
        fill_details: list[dict], broker_cash_qty: int, configured_user_qty: int,
    ) -> dict:
        """Idempotently apply exact-order broker fills to the formal AI ledger."""
        confirmed = int(broker_filled_qty)
        if confirmed <= 0 or not fill_details:
            raise ValueError("broker fill evidence is missing")
        if sum(int(row["quantity"]) for row in fill_details) != confirmed:
            raise ValueError("broker fill quantities do not agree")
        gross = sum(
            Decimal(str(row["price_twd"])) * int(row["quantity"])
            for row in fill_details
        )
        if gross <= 0 or any(Decimal(str(row["price_twd"])) <= 0
                             for row in fill_details):
            raise ValueError("broker fill price is missing")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                """SELECT symbol,side,status,detail_json FROM execution_attempts
                   WHERE decision_id=?""", (decision_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("formal order is missing")
            symbol, side, status, detail_json = row
            detail = json.loads(detail_json or "{}")
            if (side not in ("BUY", "SELL") or status not in
                    ("ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED",
                     "UNCONFIRMED", "CANCELLED", "PARTIALLY_FILLED_CANCELLED")
                    or str(detail.get("seq13")) != str(seq13)
                    or detail.get("send_return_code") != 0):
                raise RuntimeError("broker order identity is not verified")
            ordered = int(detail.get("quantity") or 0)
            recorded = int(detail.get("filled_quantity") or 0)
            if not 0 <= recorded <= confirmed <= ordered:
                raise RuntimeError("broker fill conflicts with formal order quantity")
            delta = confirmed - recorded
            if delta == 0:
                return dict(changed=False, symbol=symbol, filled_qty=confirmed)
            current_row = con.execute(
                "SELECT qty FROM ai_positions WHERE symbol=?", (symbol,),
            ).fetchone()
            current_qty = int(current_row[0]) if current_row else 0
            new_qty = current_qty + delta if side == "BUY" else current_qty - delta
            if new_qty < 0:
                raise RuntimeError("broker SELL fill exceeds formal AI shares")
            if new_qty + max(0, int(configured_user_qty)) > int(broker_cash_qty):
                raise RuntimeError("broker cash inventory does not cover confirmed AI shares")
            new_status = "FILLED" if confirmed == ordered else "PARTIALLY_FILLED"
            detail.update(
                filled_quantity=confirmed,
                broker_reconciled_seq13=str(seq13),
                broker_reconciled_qty=confirmed,
                broker_reconciled_side=side,
                broker_reconciled_at=now,
                broker_replay_fill_details=fill_details,
                broker_replay_fill_gross_twd=str(gross),
                ai_managed_qty_after_fill=new_qty,
            )
            con.execute(
                """INSERT INTO ai_positions(symbol,qty,updated_at) VALUES (?,?,?)
                   ON CONFLICT(symbol) DO UPDATE
                   SET qty=excluded.qty,updated_at=excluded.updated_at""",
                (symbol, new_qty, now),
            )
            con.execute(
                """UPDATE execution_attempts SET status=?,detail_json=?
                   WHERE decision_id=?""",
                (new_status, json.dumps(detail, ensure_ascii=False, default=str),
                 decision_id),
            )
            self._insert_event(con, decision_id, "BROKER_FILL_RECONCILED", now,
                               dict(seq13=seq13, side=side, delta_qty=delta,
                                    total_filled_qty=confirmed,
                                    broker_fill_gross_twd=str(gross)))
        return dict(changed=True, symbol=symbol, delta_qty=delta,
                    filled_qty=confirmed, status=new_status, side=side)

    def reconcile_broker_buy_fill(
        self, decision_id: str, seq13: str, broker_filled_qty: int,
        fill_details: list[dict], broker_cash_qty: int, configured_user_qty: int,
    ) -> dict:
        """Compatibility wrapper for callers restricted to BUY orders."""
        with self._connect() as con:
            row = con.execute("SELECT side FROM execution_attempts WHERE decision_id=?",
                              (decision_id,)).fetchone()
        if row is None or row[0] != "BUY":
            raise RuntimeError("broker order is not a BUY")
        return self.reconcile_broker_fill(
            decision_id, seq13, broker_filled_qty, fill_details,
            broker_cash_qty, configured_user_qty)

    def reconcile_broker_cancel(
        self, decision_id: str, seq13: str, broker_filled_qty: int,
    ) -> dict:
        """Close an order only when exact broker cancel and fill totals agree."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT symbol,side,status,detail_json FROM execution_attempts WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("formal order is missing")
            symbol, side, status, detail_json = row
            detail = json.loads(detail_json or "{}")
            if (side not in ("BUY", "SELL")
                    or str(detail.get("seq13")) != str(seq13)
                    or detail.get("send_return_code") != 0
                    or int(detail.get("filled_quantity") or 0) != int(broker_filled_qty)):
                raise RuntimeError("broker cancel does not match formal order")
            new_status = ("PARTIALLY_FILLED_CANCELLED"
                          if broker_filled_qty else "CANCELLED")
            if status == new_status:
                return dict(changed=False, symbol=symbol, status=status)
            if status not in ("ACKNOWLEDGED", "PARTIALLY_FILLED", "UNCONFIRMED"):
                raise RuntimeError("formal order cannot be closed by broker cancel")
            detail.update(broker_cancel_seq13=str(seq13), broker_cancel_verified_at=now)
            con.execute(
                "UPDATE execution_attempts SET finished_at=?,status=?,detail_json=? WHERE decision_id=?",
                (now, new_status, json.dumps(detail, ensure_ascii=False, default=str),
                 decision_id),
            )
            self._insert_event(con, decision_id, "BROKER_CANCEL_RECONCILED", now,
                               dict(seq13=seq13, side=side,
                                    filled_qty=int(broker_filled_qty)))
        return dict(changed=True, symbol=symbol, status=new_status, side=side)

    def ai_positions(self) -> dict[str, int]:
        with self._connect() as con:
            rows = con.execute("SELECT symbol, qty FROM ai_positions").fetchall()
        return {str(symbol): int(qty) for symbol, qty in rows}

    def unresolved_attempts(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                """SELECT decision_id, symbol, side, started_at, status
                   FROM execution_attempts WHERE finished_at IS NULL
                   ORDER BY started_at"""
            ).fetchall()
        return [dict(zip(("decision_id", "symbol", "side", "started_at", "status"), row))
                for row in rows]

    def pending_reconciliation_symbols(self, *, exclude_decision_id: str = "") -> set[str]:
        """Symbols whose later fills may not yet be reflected in ai_positions."""
        with self._connect() as con:
            rows = con.execute(
                """SELECT DISTINCT symbol FROM execution_attempts
                   WHERE status IN ('STARTED','SEND_PENDING','SUBMITTED',
                                    'ACKNOWLEDGED','PARTIALLY_FILLED','UNCONFIRMED')
                     AND decision_id<>?""",
                (exclude_decision_id,),
            ).fetchall()
        return {str(row[0]).upper() for row in rows}

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
