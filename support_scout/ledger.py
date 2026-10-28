"""Append observation history and retain support boundaries across daily snapshots.

This ledger tracks research episodes, never positions, cash or orders. Historical
price corrections require review; they cannot silently redraw a recorded zone.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from uuid import uuid4

from .contracts import DataError, market_clock, digest, number, timestamp

VALID_DATA_STATUSES = {"VALID", "OBSERVATION", "OBSERVATION_ONLY"}

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, as_of TEXT NOT NULL,
        as_of_epoch REAL NOT NULL, recorded_at TEXT NOT NULL, rules_hash TEXT NOT NULL,
        report_digest TEXT NOT NULL, universe_version TEXT, summary_json TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS episodes (
        episode_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, rules_hash TEXT NOT NULL,
        price_basis TEXT NOT NULL, anchor_at TEXT NOT NULL, frozen_anchor REAL NOT NULL,
        frozen_lower REAL NOT NULL, frozen_upper REAL NOT NULL, first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL, last_as_of_epoch REAL NOT NULL,
        last_session TEXT NOT NULL, last_close REAL NOT NULL, current_state TEXT NOT NULL,
        needs_revision_review INTEGER NOT NULL DEFAULT 0, invalidated_session TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS episodes_symbol_rules_basis
        ON episodes(symbol, rules_hash, price_basis)""",
    """CREATE TABLE IF NOT EXISTS current_view (
        symbol TEXT NOT NULL, rules_hash TEXT NOT NULL, price_basis TEXT NOT NULL,
        episode_id TEXT, current_state TEXT NOT NULL, last_session TEXT NOT NULL,
        last_as_of_epoch REAL NOT NULL, last_close REAL NOT NULL,
        PRIMARY KEY(symbol, rules_hash, price_basis)
    )""",
    """CREATE TABLE IF NOT EXISTS observations (
        observation_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
        snapshot_id TEXT NOT NULL, symbol TEXT NOT NULL, session TEXT NOT NULL,
        rules_hash TEXT NOT NULL, price_basis TEXT NOT NULL, episode_id TEXT,
        as_of TEXT NOT NULL, data_status TEXT NOT NULL, payload_digest TEXT NOT NULL,
        change TEXT NOT NULL, rows_json TEXT NOT NULL,
        UNIQUE(snapshot_id, symbol, session, rules_hash, price_basis)
    )""",
)


def _basis(item: dict) -> str:
    metadata = item.get("metadata")
    value = item.get("price_basis") or (metadata.get("price_basis") if isinstance(metadata, dict) else None)
    return value if isinstance(value, str) and value else "UNKNOWN"


def _event(symbol, episode, previous, current, change, session, close, **extra):
    return {"symbol": symbol, "episode_id": episode["episode_id"] if episode else None,
        "previous_state": previous, "current_state": current, "change": change,
        "session": session, "latest_close": close,
        "frozen_lower": episode["frozen_lower"] if episode else None,
        "frozen_upper": episode["frozen_upper"] if episode else None, **extra}


def _support(technical: dict) -> dict | None:
    support = technical.get("support")
    if support is None:
        return None
    if not isinstance(support, dict):
        raise DataError("LEDGER_INVALID_SUPPORT")
    try:
        anchor_at = date.fromisoformat(support["anchor_at"]).isoformat()
        values = {k: number(support.get(k), "support_" + k) for k in ("anchor", "lower", "upper")}
    except (KeyError, TypeError, ValueError) as exc:
        raise DataError("LEDGER_INVALID_SUPPORT") from exc
    if not values["lower"] <= values["anchor"] <= values["upper"]:
        raise DataError("LEDGER_INVALID_SUPPORT_RANGE")
    return {"anchor_at": anchor_at, **values}


def _episode_id(symbol, rules_hash, basis, anchor_at):
    # Anchor price and band are deliberately absent: a revised price on the same
    # anchor date must be identified as a correction to the existing episode.
    return digest({"symbol": symbol, "rules_hash": rules_hash,
                   "price_basis": basis, "anchor_at": anchor_at})


def _is_revised(episode, support):
    return any(not math.isclose(episode["frozen_" + key], support[key], rel_tol=1e-10, abs_tol=1e-10)
               for key in ("anchor", "lower", "upper"))


