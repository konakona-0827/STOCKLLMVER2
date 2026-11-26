"""Credential-free canonical data and explicit, opt-in FinMind downloads."""
from __future__ import annotations

import csv
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

FIELDS = ("symbol", "exchange", "date", "open", "high", "low", "close", "volume",
          "turnover_twd", "retrieved_at", "source", "source_sha256", "price_basis")


def digest(content):
    return hashlib.sha256(content).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def new_directory(path):
    path = Path(path).resolve()
    path.mkdir(parents=True, exist_ok=False)
    return path


def write_csv(path, fields, rows):
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def number(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError("BOOLEAN_IS_NOT_NUMERIC")
    result = float(value)
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        raise ValueError("INVALID_NUMERIC_VALUE")
    return result


def normalize(rows, symbol, exchange, retrieved_at, source_hash):
    result, rejected, seen = [], [], set()
    for item in rows:
        try:
            day = date.fromisoformat(item["date"]).isoformat()
            if str(item["stock_id"]) != symbol or day in seen:
                raise ValueError("WRONG_SYMBOL_OR_DUPLICATE_DATE")
            seen.add(day)
            prices = {key: number(item[src], positive=True) for key, src in
                      (("open", "open"), ("high", "max"), ("low", "min"), ("close", "close"))}
            if not prices["low"] <= min(prices["open"], prices["close"]) <= max(prices["open"], prices["close"]) <= prices["high"]:
                raise ValueError("INVALID_OHLC")
            volume = number(item["Trading_Volume"])
            if not volume.is_integer():
                raise ValueError("VOLUME_MUST_BE_INTEGER_SHARES")
            # Trading_turnover is the number of trades, NOT traded monetary value.
            amount = None if item.get("Trading_money") is None else number(item["Trading_money"])
            result.append(dict(symbol=symbol, exchange=exchange, date=day, **prices,
                               volume=int(volume), turnover_twd=amount, retrieved_at=retrieved_at,
                               source="FinMind/TaiwanStockPrice", source_sha256=source_hash,
                               price_basis="RAW_UNADJUSTED_UNVERIFIED_ACTIONS"))
        except (KeyError, TypeError, ValueError) as exc:
            rejected.append(dict(symbol=symbol, date=item.get("date"), reason=str(exc)))
    return sorted(result, key=lambda b: b["date"]), rejected


class FinMind:
    """No .env loading. An optional token is read from the process environment only."""
    def __init__(self):
        self.requests = []

    def fetch(self, dataset, destination, **params):
        query = dict(dataset=dataset, **params)
        url = "https://api.finmindtrade.com/api/v4/data?" + urlencode(query)
        headers = {"User-Agent": "STOCKLLM-selection-lab/1.0", "Accept": "application/json"}
        token = os.environ.get("FINMIND_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
        started = datetime.now(timezone.utc).isoformat()
        try:
            with urlopen(Request(url, headers=headers), timeout=45) as response:
                raw = response.read()
        except HTTPError as exc:
            raise RuntimeError(f"FINMIND_HTTP_{exc.code}: no automatic retries or paid fallback") from None
        payload = json.loads(raw)
        if payload.get("status") != 200 or not isinstance(payload.get("data"), list):
            raise RuntimeError(f"FINMIND_DATASET_UNAVAILABLE: {dataset}")
        Path(destination).write_bytes(raw)
        record = dict(dataset=dataset, params=params, url=url, retrieved_at=started,
                      sha256=digest(raw), rows=len(payload["data"]),
                      authentication="environment_token" if token else "anonymous")
        self.requests.append(record)
        return payload["data"], record


def download(output, symbols, start, end):
    start, end = date.fromisoformat(start), date.fromisoformat(end)
    # Date-only history must have closed before today in Taiwan. Latest-day inclusion
    # is deliberately a separate future adapter with a verified publication time.
    from datetime import timedelta
    taipei_today = datetime.now(timezone(timedelta(hours=8))).date()
    if start > end or end >= taipei_today:
        raise ValueError("END_MUST_PRECEDE_TODAY_IN_TAIWAN")
    if not symbols or len(symbols) > 50 or len(set(symbols)) != len(symbols):
        raise ValueError("REQUIRE_1_TO_50_UNIQUE_SYMBOLS")
    if any(not s.isdigit() or len(s) != 4 for s in symbols):
        raise ValueError("COMMON_STOCK_SYMBOLS_REQUIRE_FOUR_DIGITS")
    out = new_directory(output)
    raw_dir = out / "sources"
    raw_dir.mkdir()
    api = FinMind()
    master_rows, _ = api.fetch("TaiwanStockInfo", raw_dir / "master.json")
    # No date parameters: this endpoint's date is a master-record update date.
    master = {}
    for row in sorted(master_rows, key=lambda r: r.get("date", "")):
        if str(row.get("stock_id")) in symbols:
            master[str(row["stock_id"])] = row
    for symbol in symbols:
        item = master.get(symbol, {})
        if item.get("type") not in ("twse", "tpex") or "ETF" in item.get("industry_category", "").upper():
            raise ValueError("UNKNOWN_OR_NON_COMMON_STOCK: " + symbol)
    sessions, _ = api.fetch("TaiwanStockTradingDate", raw_dir / "calendar.json",
                           start_date=str(start), end_date=str(end))
    days = sorted({row["date"] for row in sessions if str(start) <= row["date"] <= str(end)})
    all_bars, rejected = [], []
    for symbol in symbols:
        rows, provenance = api.fetch("TaiwanStockPrice", raw_dir / (symbol + ".json"),
                                     data_id=symbol, start_date=str(start), end_date=str(end))
        bars, errors = normalize(rows, symbol, master[symbol]["type"].upper(),
                                 provenance["retrieved_at"], provenance["sha256"])
        all_bars.extend(bars)
        rejected.extend(errors)
    write_csv(out / "bars.csv", FIELDS, all_bars)
    write_csv(out / "sessions.csv", ["date"], [dict(date=d) for d in days])
    write_json(out / "master.json", master)
    write_json(out / "rejected.json", rejected)
    manifest = dict(schema_version=1, symbols=symbols, start=str(start), end=str(end),
                    rows=len(all_bars), rejected_rows=len(rejected), requests=api.requests,
                    price_basis="RAW_UNADJUSTED_UNVERIFIED_ACTIONS", point_in_time_certified=False,
                    universe="CURRENT_FIXED_RESEARCH_POOL_NOT_HISTORICAL_MEMBERSHIP",
                    publication_time_verified=False, corporate_actions_verified=False,
                    files={name: digest((out / name).read_bytes()) for name in
                           ("bars.csv", "sessions.csv", "master.json", "rejected.json")})
    write_json(out / "manifest.json", manifest)
    return manifest


def load_dataset(path):
    folder = Path(path)
    manifest = read_json(folder / "manifest.json")
    for filename, expected in manifest["files"].items():
        if digest((folder / filename).read_bytes()) != expected:
            raise ValueError("DATASET_HASH_MISMATCH: " + filename)
    with (folder / "sessions.csv").open(encoding="utf-8-sig", newline="") as stream:
        sessions = [row["date"] for row in csv.DictReader(stream)]
    if not sessions or sessions != sorted(set(sessions)):
        raise ValueError("INVALID_SESSION_CALENDAR")
    calendar = {day: i for i, day in enumerate(sessions)}
    grouped, seen = {}, set()
    with (folder / "bars.csv").open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            key = (row["symbol"], row["date"])
            if key in seen or row["date"] not in calendar:
                raise ValueError("DUPLICATE_BAR_OR_NON_SESSION_DATE")
            seen.add(key)
            for field in ("open", "high", "low", "close"):
                row[field] = number(row[field], positive=True)
            if not row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"]:
                raise ValueError("INVALID_OHLC")
            volume = number(row["volume"])
            if not volume.is_integer():
                raise ValueError("VOLUME_MUST_BE_INTEGER_SHARES")
            row["volume"] = int(volume)
            row["turnover_twd"] = None if row["turnover_twd"] == "" else number(row["turnover_twd"])
            row["session_index"] = calendar[row["date"]]
            grouped.setdefault(row["symbol"], []).append(row)
    return {s: sorted(b, key=lambda r: r["date"]) for s, b in grouped.items()}, sessions, manifest


def export_adapters(dataset, output):
    grouped, sessions, manifest = load_dataset(dataset)
    out = new_directory(output)
    for name in ("backtesting", "qlib_raw_features"):
        (out / name).mkdir()
    for symbol, bars in grouped.items():
        write_csv(out / "backtesting" / (symbol + ".csv"),
                  ["Date", "Open", "High", "Low", "Close", "Volume"],
                  [dict(Date=b["date"], **{f.capitalize(): b[f] for f in ("open", "high", "low", "close", "volume")}) for b in bars])
        write_csv(out / "qlib_raw_features" / ("tw" + symbol + ".csv"),
                  ["date", "open", "high", "low", "close", "volume"], bars)
    # No fake factor=1: raw diagnostics are not a certified adjusted Qlib dataset.
    write_json(out / "manifest.json", dict(source_manifest_sha256=digest((Path(dataset) / "manifest.json").read_bytes()),
               mode="RAW_FEATURE_DIAGNOSTIC_ONLY", adjusted_qlib_training_ready=False,
               missing=["verified_adjusted_OHLCV", "restoration_factor", "historical_membership", "historical_publication_times"],
               symbols=len(grouped), sessions=len(sessions), rows=manifest["rows"]))
    return out
