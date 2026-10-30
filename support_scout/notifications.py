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
ENV_PASSWORD_KEYS = {"DEVICEPW", "GOOGLE_DEVEICE_PW", "JP_DESK_GMAIL_APP_PASSWORD"}


def configure(address, path=DEFAULT_CONFIG, *, secret_env_file=None, password_env=None):
    if not isinstance(address, str) or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", address):
        raise DataError("INVALID_NOTIFICATION_EMAIL")
    value = {"schema_version": 1, "enabled": True, "sender": address, "recipient": address,
        "password_env": "JP_DESK_GMAIL_APP_PASSWORD", "password_file": str((Path(path).parent / "gmail_app_password.txt").resolve())}
    if secret_env_file is not None:
        value.update(password_env="DEVICEPW", secret_env_file=str(Path(secret_env_file).expanduser().resolve()))
    if password_env is not None:
        if password_env not in ENV_PASSWORD_KEYS:
            raise DataError("INVALID_EMAIL_SECRET_REFERENCE")
        value["password_env"] = password_env
    atomic_json(Path(path), value)
    return value


def load_config(path=DEFAULT_CONFIG):
    if not Path(path).exists():
        return None
    value = strict_json_loads(Path(path).read_text(encoding="utf-8"))
    expected = {"schema_version", "enabled", "sender", "recipient", "password_env", "password_file"}
    if (not isinstance(value, dict) or set(value) - expected - {"secret_env_file"} or not expected <= set(value)
            or value["schema_version"] != 1 or type(value["enabled"]) is not bool):
        raise DataError("INVALID_EMAIL_CONFIGURATION")
    for field in ("sender", "recipient"):
        if not isinstance(value[field], str) or not re.fullmatch(r"[^\s@\r\n]+@[^\s@\r\n]+\.[^\s@\r\n]+", value[field]):
            raise DataError("INVALID_NOTIFICATION_EMAIL")
    if (not isinstance(value["password_env"], str)
            or not re.fullmatch(r"JP_DESK_[A-Z_]+|DEVICEPW|GOOGLE_DEVEICE_PW", value["password_env"])
            or not isinstance(value["password_file"], str)):
        raise DataError("INVALID_EMAIL_SECRET_REFERENCE")
    if "secret_env_file" in value:
        if (not isinstance(value["secret_env_file"], str) or not value["secret_env_file"].strip()
                or value["password_env"] not in ENV_PASSWORD_KEYS):
            raise DataError("INVALID_EMAIL_SECRET_REFERENCE")
        selected = Path(value["secret_env_file"]).expanduser()
        value["secret_env_file"] = str((selected if selected.is_absolute() else Path(path).parent / selected).resolve())
    return value


def _device_password(path, password_env="DEVICEPW"):
    """Read one explicitly authorized dotenv field; never import other settings.

    This parser intentionally does not perform interpolation, escapes, shell
    evaluation, or automatic discovery of other .env files.
    """
    if password_env not in ENV_PASSWORD_KEYS:
        return "", "GMAIL_ENV_KEY_NOT_ALLOWED"
    found = None
    try:
        with Path(path).open(encoding="utf-8-sig") as stream:
            for line in stream:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.removeprefix("export ").split("=", 1)
                if key.strip().casefold() != password_env.casefold():
                    continue
                if found is not None:
                    return "", password_env + "_DUPLICATE"
                value = value.strip()
                if value.startswith(("'", '"')):
                    quote = value[0]
                    closing = value.find(quote, 1)
                    if closing < 0 or (value[closing + 1:].strip() and not value[closing + 1:].lstrip().startswith("#")):
                        return "", password_env + "_INVALID_FORMAT"
                    value = value[1:closing]
                else:
                    value = value.split(" #", 1)[0].strip()
                found = value
    except (OSError, UnicodeError):
        return "", password_env + "_ENV_FILE_UNREADABLE"
    return (found, None) if found is not None else ("", password_env + "_NOT_CONFIGURED")


def _password_details(config):
    value = os.environ.get(config["password_env"], "")
    source, error = "PROCESS_ENV", None
    validate_device = config["password_env"] in {"DEVICEPW", "GOOGLE_DEVEICE_PW"}
    if not value and config.get("secret_env_file"):
        value, error = _device_password(config["secret_env_file"], config["password_env"])
        source, validate_device = ("DEVICEPW_ENV_FILE" if config["password_env"] == "DEVICEPW" else "AUTHORIZED_ENV_FILE"), True
    elif not value:
        source = "LEGACY_PASSWORD_FILE"
        try:
            value = Path(config["password_file"]).read_text(encoding="utf-8-sig") if Path(config["password_file"]).is_file() else ""
        except (OSError, UnicodeError):
            return "", source, "GMAIL_PASSWORD_FILE_UNREADABLE"
    value = value.replace(" ", "").strip()
    if not error and validate_device and not re.fullmatch(r"[A-Za-z0-9]{16}", value):
        error = config["password_env"] + ("_INVALID_FORMAT" if value else "_NOT_CONFIGURED")
    return value, source if value or error else "NOT_CONFIGURED", error