def _record_item(conn, item, *, run_id, snapshot_id, rules_hash, as_of, cutoff_epoch, cutoff_day):
    symbol = item.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        raise DataError("LEDGER_SYMBOL_REQUIRED")
    basis = _basis(item)
    technical = item.get("technical")
    if not isinstance(technical, dict):
        technical = {}
    data_status = item.get("data_status", "REJECTED")
    valid = data_status in VALID_DATA_STATUSES
    session = technical.get("latest_session") if valid else None
    session = session or cutoff_day
    try:
        session = date.fromisoformat(session).isoformat()
    except (TypeError, ValueError) as exc:
        raise DataError("LEDGER_INVALID_SESSION") from exc
    if session > cutoff_day:
        raise DataError("LEDGER_FUTURE_SESSION")
    payload = {"data_status": data_status, "technical": technical, "price_basis": basis}
    payload_digest = digest(payload)
    existing = conn.execute("""SELECT * FROM observations WHERE snapshot_id=? AND symbol=?
        AND session=? AND rules_hash=? AND price_basis=?""",
        (snapshot_id, symbol, session, rules_hash, basis)).fetchone()
    current = conn.execute("""SELECT * FROM current_view WHERE symbol=? AND rules_hash=?
        AND price_basis=?""", (symbol, rules_hash, basis)).fetchone()
    if current is None and not valid:
        current = conn.execute("""SELECT * FROM current_view WHERE symbol=? AND rules_hash=?
            ORDER BY last_as_of_epoch DESC LIMIT 1""", (symbol, rules_hash)).fetchone()
    previous_episode = conn.execute("SELECT * FROM episodes WHERE episode_id=?", (current["episode_id"],)).fetchone() if current and current["episode_id"] else None
    previous_state = current["current_state"] if current else None
    if existing:
        if existing["payload_digest"] != payload_digest:
            raise DataError("LEDGER_SNAPSHOT_CONTENT_CONFLICT")
        # Deduplication cannot return a historic approval after newer observations
        # invalidated or revised it. Preserve the append-only observation row,
        # but bind this invocation's decision to the current ledger state.
        if current and (session < current["last_session"] or cutoff_epoch < current["last_as_of_epoch"]):
            return [_event(symbol, previous_episode, previous_state, previous_state,
                "STALE_IGNORED", session, None,
                revision_review=bool(previous_episode and previous_episode["needs_revision_review"]))]
        rows = []
        for row in json.loads(existing["rows_json"]):
            episode = conn.execute("SELECT * FROM episodes WHERE episode_id=?", (row.get("episode_id"),)).fetchone()
            rows.append({**row, "change": "DUPLICATE",
                "current_state": episode["current_state"] if episode else row["current_state"],
                "revision_review": bool(episode and episode["needs_revision_review"])})
        return rows
    if not valid:
        rows = [_event(symbol, previous_episode, previous_state, previous_state,
                       "DATA_REJECTED", session, None)]
    elif current and (session < current["last_session"] or cutoff_epoch < current["last_as_of_epoch"]):
        rows = [_event(symbol, previous_episode, previous_state, previous_state,
                       "STALE_IGNORED", session, None)]
    else:
        close = number(technical.get("latest_close"), "latest_close")
        state = technical.get("state")
        if not isinstance(state, str) or not state:
            raise DataError("LEDGER_STATE_REQUIRED")
        support = _support(technical)
        if support and support["anchor_at"] > session:
            raise DataError("LEDGER_FUTURE_ANCHOR")
        event_map = {}
        # Every previously observed support remains frozen even when the current
        # detector moves to a different episode or no longer returns a setup.
        for old in conn.execute("""SELECT * FROM episodes WHERE symbol=? AND rules_hash=?
                AND price_basis=? AND current_state!='INVALIDATED'""", (symbol, rules_hash, basis)).fetchall():
            if session < old["last_session"] or cutoff_epoch < old["last_as_of_epoch"]:
                continue
            if close < old["frozen_lower"]:
                conn.execute("""UPDATE episodes SET current_state='INVALIDATED', invalidated_session=?,
                    last_seen_at=?, last_as_of_epoch=?, last_session=?, last_close=? WHERE episode_id=?""",
                    (session, as_of, cutoff_epoch, session, close, old["episode_id"]))
                event_map[old["episode_id"]] = _event(symbol, old, old["current_state"], "INVALIDATED",
                                                     "INVALIDATED", session, close)
        selected_id = None
        if support:
            selected_id = _episode_id(symbol, rules_hash, basis, support["anchor_at"])
            episode = conn.execute("SELECT * FROM episodes WHERE episode_id=?", (selected_id,)).fetchone()
            if episode is None:
                new_state = "INVALIDATED" if close < support["lower"] or state == "INVALIDATED" else state
                conn.execute("""INSERT INTO episodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (selected_id, symbol, rules_hash, basis, support["anchor_at"], support["anchor"],
                     support["lower"], support["upper"], as_of, as_of, cutoff_epoch, session, close,
                     new_state, 0, session if new_state == "INVALIDATED" else None))
                episode = conn.execute("SELECT * FROM episodes WHERE episode_id=?", (selected_id,)).fetchone()
                event_map[selected_id] = _event(symbol, episode, None, new_state, "NEW", session, close)
            else:
                original = event_map.get(selected_id)
                old_state = original["previous_state"] if original else episode["current_state"]
                revised = _is_revised(episode, support)
                needs_review = revised or bool(episode["needs_revision_review"])
                invalidated = episode["current_state"] == "INVALIDATED" or state == "INVALIDATED" or close < episode["frozen_lower"]
                new_state = "INVALIDATED" if invalidated else "REVISION_REVIEW" if needs_review else state
                change = ("INVALIDATED" if invalidated and old_state != "INVALIDATED" else
                          "REVISION_REVIEW" if revised else "CHANGED" if new_state != old_state else "UNCHANGED")
                conn.execute("""UPDATE episodes SET current_state=?, needs_revision_review=?,
                    invalidated_session=COALESCE(invalidated_session, ?), last_seen_at=?, last_as_of_epoch=?,
                    last_session=?, last_close=? WHERE episode_id=?""",
                    (new_state, int(needs_review), session if invalidated else None, as_of, cutoff_epoch,
                     session, close, selected_id))
                event_map[selected_id] = _event(symbol, episode, old_state, new_state, change,
                    session, close, revision_review=needs_review)
        elif previous_episode:
            selected_id = previous_episode["episode_id"]
            episode = conn.execute("SELECT * FROM episodes WHERE episode_id=?", (selected_id,)).fetchone()
            if selected_id not in event_map:
                event_map[selected_id] = _event(symbol, episode, previous_state, episode["current_state"],
                    "NO_CURRENT_SETUP", session, close)
            conn.execute("""UPDATE episodes SET last_seen_at=?,last_as_of_epoch=?,last_session=?,last_close=?
                WHERE episode_id=?""", (as_of, cutoff_epoch, session, close, selected_id))
        if selected_id:
            current_state = conn.execute("SELECT current_state FROM episodes WHERE episode_id=?", (selected_id,)).fetchone()[0]
        else:
            current_state = state
            event_map["no-setup"] = _event(symbol, None, previous_state, state,
                "CHANGED" if previous_state and state != previous_state else "NO_SETUP", session, close)
        conn.execute("""INSERT INTO current_view VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol,rules_hash,price_basis) DO UPDATE SET episode_id=excluded.episode_id,
            current_state=excluded.current_state,last_session=excluded.last_session,
            last_as_of_epoch=excluded.last_as_of_epoch,last_close=excluded.last_close""",
            (symbol, rules_hash, basis, selected_id, current_state, session, cutoff_epoch, close))
        rows = list(event_map.values())
    conn.execute("""INSERT INTO observations(run_id,snapshot_id,symbol,session,rules_hash,price_basis,
        episode_id,as_of,data_status,payload_digest,change,rows_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, snapshot_id, symbol, session, rules_hash, basis, rows[-1]["episode_id"], as_of,
         str(data_status), payload_digest, rows[-1]["change"], json.dumps(rows, ensure_ascii=False, allow_nan=False)))
    return rows


