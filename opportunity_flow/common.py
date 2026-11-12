from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
from uuid import uuid4

TAIPEI = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]


def stamp(value):
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(dt, datetime) or dt.tzinfo is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return dt.astimezone(TAIPEI)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8",newline="\n")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def number(value, positive=False):
    if isinstance(value, bool):
        raise ValueError("BOOLEAN_IS_NOT_NUMERIC")
    n = float(str(value).replace(",", ""))
    if not math.isfinite(n) or n < 0 or (positive and n == 0):
        raise ValueError("INVALID_NUMBER")
    return n


def policy(path=None):
    p = read(path or Path(__file__).with_name("policy.json"))
    required = set(read(Path(__file__).with_name("policy.json")))
    if set(p) != required:
        raise ValueError("POLICY_FIELDS_MISMATCH")
    for key, value in p.items():
        if isinstance(value, (int, float)):
            number(value, positive=True)
    integers=("history_sessions","minimum_history_sessions","holding_sessions","quote_max_age_seconds",
              "review_max_age_seconds","failed_review_retry_seconds","numeric_recheck_seconds","model_timeout_seconds","model_batch_size")
    if any(type(p[k]) is not int for k in integers):
        raise ValueError("POLICY_INTEGER_REQUIRED")
    if not (21 <= p["minimum_history_sessions"] <= p["history_sessions"] <= 250
            and p["holding_sessions"]<p["minimum_history_sessions"]
            and 0 < p["allocation_fraction"] <= 1
            and 0 < p["maximum_position_risk_fraction"] < 1
            and 0 < p["material_price_change_fraction"] < 1
            and 1 <= p["model_batch_size"] <= 10):
        raise ValueError("INVALID_POLICY")
    if any(not isinstance(p[k],str) or not p[k].strip() for k in ("model","version","daily_refresh_taipei","weekly_refresh")):
        raise ValueError("POLICY_TEXT_REQUIRED")
    return p
