"""Head of Desk: allocate bounded research, bind identities, enforce gates, record audit."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from uuid import uuid4

from .agents import TASK_PROTOCOL, validate_reviews, verify_packet
from .contracts import DataError, Rules, digest, timestamp
from .pipeline import run
from .providers import ANSWER_FIELDS, Completion, OfflineProvider, ProviderError


@dataclass(frozen=True)
class DeskConfig:
    max_packets: int = 5
    max_calls: int = 10

    def __post_init__(self):
        if type(self.max_packets) is not int or not 1 <= self.max_packets <= 50:
            raise DataError("MAX_PACKETS_MUST_BE_1_TO_50")
        if type(self.max_calls) is not int or not 2 <= self.max_calls <= 100:
            raise DataError("MAX_CALLS_MUST_BE_2_TO_100")


def _call(provider, packet: dict, role: str, run_id: str) -> tuple[dict | None, dict]:
    agent_id = f"{run_id}:{role}:{uuid4().hex}"
    audit = {"packet_id": packet["packet_id"], "symbol": packet["core"]["symbol"],
        "role": role, "agent_id": agent_id, "model": provider.model,
        "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        isolated = deepcopy(packet)
        verify_packet(isolated)
        completion = provider.complete(isolated, role)
        verify_packet(isolated)
        if not isinstance(completion, Completion) or not isinstance(completion.answer, dict) or set(completion.answer) != ANSWER_FIELDS:
            raise ProviderError("INVALID_PROVIDER_COMPLETION")
        review = {**deepcopy(completion.answer), "packet_id": packet["packet_id"], "role": role,
            "agent_id": agent_id, "produced_at": datetime.now(timezone.utc).isoformat()}
        accepted, errors = validate_reviews(packet, [review])
        if errors or role not in accepted:
            raise ProviderError("REVIEW_CONTRACT_REJECTED:" + ",".join(errors))
        if provider.mode == "offline_simulation" and review["verdict"] != "WATCH":
            raise ProviderError("OFFLINE_PROMOTION_REFUSED")
        audit.update(status="ACCEPTED", response_id=completion.response_id, usage=completion.usage,
                     review_digest=digest(review))
        return review, audit
    except Exception as exc:
        # Controlled errors only; never persist arbitrary exception text.
        code = str(exc) if isinstance(exc, (ProviderError, DataError)) else type(exc).__name__
        audit.update(status="ERROR", error=code)
        return None, audit
    finally:
        audit["finished_at"] = datetime.now(timezone.utc).isoformat()


def run_desk(bundle: dict, *, researcher=None, reviewer=None, config: DeskConfig | None = None,
             rules: Rules | None = None, as_of: str | None = None, selected_symbols: list[str] | None = None) -> dict:
    config = config or DeskConfig()
    researcher = researcher or OfflineProvider()
    reviewer = reviewer or OfflineProvider()
    modes = {researcher.mode, reviewer.mode}
    if len(modes) != 1:
        raise DataError("MIXED_OFFLINE_AND_LIVE_PROVIDERS_FORBIDDEN")
    snapshot = deepcopy(bundle)
    rules = rules or Rules()
    initial = run(snapshot, as_of=as_of, rules=rules)
    discovery_skipped = False
    if snapshot.get("discovery_required") is True:
        from .discovery import validate_search_result
        discovery = initial.get("discovery", {})
        result = discovery.get("search")
        approved = (validate_search_result(result, initial) if discovery.get("mode") == "AGENT_SEARCH"
            and isinstance(result, dict) else [])
        approved_symbols = [s["symbol"] for s in approved]
        if selected_symbols is not None and (not isinstance(selected_symbols, list)
                or any(not isinstance(s, str) or s not in approved_symbols for s in selected_symbols)):
            raise DataError("DESK_SELECTION_NOT_APPROVED_BY_SEARCH")
        selected_symbols = approved_symbols if selected_symbols is None else selected_symbols
        discovery_skipped = not selected_symbols
    if timestamp(initial["as_of"]) > datetime.now(timezone.utc):
        raise DataError("DESK_CUTOFF_IN_FUTURE")
    run_id = uuid4().hex
    outputs = {s["packet_id"]: s for s in initial["securities"] if "packet_id" in s}
    # Confirmed near-support candidates first; earlier stages form an observation queue.
    eligible = [p for p in initial["agent_packets"] if outputs[p["packet_id"]]["verdict"] != "REJECT"]
    if selected_symbols is not None:
        available = {p["core"]["symbol"]: p for p in eligible}
        if (not isinstance(selected_symbols, list) or any(not isinstance(s, str) for s in selected_symbols)
                or len(selected_symbols) != len(set(selected_symbols)) or not set(selected_symbols) <= set(available)):
            raise DataError("INVALID_DISCOVERY_SELECTION")
        eligible = [available[s] for s in selected_symbols]
    eligible.sort(key=lambda p: (not (p["core"]["technical"]["technical_candidate"] and not p["core"]["context_blockers"]),
        not p["core"]["technical"]["technical_candidate"],
        p["core"]["technical"].get("position") == "EXTENDED",
        -(p["core"]["technical"]["priority_score"] or 0), p["core"]["symbol"]))
    if selected_symbols is not None:
        eligible.sort(key=lambda p: selected_symbols.index(p["core"]["symbol"]))
    limit = min(config.max_packets, config.max_calls // 2)
    selected, deferred = eligible[:limit], eligible[limit:]
    reviews, events = [], []
    # Fresh requests in independent contexts. Neither role sees the other's answer.
    with ThreadPoolExecutor(max_workers=2) as executor:
        for packet in selected:
            futures = [executor.submit(_call, provider, packet, role, run_id)
                       for role, provider in (("researcher", researcher), ("reviewer", reviewer))]
            for future in futures:
                review, event = future.result()
                events.append(event)
                if review is not None:
                    reviews.append(review)
    final = run(snapshot, as_of=as_of, rules=rules, reviews=reviews)
    errors = sum(e["status"] == "ERROR" for e in events)
    final["desk"] = {"run_id": run_id, "protocol": TASK_PROTOCOL, "mode": researcher.mode,
        "status": "SKIPPED_DISCOVERY" if discovery_skipped else "DEGRADED" if errors else "PARTIAL_BUDGET" if deferred else "COMPLETE",
        "config": asdict(config), "call_count": len(events), "error_count": errors,
        "selected_symbols": [p["core"]["symbol"] for p in selected],
        "deferred_symbols": [p["core"]["symbol"] for p in deferred], "events": events,
        "identity_binding": "RUNTIME_ASSIGNED_SEPARATE_CONTEXTS_NOT_INDEPENDENT_MODELS",
        "capabilities": ["READ_SNAPSHOT", "RESEARCH", "REVIEW", "REPORT"],
        "execution_enabled": False}
    final["reviews"] = reviews
    final["limitations"] = [s for s in final["limitations"] if s != "MANUAL_REVIEW_IDENTITIES_ARE_SELF_REPORTED"]
    final["limitations"].append("MODEL_ERRORS_MAY_BE_CORRELATED_DESPITE_SEPARATE_CONTEXTS")
    return final
