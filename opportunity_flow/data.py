"""Credential-free input boundary; no imports from the legacy application."""
from __future__ import annotations

import csv
from datetime import date
import hashlib
from pathlib import Path

from .common import digest, number, read, stamp


def load_dataset(folder):
    folder = Path(folder)
    if (folder / ".collecting").exists():
        raise ValueError("DATASET_UPDATE_IN_PROGRESS")
    manifest = read(folder / "manifest.json")
    for name, expected in manifest.get("files", {}).items():
        candidate = (folder / name).resolve()
        if not candidate.is_relative_to(folder.resolve()):
            raise ValueError("DATASET_PATH_ESCAPE")
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != expected:
            raise ValueError("DATASET_HASH_MISMATCH:" + name)
    with (folder / "bars.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    with (folder / "sessions.csv").open(encoding="utf-8-sig", newline="") as f:
        sessions = [date.fromisoformat(r["date"]).isoformat() for r in csv.DictReader(f)]
    if sessions != sorted(set(sessions)):
        raise ValueError("CALENDAR_NOT_UNIQUE_SORTED")
    symbols = manifest.get("symbols") or sorted({r["symbol"] for r in rows})
    grouped = {s: [] for s in symbols}
    errors = {s: [] for s in symbols}
    seen = set()
    for row in rows:
        symbol = row.get("symbol", "UNKNOWN")
        if symbol not in grouped:
            raise ValueError("ROW_OUTSIDE_DECLARED_UNIVERSE")
        grouped.setdefault(symbol, [])
        errors.setdefault(symbol, [])
        try:
            day = date.fromisoformat(row["date"]).isoformat()
            if row["exchange"] not in ("TWSE", "TPEX") or (symbol, day) in seen:
                raise ValueError("INVALID_EXCHANGE_OR_DUPLICATE_BAR")
            seen.add((symbol, day))
            values = {k: number(row[k], True) for k in ("open", "high", "low", "close")}
            if not values["low"] <= min(values["open"], values["close"]) <= max(values["open"], values["close"]) <= values["high"]:
                raise ValueError("INVALID_OHLC")
            volume = number(row["volume"])
            if not volume.is_integer():
                raise ValueError("VOLUME_NOT_INTEGER_SHARES")
            amount = None if row.get("turnover_twd") in (None, "") else number(row["turnover_twd"])
            grouped[symbol].append(dict(symbol=symbol, exchange=row["exchange"], date=day,
                **values, volume=int(volume), turnover_twd=amount, source=row.get("source", "UNKNOWN"),
                source_sha256=row.get("source_sha256", ""), retrieved_at=row.get("retrieved_at"),
                price_basis=row.get("price_basis", "RAW_UNADJUSTED_UNVERIFIED_ACTIONS")))
        except (KeyError, ValueError, TypeError) as exc:
            errors[symbol].append(dict(date=row.get("date"), reason=str(exc)))
    for bars in grouped.values():
        bars.sort(key=lambda b: b["date"])
    if (folder/"rejected.json").exists():
        for error in read(folder/"rejected.json"):
            if error.get("symbol") in errors:
                errors[error["symbol"]].append(dict(date=error.get("date"),reason=error.get("reason","SOURCE_REJECTED")))
    return dict(grouped=grouped, errors=errors, sessions=sessions, manifest=manifest,
                manifest_sha256=digest(manifest), master=read(folder / "master.json") if (folder / "master.json").exists() else {})


def quote_map(path):
    if not path:
        return {}
    obj = read(path)
    rows = obj if isinstance(obj, list) else obj.get("quotes", [])
    result = {}
    for q in rows:
        key = (q.get("exchange"), q.get("symbol"))
        if key in result:
            raise ValueError("DUPLICATE_QUOTE")
        result[key] = q
    return result


def validate_quote(q, symbol, exchange, at, max_age):
    if not q:
        return None, "ODDLOT_QUOTE_MISSING"
    try:
        if q.get("symbol") != symbol or q.get("exchange") != exchange:
            raise ValueError("QUOTE_SYMBOL_OR_EXCHANGE_MISMATCH")
        if q.get("quote_status") != "LIVE" or q.get("is_trial") is not False:
            raise ValueError("LIVE_NON_TRIAL_QUOTE_REQUIRED")
        if q.get("quote_basis") != "capital_intraday_odd_lot":
            raise ValueError("VERIFIED_ODDLOT_QUOTE_BASIS_REQUIRED")
        when = stamp(q["quote_timestamp"])
        age = (stamp(at) - when).total_seconds()
        if when.date() != stamp(at).date() or not 0 <= age <= max_age:
            raise ValueError("QUOTE_STALE_OR_FUTURE")
        bid, ask, last = (number(q[k], True) for k in ("bid", "ask", "last_price"))
        if bid > ask:
            raise ValueError("CROSSED_QUOTE")
        return dict(bid=bid, ask=ask, last_price=last, quote_timestamp=when.isoformat(),
                    source=q.get("source", "CAPITAL_SKCOM")), None
    except (ValueError, TypeError, KeyError) as exc:
        return None, str(exc)


def validate_reference(q,symbol,exchange,at,max_age):
    if not q or q.get("quote_status")!="PUBLIC_REFERENCE":
        return None
    try:
        if q.get("symbol")!=symbol or q.get("exchange")!=exchange or q.get("quote_basis")!="TWSE_MIS_BOARD_LOT_NOT_ODDLOT":
            return None
        when=stamp(q["quote_timestamp"])
        if when.date()!=stamp(at).date() or not 0<=(stamp(at)-when).total_seconds()<=max_age:
            return None
        return dict(price=number(q["last_price"],True),basis=q["quote_basis"])
    except (ValueError,TypeError,KeyError):
        return None
