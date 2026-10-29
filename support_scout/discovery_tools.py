"""Read-only market exploration tools and immutable per-run history snapshots."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timezone
import math
import re
from threading import RLock

from .contracts import DataError, Rules, calendar_at, digest, load_series, timestamp, visible_series
from .pipeline import run


def _integer(value, minimum, maximum, code):
    if type(value) is not int or not minimum <= value <= maximum:
        raise DataError(code)
    return value


def _unique(values):
    result, seen = [], set()
    for value in values:
        key = digest(value)
        if key not in seen:
            seen.add(key)
            result.append(deepcopy(value))
    return result


def _audit_value(value):
    try:
        digest(value)
    except (TypeError, ValueError):
        return {"invalid_value_type": type(value).__name__}
    return deepcopy(value)


class HistoryRepository:
    """Share actual fetched histories, never another scout's conversation or choices."""

    def __init__(self, market: dict, loader, *, rules: Rules | None = None):
        if not isinstance(market, dict) or not callable(loader):
            raise DataError("DISCOVERY_MARKET_AND_LOADER_REQUIRED")
        self._market = deepcopy(market)
        cutoff = timestamp(self._market.get("as_of"))
        if cutoff > datetime.now(timezone.utc):
            raise DataError("DISCOVERY_MARKET_CUTOFF_IN_FUTURE")
        try:
            date.fromisoformat(self._market["session"])
        except (KeyError, TypeError, ValueError):
            raise DataError("DISCOVERY_MARKET_SESSION_REQUIRED") from None
        if (type(self._market.get("synthetic")) is not bool or
                not isinstance(self._market.get("catalog_id"), str) or not self._market["catalog_id"]):
            raise DataError("DISCOVERY_MARKET_IDENTITY_REQUIRED")
        rows = self._market.get("securities")
        if not isinstance(rows, list) or not rows:
            raise DataError("DISCOVERY_MARKET_SECURITIES_REQUIRED")
        self._symbols = {}
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("symbol"), str) or
                    not re.fullmatch(r"[1-9][0-9]{3}", row["symbol"]) or
                    row["symbol"] in self._symbols or row.get("session") != self._market["session"]):
                raise DataError("DISCOVERY_INVALID_MARKET_SECURITY")
            self._symbols[row["symbol"]] = row
        self.loader = loader
        self.rules = rules or Rules()
        # The owning run persists these deep copies for replay, not just summaries.
        self.bundles = {}
        self._inspections = {}
        self._errors = {}
        self._lock = RLock()

    @property
    def market(self):
        return deepcopy(self._market)

    def contains(self, symbol):
        return isinstance(symbol, str) and symbol in self._symbols

    def inspect(self, symbol: str) -> dict:
        if not self.contains(symbol):
            raise DataError("DISCOVERY_SYMBOL_NOT_IN_MARKET")
        # Serialize misses as well as cache access: even simultaneous scouts fetch once.
        with self._lock:
            if symbol in self._inspections:
                return deepcopy(self._inspections[symbol])
            if symbol in self._errors:
                raise DataError(self._errors[symbol])
            try:
                try:
                    bundle = deepcopy(self.loader(symbol))
                except Exception:
                    raise DataError("DISCOVERY_HISTORY_SOURCE_UNAVAILABLE") from None
                result = self._validate_history(symbol, bundle)
            except DataError as exc:
                self._errors[symbol] = str(exc)
                raise
            self.bundles[symbol] = bundle
            self._inspections[symbol] = result
            return deepcopy(result)

    def _validate_history(self, symbol, bundle):
        if not isinstance(bundle, dict):
            raise DataError("DISCOVERY_HISTORY_BUNDLE_REQUIRED")
        securities = bundle.get("securities")
        if (not isinstance(securities, list) or len(securities) != 1 or
                not isinstance(securities[0], dict) or securities[0].get("symbol") != symbol):
            raise DataError("DISCOVERY_HISTORY_SYMBOL_MISMATCH")
        if bundle.get("synthetic") is not self._market["synthetic"]:
            raise DataError("DISCOVERY_HISTORY_SYNTHETIC_MISMATCH")
        cutoff = timestamp(bundle.get("as_of"))
        if cutoff > datetime.now(timezone.utc):
            raise DataError("DISCOVERY_HISTORY_CUTOFF_IN_FUTURE")
        calendar = calendar_at(bundle, cutoff)
        if not calendar or calendar[-1] != self._market["session"]:
            raise DataError("DISCOVERY_HISTORY_SESSION_MISMATCH")
        try:
            report = run(bundle, rules=self.rules)
            item = report["securities"][0]
            if item["data_status"] == "REJECTED":
                raise DataError("DISCOVERY_HISTORY_DATA_REJECTED")
            series = load_series(securities[0], cutoff, calendar,
                allow_observation=bundle.get("observation_only") is True and securities[0].get("observation_only") is True)
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, DataError) and str(exc) == "DISCOVERY_HISTORY_DATA_REJECTED":
                raise
            raise DataError("DISCOVERY_HISTORY_DATA_REJECTED") from None
        bars = visible_series(series)["bars"]
        blockers = list(item.get("context_blockers", [])) + list(item["technical"].get("blockers", []))
        if "HARD_INVALIDATION" in item.get("reasons", []):
            blockers.append("HARD_INVALIDATION")
        return {"symbol": symbol, "name": series.name, "catalog_id": self._market["catalog_id"],
            "history_evidence_id": "history:" + symbol + ":" + digest(bundle)[:20],
            "as_of": bundle["as_of"], "session": self._market["session"],
            "synthetic": bundle["synthetic"], "data_status": item["data_status"],
            "technical": deepcopy(item["technical"]), "recent_bars": bars[-100:],
            "total_bar_count": len(bars), "recent_bar_limit": 100, "bars_truncated": len(bars) > 100,
            "blockers": sorted(set(blockers))}

    def combine(self, symbols) -> dict:
        """Assemble only already-inspected snapshots; never refetch or backdate bars."""
        if (not isinstance(symbols, list) or not symbols or
                any(not isinstance(s, str) for s in symbols) or len(set(symbols)) != len(symbols)):
            raise DataError("DISCOVERY_UNIQUE_NOMINEES_REQUIRED")
        with self._lock:
            if any(s not in self._inspections for s in symbols):
                raise DataError("DISCOVERY_HISTORY_NOT_INSPECTED")
            bundles = [deepcopy(self.bundles[s]) for s in symbols]
        sessions = bundles[0]["calendar"]["sessions"]
        observation_only = bundles[0].get("observation_only") is True
        for bundle in bundles:
            if (bundle["calendar"]["sessions"] != sessions or
                    calendar_at(bundle, timestamp(bundle["as_of"]))[-1] != self._market["session"]):
                raise DataError("DISCOVERY_COMBINE_CALENDAR_MISMATCH")
            if (bundle.get("observation_only") is True) != observation_only:
                raise DataError("DISCOVERY_COMBINE_PRICE_CHANNEL_MISMATCH")
        latest = max(bundles, key=lambda b: timestamp(b["as_of"]))
        combined = deepcopy(latest)
        combined["securities"] = [deepcopy(b["securities"][0]) for b in bundles]
        identity = {"catalog_id": self._market["catalog_id"],
                    "histories": [{"symbol": s, "digest": digest(b)} for s, b in zip(symbols, bundles)]}
        combined["universe_version"] = "agent-discovery-" + digest(identity)[:20]
        collections = [b.get("collection", {}) for b in bundles]
        sources = _unique(self._market.get("sources", []) +
                          [source for c in collections for source in c.get("sources", [])])
        errors = _unique(self._market.get("errors", []) +
                         [error for c in collections for error in c.get("errors", [])])
        combined["collection"] = {**deepcopy(latest.get("collection", {})),
            "status": "DEGRADED" if errors or self._market.get("status") not in (None, "OK", "COMPLETE") or
                any(c.get("status") not in (None, "OK", "COMPLETE") for c in collections) else "COMPLETE",
            "started_at": min((c.get("started_at", b["as_of"]) for c, b in zip(collections, bundles)), key=timestamp),
            "finished_at": latest["as_of"], "expected_latest_session": self._market.get("expected_session", self._market["session"]),
            "actual_latest_session": self._market["session"], "requested_symbols": list(symbols),
            "successful_symbols": list(symbols), "sources": sources, "errors": errors,
            "limitations": _unique([value for c in collections for value in c.get("limitations", [])])}
        combined["discovery_source_snapshots"] = [
            {"symbol": s, "as_of": b["as_of"], "bundle_digest": digest(b),
             "calendar": deepcopy(b["calendar"]), "collection": deepcopy(b.get("collection", {}))}
            for s, b in zip(symbols, bundles)]
        combined["discovery_market_snapshot"] = {key: deepcopy(self._market[key]) for key in
            ("catalog_id", "as_of", "session", "sources", "coverage", "status", "errors") if key in self._market}
        return combined


