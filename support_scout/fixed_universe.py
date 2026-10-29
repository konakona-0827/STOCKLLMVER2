"""Configurable broker-replaceable pool, while SEARCH still chooses the candidates."""
from datetime import datetime
import hashlib
from pathlib import Path

from .contracts import DataError, TOKYO, digest, strict_json_loads
from .japan import normalize_symbol

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "config" / "universe_jp.json"
PROTOCOL = "jp-fixed-universe-v1"


def load_fixed_universe(path=DEFAULT_FILE, *, now=None):
    path = Path(path)
    raw = path.read_bytes()
    value = strict_json_loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict) or not isinstance(value.get("version"), str) or not value["version"].strip():
        raise DataError("FIXED_UNIVERSE_VERSION_REQUIRED")
    cap = value.get("max_symbols", 20)
    if type(cap) is not int or not 1 <= cap <= 100:
        raise DataError("FIXED_UNIVERSE_CAP_MUST_BE_1_TO_100")
    rows = value.get("symbols")
    if not isinstance(rows, list) or not 1 <= len(rows) <= cap:
        raise DataError("FIXED_UNIVERSE_SIZE_EXCEEDS_CONFIGURED_CAP")
    symbols = [normalize_symbol(s) for s in rows]
    if len(symbols) != len(set(symbols)):
        raise DataError("FIXED_UNIVERSE_DUPLICATE_SYMBOL")
    clock = now or datetime.now(TOKYO)
    if clock.tzinfo is None:
        raise DataError("FIXED_UNIVERSE_AWARE_TIME_REQUIRED")
    checksum = hashlib.sha256(raw).hexdigest()
    return {"protocol": PROTOCOL, "universe_id": "fixed-jp-" + digest(value)[:24], "market": "JP",
        "as_of": clock.astimezone(TOKYO).date().isoformat(), "available_at": clock.isoformat(),
        "sha256": checksum, "max_symbols": cap, "symbols": symbols,
        "items": [{"symbol": s, "name": s} for s in symbols],
        "source": {"type": "USER_CONFIGURED_FIXED_POOL", "file": str(path.resolve()), "sha256": checksum},
        "version": value["version"], "broker_eligibility": value.get("broker_eligibility", "NOT_YET_VERIFIED"),
        "limitations": ["FIXED_POOL_NOT_FULL_MARKET", "PAYPAY_ELIGIBILITY_AWAITING_USER_LIST", "NO_TRADING_PERMISSION"]}