def record_observation(report: dict, db_path: Path, *, before_commit=None) -> dict:
    """Atomically record a real-data report and return changes for a daily view.

    Synthetic reports are rejected before opening the database. Per-security
    malformed observations are isolated with savepoints and cannot alter state.
    SQLite serializes concurrent writers; every invocation has a separate run ID.
    An optional before_commit(summary) hook may publish the report atomically.
    Hook failures propagate and roll back the entire run, including other stocks.
    """
    if not isinstance(report, dict) or report.get("synthetic") is not False:
        raise DataError("LEDGER_REAL_DATA_REQUIRED")
    if report.get("schema_version") != 1 or not isinstance(report.get("rules"), dict) or not report["rules"]:
        raise DataError("LEDGER_INVALID_REPORT_CONTRACT")
    snapshot_id = report.get("snapshot_id")
    items = report.get("securities")
    if not isinstance(snapshot_id, str) or not snapshot_id or not isinstance(items, list) or not items:
        raise DataError("LEDGER_SNAPSHOT_AND_SECURITIES_REQUIRED")
    if before_commit is not None and not callable(before_commit):
        raise DataError("LEDGER_BEFORE_COMMIT_MUST_BE_CALLABLE")
    cutoff = timestamp(report.get("as_of"))
    cutoff_epoch = cutoff.timestamp()
    cutoff_day = cutoff.astimezone(market_clock(report.get("market", "TW"))[0]).date().isoformat()
    rules_hash = digest(report["rules"])
    report_digest = digest(report)
    run_id = uuid4().hex
    summary = {"run_id": run_id, "status": "COMPLETE", "new": 0, "changed": 0,
        "invalidated": 0, "revision": 0, "duplicate": 0, "stale": 0, "errors": [], "rows": []}
    counts = Counter(item.get("symbol") for item in items if isinstance(item, dict) and isinstance(item.get("symbol"), str))
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise DataError("LEDGER_UNSUPPORTED_SCHEMA")
        for statement in SCHEMA:
            conn.execute(statement)
        conn.execute("PRAGMA user_version=1")
        conn.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)", (run_id, snapshot_id, cutoff.isoformat(),
            cutoff_epoch, datetime.now(timezone.utc).isoformat(), rules_hash, report_digest,
            str(report.get("universe_version", "unknown")), None))
        for index, item in enumerate(items):
            conn.execute("SAVEPOINT item_record")
            try:
                if not isinstance(item, dict) or not isinstance(item.get("symbol"), str):
                    raise DataError("LEDGER_INVALID_SECURITY")
                if counts[item["symbol"]] != 1:
                    raise DataError("LEDGER_DUPLICATE_SYMBOL")
                rows = _record_item(conn, item, run_id=run_id, snapshot_id=snapshot_id, rules_hash=rules_hash,
                    as_of=cutoff.isoformat(), cutoff_epoch=cutoff_epoch, cutoff_day=cutoff_day)
                summary["rows"].extend(rows)
            except (DataError, KeyError, TypeError, ValueError) as exc:
                conn.execute("ROLLBACK TO item_record")
                summary["errors"].append({"symbol": item.get("symbol") if isinstance(item, dict) else f"item-{index}",
                    "error": str(exc) if isinstance(exc, DataError) else type(exc).__name__})
            finally:
                conn.execute("RELEASE item_record")
        summary["new"] = sum(row["change"] == "NEW" for row in summary["rows"])
        summary["changed"] = sum(row["change"] == "CHANGED" for row in summary["rows"])
        summary["invalidated"] = sum(row["change"] == "INVALIDATED" for row in summary["rows"])
        summary["revision"] = sum(row["change"] == "REVISION_REVIEW" or row.get("revision_review", False) for row in summary["rows"])
        summary["duplicate"] = sum(row["change"] == "DUPLICATE" for row in summary["rows"])
        summary["stale"] = sum(row["change"] == "STALE_IGNORED" for row in summary["rows"])
        if summary["errors"]:
            summary["status"] = "DEGRADED"
        conn.execute("UPDATE runs SET summary_json=? WHERE run_id=?",
                     (json.dumps(summary, ensure_ascii=False, allow_nan=False), run_id))
        if before_commit is not None:
            before_commit(summary)
        # Publication may attach the ledger summary or final run status to the
        # report. Bind the audit digest to that final published representation.
        conn.execute("UPDATE runs SET report_digest=? WHERE run_id=?", (digest(report), run_id))
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        if conn is not None and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise DataError("LEDGER_WRITE_FAILED:" + type(exc).__name__) from None
    except Exception:
        if conn is not None and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        if conn is not None:
            conn.close()
    return summary