class DiscoveryTools:
    """One scout's capability and evidence ledger; budgets include failed attempts."""

    SORTS = {"symbol", "turnover_desc", "change_asc", "change_abs_asc"}

    def __init__(self, repository: HistoryRepository, *, max_tool_calls=12, max_history_symbols=4):
        self.repository = repository
        self.max_tool_calls = _integer(max_tool_calls, 1, 1000, "DISCOVERY_INVALID_TOOL_BUDGET")
        self.max_history_symbols = _integer(max_history_symbols, 1, 100, "DISCOVERY_INVALID_HISTORY_BUDGET")
        self.audit = []
        self.seen_symbols = set()
        self.inspected = {}
        self.tool_call_count = 0
        self._history_attempts = set()
        self._lock = RLock()

    def execute(self, name, args) -> dict:
        with self._lock:
            self.tool_call_count += 1
            try:
                if self.tool_call_count > self.max_tool_calls:
                    raise DataError("DISCOVERY_TOOL_BUDGET_EXHAUSTED")
                if not isinstance(name, str) or name not in {"market_overview", "search_market", "inspect_support"}:
                    raise DataError("DISCOVERY_UNKNOWN_TOOL")
                if not isinstance(args, dict):
                    raise DataError("DISCOVERY_TOOL_ARGUMENTS_MUST_BE_OBJECT")
                if name == "market_overview":
                    if args:
                        raise DataError("DISCOVERY_UNEXPECTED_TOOL_ARGUMENT")
                    value = self._overview()
                elif name == "search_market":
                    value = self._search(args)
                else:
                    if set(args) != {"symbol"}:
                        raise DataError("DISCOVERY_INVALID_INSPECT_ARGUMENTS")
                    symbol = args["symbol"]
                    if not self.repository.contains(symbol):
                        raise DataError("DISCOVERY_SYMBOL_NOT_IN_MARKET")
                    if symbol not in self._history_attempts and len(self._history_attempts) >= self.max_history_symbols:
                        raise DataError("DISCOVERY_HISTORY_BUDGET_EXHAUSTED")
                    self._history_attempts.add(symbol)
                    value = self.repository.inspect(symbol)
                    self.inspected[symbol] = deepcopy(value)
                    self.seen_symbols.add(symbol)
                result = {**value, "status": "OK"}
            except DataError as exc:
                result = {"status": "ERROR", "error": str(exc)}
            except Exception:
                result = {"status": "ERROR", "error": "DISCOVERY_TOOL_FAILED"}
            event = {"sequence": self.tool_call_count, "name": _audit_value(name), "arguments": _audit_value(args),
                     "result": deepcopy(result), "result_digest": digest(result)}
            self.audit.append(event)
            return deepcopy(result)

    def _overview(self):
        market = self.repository.market
        counts = Counter(str(row.get("industry") or "UNKNOWN") for row in market["securities"])
        return {"catalog_id": market["catalog_id"], "as_of": market["as_of"], "session": market["session"],
            "market_status": market.get("status"), "expected_session": market.get("expected_session", market["session"]),
            "data_errors": deepcopy(market.get("errors", [])),
            "synthetic": market["synthetic"], "market_total": len(market["securities"]),
            "scope": market.get("scope", "TWSE_LISTED_COMMON_STOCKS"),
            "market_status": market.get("status", "UNKNOWN"),
            "market_coverage": deepcopy(market.get("coverage", {})),
            "market_limitations": deepcopy(market.get("limitations", [])),
            "industries": [{"industry": name, "count": counts[name]} for name in sorted(counts)],
            "history_inspections": len(self.inspected), "history_symbols_limit": self.max_history_symbols,
            "tool_calls_remaining": max(0, self.max_tool_calls - self.tool_call_count)}

    def _search(self, args):
        allowed = {"industry", "query", "sort", "min_turnover_twd", "offset", "limit"}
        if set(args) - allowed:
            raise DataError("DISCOVERY_UNEXPECTED_TOOL_ARGUMENT")
        industry, query = args.get("industry"), args.get("query")
        for text in (industry, query):
            if text is not None and (not isinstance(text, str) or not text.strip() or len(text) > 100):
                raise DataError("DISCOVERY_INVALID_MARKET_FILTER")
        sort = args.get("sort", "symbol")
        if not isinstance(sort, str) or sort not in self.SORTS:
            raise DataError("DISCOVERY_INVALID_MARKET_SORT")
        minimum = args.get("min_turnover_twd", 0)
        if type(minimum) not in (int, float) or not math.isfinite(minimum) or minimum < 0:
            raise DataError("DISCOVERY_INVALID_TURNOVER_FILTER")
        offset = _integer(args.get("offset", 0), 0, 1_000_000, "DISCOVERY_INVALID_MARKET_OFFSET")
        limit = _integer(args.get("limit", 20), 1, 50, "DISCOVERY_INVALID_MARKET_LIMIT")
        market = self.repository.market
        rows = market["securities"]
        matching = [row for row in rows if
            (industry is None or row.get("industry") == industry.strip()) and
            (query is None or query.strip().casefold() in (str(row.get("symbol", "")) + " " + str(row.get("name", ""))).casefold()) and
            row.get("turnover_twd", 0) >= minimum]
        if sort == "symbol":
            matching.sort(key=lambda row: row["symbol"])
        elif sort == "turnover_desc":
            matching.sort(key=lambda row: (-row["turnover_twd"], row["symbol"]))
        else:
            def change_order(row):
                value = row.get("change_pct")
                valid = type(value) in (int, float) and math.isfinite(value)
                return (not valid, (abs(value) if sort == "change_abs_asc" else value) if valid else 0, row["symbol"])
            matching.sort(key=change_order)
        selected = matching[offset:offset + limit]
        self.seen_symbols.update(row["symbol"] for row in selected)
        return {"catalog_id": market["catalog_id"], "as_of": market["as_of"], "session": market["session"],
            "market_status": market.get("status"), "expected_session": market.get("expected_session", market["session"]),
            "market_status": market.get("status", "UNKNOWN"),
            "market_total": len(rows), "matched_total": len(matching), "offset": offset,
            "limit": limit, "returned_count": len(selected), "has_more": offset + len(selected) < len(matching),
            "securities": deepcopy(selected)}
