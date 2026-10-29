"""Decision-change outbox and optional Gmail delivery; never a trading interface."""
from copy import deepcopy
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
import json
import os
from pathlib import Path
import re
import smtplib
import sqlite3
import ssl

from .contracts import DataError, digest, strict_json_loads, timestamp
from .storage import atomic_json

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "local_data" / "email_settings.json"
DEFAULT_DB = ROOT / "local_data" / "notifications.sqlite3"


def configure(address, path=DEFAULT_CONFIG):
    if not isinstance(address, str) or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", address):
        raise DataError("INVALID_NOTIFICATION_EMAIL")
    value = {"schema_version": 1, "enabled": True, "sender": address, "recipient": address,
        "password_env": "JP_DESK_GMAIL_APP_PASSWORD", "password_file": str((Path(path).parent / "gmail_app_password.txt").resolve())}
    atomic_json(Path(path), value)
    return value


def load_config(path=DEFAULT_CONFIG):
    if not Path(path).exists():
        return None
    value = strict_json_loads(Path(path).read_text(encoding="utf-8"))
    expected = {"schema_version", "enabled", "sender", "recipient", "password_env", "password_file"}
    if not isinstance(value, dict) or set(value) != expected or value["schema_version"] != 1 or type(value["enabled"]) is not bool:
        raise DataError("INVALID_EMAIL_CONFIGURATION")
    for field in ("sender", "recipient"):
        if not isinstance(value[field], str) or not re.fullmatch(r"[^\s@\r\n]+@[^\s@\r\n]+\.[^\s@\r\n]+", value[field]):
            raise DataError("INVALID_NOTIFICATION_EMAIL")
    if not re.fullmatch(r"JP_DESK_[A-Z_]+", value["password_env"]) or not isinstance(value["password_file"], str):
        raise DataError("INVALID_EMAIL_SECRET_REFERENCE")
    return value


def _password(config):
    value = os.environ.get(config["password_env"], "")
    if not value and Path(config["password_file"]).is_file():
        value = Path(config["password_file"]).read_text(encoding="utf-8-sig").strip()
    return value.replace(" ", "").strip()


def _connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS states(symbol TEXT PRIMARY KEY, fingerprint TEXT, as_of REAL)")
    db.execute("""CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, symbol TEXT, as_of TEXT,
        payload TEXT, status TEXT, error TEXT, sent_at TEXT)""")
    db.commit()
    return db


def enqueue_report(report, report_path, db_path=DEFAULT_DB):
    context = report.get("analysis_context", {})
    if (report.get("synthetic") is not False or report.get("desk", {}).get("mode") not in {None, "openai"}
            or context.get("mode", "").startswith("RETROSPECTIVE")
            or any(p.get("core", {}).get("analysis_context", {}).get("mode", "").startswith("RETROSPECTIVE") for p in report.get("all_packets", []))):
        return {"status": "SKIPPED_CASE_OR_SYNTHETIC", "queued": []}
    selected = {s["symbol"] for s in (report.get("discovery", {}).get("search") or {}).get("selection", [])}
    selected.update(report.get("desk", {}).get("selected_symbols", []))
    held = report.get("portfolio_snapshot", {}).get("positions", {})
    selected.update(held)
    epoch = timestamp(report["as_of"]).timestamp()
    queued = []
    db = _connect(db_path)
    try:
        db.execute("BEGIN IMMEDIATE")
        for item in report["securities"]:
            if item["symbol"] not in selected:
                continue
            action = item.get("manual_action", {})
            decision = item.get("trade_decision", {})
            stable = {"symbol": item["symbol"], "action": action.get("action", decision.get("conclusion", "WAIT")),
                "conclusion": decision.get("conclusion"), "quantity": held.get(item["symbol"], {}).get("quantity", 0),
                "patterns": [(p["pattern_id"], p["stage"]) for p in item.get("patterns", [])],
                "reasons": sorted(decision.get("reason_codes", []))}
            fingerprint = digest(stable)
            previous = db.execute("SELECT fingerprint,as_of FROM states WHERE symbol=?", (item["symbol"],)).fetchone()
            if previous and (epoch < previous[1] or previous[0] == fingerprint):
                continue
            event_id = digest({"state": stable, "as_of": report["as_of"]})
            payload = {**stable, "as_of": report["as_of"], "summary": decision.get("summary", ""),
                "reference_plan": decision.get("reference_plan"), "position": held.get(item["symbol"]),
                "report": str(Path(report_path).resolve()), "execution_enabled": False}
            db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)",
                (event_id, item["symbol"], report["as_of"], json.dumps(payload, ensure_ascii=False, allow_nan=False), "PENDING", None, None))
            db.execute("INSERT OR REPLACE INTO states VALUES(?,?,?)", (item["symbol"], fingerprint, epoch))
            queued.append(event_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"status": "QUEUED" if queued else "UNCHANGED", "queued": queued}


