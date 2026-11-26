"""Append-only, versioned forward PAPER ledger fed by credential-free snapshots.

No broker transport is accepted. Activation/rollback appends control events;
old accounts and decisions stay intact. Different versions never share cash.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

from .experiment import digest, load_protocol, validate_protocol
from .market import Costs, Market, capital_quote, decimal, instant
from .simulation import pool_blocks, simulate, setup_tags


def known_bars(bars, at, sessions):
    cutoff = instant(at)
    seen, result = set(), []
    market = Market()
    for original in bars:
        b = dict(original)
        key = (b["symbol"], b["date"])
        if key in seen or b["date"] not in sessions:
            raise ValueError("DUPLICATE_OR_UNVERIFIED_SESSION")
        seen.add(key)
        closed = datetime.fromisoformat(b["date"] + "T" + market.rules["regular_close"]).replace(tzinfo=market.tz)
        if closed > cutoff or not closed <= instant(b["available_at"]) <= instant(b["retrieved_at"]) <= cutoff:
            raise ValueError("FUTURE_OR_UNAVAILABLE_BAR")
        if not isinstance(b.get("source_sha256"), str) or len(b["source_sha256"]) != 64:
            raise ValueError("SOURCE_DIGEST_REQUIRED")
        for k in ("open", "high", "low", "close"):
            b[k] = float(decimal(b[k]))
        if not 0 < b["low"] <= min(b["open"], b["close"]) <= max(b["open"], b["close"]) <= b["high"]:
            raise ValueError("INVALID_OHLC")
        if type(b["volume"]) is not int or b["volume"] <= 0 or decimal(b["turnover_twd"]) <= 0:
            raise ValueError("INVALID_LIQUIDITY")
        b["turnover_twd"] = float(decimal(b["turnover_twd"]))
        b["session_index"] = sessions.index(b["date"])
        result.append(b)
    return sorted(result, key=lambda b: (b["symbol"], b["date"]))


class ForwardStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        root = Path(__file__).resolve().parents[2] / "research-runs"
        if not self.path.is_relative_to(root):
            raise ValueError("PAPER_STORE_MUST_BE_UNDER_RESEARCH_RUNS")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS versions (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, config TEXT NOT NULL, source TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS source_archives (version TEXT NOT NULL REFERENCES versions(id), path TEXT NOT NULL,
                  sha256 TEXT NOT NULL, content TEXT NOT NULL, PRIMARY KEY(version,path));
              CREATE TABLE IF NOT EXISTS controls (id INTEGER PRIMARY KEY, version TEXT NOT NULL REFERENCES versions(id), at TEXT NOT NULL, active INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, version TEXT NOT NULL REFERENCES versions(id), at TEXT NOT NULL,
                  snapshot_hash TEXT NOT NULL, result TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY, run TEXT NOT NULL REFERENCES runs(id), version TEXT NOT NULL,
                  at TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('BUY','SELL')), qty INTEGER NOT NULL CHECK(qty>0),
                  price TEXT NOT NULL, amount TEXT NOT NULL, basis TEXT NOT NULL, stop TEXT, target TEXT, expires TEXT,
                  release_date TEXT, evidence_id TEXT NOT NULL);
            """)
            for table in ("versions", "source_archives", "controls", "runs", "fills"):
                for operation in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def register(self, protocol=None, at=None):
        protocol = validate_protocol(protocol or load_protocol())
        if protocol["mode"] != "PAPER_ONLY":
            raise ValueError("PAPER_ONLY_REQUIRED")
        at = at or datetime.now(timezone.utc).isoformat()
        instant(at)
        folder = Path(__file__).parent
        source = {p.name: digest(p.read_bytes()) for p in folder.glob("*") if p.is_file()}
        version = digest(dict(protocol=protocol, source=source))[:20]
        with self.connect() as db:
            db.execute("INSERT INTO versions VALUES(?,?,?,?)", (version, at, json.dumps(protocol), json.dumps(source)))
            for name, sha in source.items():
                db.execute("INSERT INTO source_archives VALUES(?,?,?,?)", (version, name, sha, (folder / name).read_text(encoding="utf-8")))
            db.execute("INSERT INTO controls(version,at,active) VALUES(?,?,1)", (version, at))
        return version

    def set_active(self, version, active, at=None):
        at = at or datetime.now(timezone.utc).isoformat()
        instant(at)
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM versions WHERE id=?", (version,)).fetchone():
                raise ValueError("UNKNOWN_VERSION")
            latest = db.execute("SELECT at FROM controls WHERE version=? ORDER BY id DESC LIMIT 1", (version,)).fetchone()
            if latest and instant(at) < instant(latest[0]):
                raise ValueError("CONTROL_TIME_MUST_BE_MONOTONIC")
            db.execute("INSERT INTO controls(version,at,active) VALUES(?,?,?)", (version, at, int(bool(active))))

    def _account(self, db, version, protocol, day):
        cash, unsettled, positions = decimal(protocol["initial_cash_twd"]), Decimal(0), {}
        daily = Decimal(0)
        for row in db.execute("SELECT * FROM fills WHERE version=? ORDER BY id", (version,)):
            qty, amount, basis = row["qty"], decimal(row["amount"]), decimal(row["basis"])
            if row["side"] == "BUY":
                cash -= amount
                daily += amount if row["at"][:10] == day else 0
                if row["symbol"] in positions:
                    raise ValueError("DUPLICATE_OPEN_POSITION")
                positions[row["symbol"]] = dict(qty=qty, basis=basis, stop=decimal(row["stop"]),
                                               target=decimal(row["target"]), expires=row["expires"])
            else:
                if row["release_date"] <= day:
                    cash += amount
                else:
                    unsettled += amount
                position = positions[row["symbol"]]
                position["qty"] -= qty
                position["basis"] -= basis
                if not position["qty"]:
                    del positions[row["symbol"]]
        return cash, unsettled, daily, positions

    def cycle(self, version, snapshot, at=None):
        at = at or datetime.now(timezone.utc).isoformat()
        market = Market()
        clock = instant(at)
        day = clock.astimezone(market.tz).date().isoformat()
        sessions = snapshot["sessions"]
        if sessions != sorted(set(sessions)) or day not in sessions:
            raise ValueError("VERIFIED_SESSION_CALENDAR_REQUIRED")
        if snapshot.get("mode") != "PAPER_ONLY" or snapshot.get("historical_membership_verified") is not True:
            raise ValueError("PAPER_AND_MEMBERSHIP_ATTESTATION_REQUIRED")
        if not 0 <= (clock - instant(snapshot["at"])).total_seconds() <= 10:
            raise ValueError("STALE_OR_FUTURE_SNAPSHOT")
        session_index = sessions.index(day)
        # Need enough future calendar to model expiry and settlement, never prices.
        bars = known_bars(snapshot["bars"], at, sessions)
        grouped = {}
        for b in bars:
            grouped.setdefault(b["symbol"], []).append(b)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ver = db.execute("SELECT * FROM versions WHERE id=?", (version,)).fetchone()
            if not ver or clock < instant(ver["started_at"]):
                raise ValueError("FORWARD_START_REQUIRED")
            # Never run a historical source version through newer Python code.
            current = {p.name: digest(p.read_bytes()) for p in Path(__file__).parent.glob("*") if p.is_file()}
            if json.loads(ver["source"]) != current:
                raise ValueError("SOURCE_CHANGED_REGISTER_NEW_VERSION_OR_RESTORE_OLD_SOURCE")
            control = db.execute("SELECT * FROM controls WHERE version=? ORDER BY id DESC LIMIT 1", (version,)).fetchone()
            if not control["active"] or clock < instant(control["at"]):
                raise ValueError("VERSION_INACTIVE")
            previous = db.execute("SELECT at FROM runs WHERE version=? ORDER BY rowid DESC LIMIT 1", (version,)).fetchone()
            if previous and clock <= instant(previous["at"]):
                raise ValueError("FORWARD_TIME_MUST_INCREASE")
            protocol = validate_protocol(json.loads(ver["config"]))
            if protocol["mode"] != "PAPER_ONLY":
                raise ValueError("PAPER_ONLY_REQUIRED")
            if len(sessions) <= session_index + protocol["horizon"] + market.rules["settlement_sessions"]:
                raise ValueError("EXPIRY_AND_SETTLEMENT_CALENDAR_REQUIRED")
            costs = Costs(protocol["costs"])
            cash, unsettled, daily, positions = self._account(db, version, protocol, day)
            quotes, reasons = {}, []
            seen = set()
            for raw in snapshot["quotes"]:
                if raw["symbol"] in seen:
                    raise ValueError("DUPLICATE_QUOTE")
                seen.add(raw["symbol"])
                try:
                    quotes[raw["symbol"]] = capital_quote(raw, at, sessions, protocol, market)
                except (ValueError, KeyError, TypeError) as exc:
                    reasons.append(dict(symbol=raw["symbol"], status="QUOTE_REJECTED", reason=str(exc)))
            fills = []
            # Exits are evaluated even if new entries lack sufficient history.
            for symbol, p in list(positions.items()):
                q = quotes.get(symbol)
                if not q or not (q["bid"] <= p["stop"] or q["bid"] >= p["target"] or day >= p["expires"]):
                    continue
                qty = min(p["qty"], q["bid_size_shares"])
                price = costs.fill_price(q["bid"], "SELL", market, day)
                if price < q["limit_down"]:
                    reasons.append(dict(symbol=symbol, status="MODELED_SLIPPAGE_OUTSIDE_LIMIT"))
                    continue
                amount = costs.amount(price, qty, "SELL")
                basis = p["basis"] * qty / p["qty"]
                release = sessions[session_index + market.rules["settlement_sessions"]]
                fills.append(dict(symbol=symbol, side="SELL", qty=qty, price=str(price), amount=str(amount), basis=str(basis),
                                  stop=None, target=None, expires=None, release_date=release, evidence_id=q["evidence_id"]))
                unsettled += amount
                p["qty"] -= qty
                p["basis"] -= basis
                if not p["qty"]:
                    del positions[symbol]
            candidates = []
            prior = sessions[session_index - 1] if session_index else None
            for symbol, history in grouped.items():
                if len(history) < 61 or history[-1]["date"] != prior:
                    reasons.append(dict(symbol=symbol, status="INCOMPLETE_HISTORY"))
                    continue
                window = history[-61:]
                if any(b["session_index"] != a["session_index"] + 1 for a, b in zip(window, window[1:])):
                    reasons.append(dict(symbol=symbol, status="GAPPED_HISTORY"))
                    continue
                if sum(b["turnover_twd"] for b in history[-20:]) / 20 < protocol["minimum_turnover_twd"]:
                    reasons.append(dict(symbol=symbol, status="INSUFFICIENT_LIQUIDITY"))
                    continue
                score = history[-1]["close"] / history[-21]["close"] - 1
                candidates.append((score, symbol))
            # Frozen top K before quote/model exclusions; no lower-rank replacement.
            for _, symbol in sorted(candidates, key=lambda r: (-r[0], r[1]))[:protocol["top_k"]]:
                history, q = grouped[symbol], quotes.get(symbol)
                if symbol in positions or any(f["symbol"] == symbol for f in fills) or not q:
                    continue
                if len(positions) >= protocol["max_positions"]:
                    reasons.append(dict(symbol=symbol, status="MAX_POSITIONS"))
                    continue
                pool = pool_blocks(history, history[-1]["date"], protocol, protocol["block_size"])
                result = simulate(pool, float(q["ask"]), day, protocol)
                reasons.append(dict(symbol=symbol, simulation=result, status="SIMULATION_REVIEW"))
                if result["status"] != "OK" or result["expected_net_twd"] <= 0:
                    continue
                entry = costs.fill_price(q["ask"], "BUY", market, day)
                if entry > q["limit_up"]:
                    reasons.append(dict(symbol=symbol, status="MODELED_SLIPPAGE_OUTSIDE_LIMIT"))
                    continue
                stop = market.round_price(entry * (1 - decimal(protocol["stop_fraction"])), day)
                target = market.round_price(entry * (1 + decimal(protocol["target_fraction"])), day, up=True)
                budget = min(decimal(protocol["notional_twd"]), decimal(protocol["max_daily_buy_twd"]) - daily)
                qty = costs.size(entry, cash, budget, decimal(protocol["initial_cash_twd"]) * decimal(protocol["risk_fraction"]), stop,
                                 max_qty=min(market.rules["odd_lot_max"], q["ask_size_shares"], int(history[-1]["volume"] * decimal(protocol["max_participation"]))))
                if not qty:
                    reasons.append(dict(symbol=symbol, status="BUDGET_OR_LIQUIDITY"))
                    continue
                amount = costs.amount(entry, qty, "BUY")
                cash -= amount
                daily += amount
                fills.append(dict(symbol=symbol, side="BUY", qty=qty, price=str(entry), amount=str(amount), basis=str(amount),
                                  stop=str(stop), target=str(target), expires=sessions[session_index + protocol["horizon"]],
                                  release_date=None, evidence_id=q["evidence_id"]))
                positions[symbol] = dict(qty=qty, basis=amount, stop=stop, target=target)
            if cash < 0 or daily > decimal(protocol["max_daily_buy_twd"]):
                raise AssertionError("PAPER_ACCOUNTING_INVARIANT")
            snap_hash = digest(snapshot)
            run_id = digest([version, snapshot["at"], snap_hash])[:24]
            result = dict(mode="PAPER_ONLY", version=version, at=at, run_id=run_id, cash_twd=str(cash),
                          unsettled_twd=str(unsettled), simulated_fills=fills, decisions=reasons,
                          execution_model="SIDE_QUOTE_DEPTH_PROXY_NO_QUEUE_CERTIFICATION", llm_api_cost_usd=0)
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?)", (run_id, version, at, snap_hash, json.dumps(result)))
            for f in fills:
                db.execute("INSERT INTO fills(run,version,at,symbol,side,qty,price,amount,basis,stop,target,expires,release_date,evidence_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (run_id, version, clock.astimezone(market.tz).isoformat(), *(f[k] for k in ("symbol", "side", "qty", "price", "amount", "basis", "stop", "target", "expires", "release_date", "evidence_id"))))
        return result