def _password(config):
    value, _, error = _password_details(config)
    if error:
        raise DataError(error)
    return value


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
            opportunity = item.get("opportunity_decision") or {}
            opportunity = {key: deepcopy(opportunity[key]) for key in
                ("action", "label", "pattern_id", "reference_plan", "manual_checks", "policy_version") if key in opportunity}
            stable = {"symbol": item["symbol"], "action": action.get("action", opportunity.get("action", decision.get("conclusion", "WAIT"))),
                "conclusion": decision.get("conclusion"), "quantity": held.get(item["symbol"], {}).get("quantity", 0),
                "patterns": [(p["pattern_id"], p["stage"]) for p in item.get("patterns", [])],
                "reasons": sorted(decision.get("reason_codes", []))}
            if opportunity:
                stable["opportunity_decision"] = opportunity
            if item.get("execution_plan"):
                stable["decision_brief"] = {k: deepcopy(v) for k,v in item["execution_plan"].items() if k != "as_of"}
            fingerprint = digest(stable)
            previous = db.execute("SELECT fingerprint,as_of FROM states WHERE symbol=?", (item["symbol"],)).fetchone()
            if previous and (epoch < previous[1] or previous[0] == fingerprint):
                continue
            event_id = digest({"state": stable, "as_of": report["as_of"]})
            payload = {**stable, "as_of": report["as_of"], "summary": decision.get("summary", ""),
                "reference_plan": decision.get("reference_plan"), "position": held.get(item["symbol"]),
                "report": str(Path(report_path).resolve()), "execution_enabled": False}
            if report.get("decision_brief"):
                payload["budget"] = {k: report["decision_brief"][k] for k in ("capital_jpy", "per_trade_risk_budget_jpy",
                    "proposed_total_jpy", "proposed_loss_scenario_jpy", "available_cash_estimate_jpy")}
                payload["desk_counts"] = deepcopy(report.get("opportunity_summary", {}).get("counts", {}))
            # A newer state replaces an unsent older recommendation, while its
            # audit record stays available. Never resend uncertain delivery.
            db.execute("UPDATE events SET status='SUPERSEDED' WHERE symbol=? AND status='PENDING'", (item["symbol"],))
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
    value, source, error = _password_details(config) if config else ("", "NOT_CONFIGURED", None)
    ready = bool(config and config["enabled"] and value and not error)
    status = ("DISABLED" if not config or not config["enabled"] else "READY" if ready else
        "NEEDS_GMAIL_APP_PASSWORD" if error is None or error.endswith("_NOT_CONFIGURED") else "GMAIL_CREDENTIAL_SOURCE_ERROR")
    return {"status": status, "recipient": config["recipient"] if config else None,
        "credential_source": source, "credential_configured": bool(value and not error),
        "credential_error": error, "counts": counts, "execution_enabled": False}


def decision_email_text(value):
    brief = value.get("decision_brief") or {}
    budget = value.get("budget") or {}
    number = lambda x: f"{x:,.2f}" if type(x) in (int, float) else "待確認"
    lines = ["日股三Agent決策摘要（人工操作，沒有自動下單）", "資料截止：" + value.get("as_of", "待確認"), "",
        f"標的：{value.get('symbol','—')} {brief.get('name','')}",
        "結論：" + brief.get("label", value.get("action", "等待核對")),
        "建議新增投入：" + number(brief.get("suggested_amount_jpy")) + " 日圓",
        "換算股數：" + number(brief.get("suggested_shares")) + " 股（金額指定僅為換算）",
        "買進條件：" + brief.get("entry_condition", "沒有完整進場計畫，不依此通知買進。"), "",
        "參考進場：" + number(brief.get("entry_reference")),
        "固定失效價：" + number(brief.get("invalidation_reference")),
        "主要阻力參考：" + number(brief.get("target_reference")),
        "本筆情境虧損：" + number(brief.get("estimated_loss_jpy")) + " 日圓",
        "單筆虧損上限：" + number(budget.get("per_trade_risk_budget_jpy", brief.get("risk_budget_jpy"))) + " 日圓",
        "資金設定：" + number(budget.get("capital_jpy")) + " 日圓（非券商餘額）",
        "試算狀態：" + brief.get("sizing_status", "尚未建立"), "",
        "判斷說明：" + value.get("summary", "詳見報告。")]
    if value.get("desk_counts"):
        c = value["desk_counts"]
        lines.extend(["", f"本輪：可評估試單 {c.get('PROBE_BUY_REVIEW',0)} 檔、等待 {c.get('WAIT',0)} 檔、暫不進場 {c.get('AVOID',0)} 檔。"])
    checks = brief.get("manual_checks", [])
    if checks:
        lines.extend(["", "人工核對：", *["- " + x for x in checks]])
    lines.extend(["", "參考價需依下一交易時段重算；失效價不是保證成交的停損單，跳空可能超過情境虧損。",
        "只有你確認實際成交後才更新持倉。", "本機報告：" + value.get("report", "—")])
    return "\n".join(lines)


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
    message.set_content(decision_email_text(value))
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