def email_status(config_path=DEFAULT_CONFIG, db_path=DEFAULT_DB):
    config = load_config(config_path)
    db = _connect(db_path)
    try:
        counts = dict(db.execute("SELECT status,count(*) FROM events GROUP BY status"))
    finally:
        db.close()
    ready = bool(config and config["enabled"] and _password(config))
    return {"status": "READY" if ready else "NEEDS_GMAIL_APP_PASSWORD" if config and config["enabled"] else "DISABLED",
        "recipient": config["recipient"] if config else None, "counts": counts, "execution_enabled": False}


def send_email(event, config):
    password = _password(config)
    if not password:
        raise DataError("GMAIL_APP_PASSWORD_REQUIRED")
    value = strict_json_loads(event["payload"])
    message = EmailMessage()
    message["From"], message["To"] = config["sender"], config["recipient"]
    message["Subject"] = f"[日股決策] {value['symbol']} · {value['action']}"
    message["Message-ID"] = f"<{event['id']}@japan-support-desk.local>"
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message.set_content("日股三Agent決策更新（人工評估，沒有自動下單）\n\n" +
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15, context=ssl.create_default_context()) as smtp:
        smtp.login(config["sender"], password)
        smtp.send_message(message)


def flush(config_path=DEFAULT_CONFIG, db_path=DEFAULT_DB, *, sender=None, limit=5):
    if type(limit) is not int or not 1 <= limit <= 20:
        raise DataError("INVALID_EMAIL_DELIVERY_LIMIT")
    config = load_config(config_path)
    status = email_status(config_path, db_path)
    if status["status"] != "READY":
        return {**status, "sent": 0}
    sender = sender or send_email
    db, sent, errors = _connect(db_path), 0, []
    try:
        db.row_factory = sqlite3.Row
        for _ in range(limit):
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM events WHERE status='PENDING' ORDER BY as_of,id LIMIT 1").fetchone()
            if row is None:
                db.commit()
                break
            db.execute("UPDATE events SET status='SENDING',error=NULL WHERE id=?", (row["id"],))
            db.commit()
            try:
                sender(dict(row), config)
            except (smtplib.SMTPAuthenticationError, DataError):
                db.execute("UPDATE events SET status='PENDING',error='GMAIL_AUTHENTICATION_REQUIRED' WHERE id=?", (row["id"],))
                db.commit()
                errors.append("GMAIL_AUTHENTICATION_REQUIRED")
                break
            except Exception:
                # SMTP may have accepted the message before a disconnect. Do not auto-repeat.
                db.execute("UPDATE events SET status='UNKNOWN',error='DELIVERY_UNCONFIRMED' WHERE id=?", (row["id"],))
                db.commit()
                errors.append("DELIVERY_UNCONFIRMED_MANUAL_REVIEW_REQUIRED")
                break
            else:
                db.execute("UPDATE events SET status='SENT',sent_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), row["id"]))
                db.commit()
                sent += 1
    finally:
        db.close()
    return {"status": "ERROR" if errors else "COMPLETE", "sent": sent, "errors": errors}


def after_report(report, directory, *, config_path=DEFAULT_CONFIG, db_path=DEFAULT_DB):
    """Delivery is a sidecar after ledger/report commit; never changes the audited report."""
    try:
        queue = enqueue_report(report, Path(directory) / "report.html", db_path)
        delivery = flush(config_path, db_path) if not queue["status"].startswith("SKIPPED") else {"status": "SKIPPED", "sent": 0}
        result = {"queue": queue, "delivery": delivery}
    except Exception:
        result = {"status": "NOTIFICATION_FAILED", "error": "LOCAL_NOTIFICATION_ERROR"}
    try:
        atomic_json(Path(directory) / "email_delivery.json", result)
    except OSError:
        result = {"status": "NOTIFICATION_FAILED", "error": "DELIVERY_STATUS_UNWRITABLE"}
    return result
