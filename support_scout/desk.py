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


def _call(provider, packet: dict, role: str, run_id: str, shared_public_information=None) -> tuple[dict | None, dict]:
    agent_id = f"{run_id}:{role}:{uuid4().hex}"
    audit = {"packet_id": packet["packet_id"], "symbol": packet["core"]["symbol"],
        "role": role, "agent_id": agent_id, "model": provider.model,
        "started_at": datetime.now(timezone.utc).isoformat()}
    if packet["core"].get("public_information_policy", {}).get("enabled"):
        audit.update(web_tool_call_count=None, web_tool_attempt_count=None,
            web_usage_status="UNKNOWN_UNTIL_RESPONSE_VALIDATED")
    try:
        isolated = deepcopy(packet)
        verify_packet(isolated)
        completion = (provider.complete(isolated, role, shared_public_information=shared_public_information)
            if shared_public_information is not None else provider.complete(isolated, role))
        verify_packet(isolated)
        if not isinstance(completion, Completion) or not isinstance(completion.answer, dict) or set(completion.answer) != ANSWER_FIELDS:
            raise ProviderError("INVALID_PROVIDER_COMPLETION")
        # A locally rejected answer still consumed API usage; preserve that fact.
        audit.update(response_id=completion.response_id, usage=completion.usage)
        review = {**deepcopy(completion.answer), "packet_id": packet["packet_id"], "role": role,
            "agent_id": agent_id, "produced_at": datetime.now(timezone.utc).isoformat()}
        if completion.public_information is not None:
            review["public_information"] = deepcopy(completion.public_information)
        if completion.position_view is not None:
            review["position_view"] = deepcopy(completion.position_view)
        if completion.opportunity_view is not None:
            review["opportunity_view"] = deepcopy(completion.opportunity_view)
        accepted, errors = validate_reviews(packet, [review])
        if errors or role not in accepted:
            raise ProviderError("REVIEW_CONTRACT_REJECTED:" + ",".join(errors))
        if provider.mode == "offline_simulation" and review["verdict"] != "WATCH":
            raise ProviderError("OFFLINE_PROMOTION_REFUSED")
        audit.update(status="ACCEPTED", response_id=completion.response_id, usage=completion.usage,
                     review_digest=digest(review))
        if completion.wire_metrics is not None:
            audit["wire_metrics"] = deepcopy(completion.wire_metrics)
        if completion.public_information is not None:
            info = completion.public_information
            audit.update(web_tool_call_count=0 if shared_public_information else info["completed_tool_call_count"],
                web_tool_attempt_count=0 if shared_public_information else info["tool_attempt_count"], public_information_digest=info["digest"],
                public_source_mode="SHARED_FACTS_NO_PEER_OPINION" if shared_public_information else "OWN_RETRIEVAL",
                public_finding_count=len(info["findings"]), public_gaps=info["gaps"], web_usage_status="RECORDED")
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
    if snapshot.get("market") == "JP":
        from .patterns import load_registry
        snapshot["pattern_registry"] = load_registry(snapshot.get("pattern_registry"))
    rules = rules or Rules()
    initial = run(snapshot, as_of=as_of, rules=rules)
    held_symbols = set(snapshot.get("portfolio_snapshot", {}).get("positions", {}))
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
        selected_symbols = list(dict.fromkeys([*(approved_symbols if selected_symbols is None else selected_symbols),
            *(s for s in held_symbols if any(p["core"]["symbol"] == s for p in initial["agent_packets"]))]))
        discovery_skipped = not selected_symbols
    if timestamp(initial["as_of"]) > datetime.now(timezone.utc):
        raise DataError("DESK_CUTOFF_IN_FUTURE")
    run_id = uuid4().hex
    outputs = {s["packet_id"]: s for s in initial["securities"] if "packet_id" in s}
    # Confirmed near-support candidates first; earlier stages form an observation queue.
    from .opportunity import active_candidate
    eligible = [p for p in initial["agent_packets"] if outputs[p["packet_id"]]["verdict"] != "REJECT"
        or p["core"]["symbol"] in held_symbols or active_candidate(p["core"])]
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
    eligible.sort(key=lambda p: p["core"]["symbol"] not in held_symbols)
    limit = min(config.max_packets, config.max_calls // 2)
    selected, deferred = eligible[:limit], eligible[limit:]
    reviews, events = [], []
    # Fresh requests in independent contexts. Neither role sees the other's answer.
    with ThreadPoolExecutor(max_workers=2) as executor:
        for packet in selected:
            shared_once = (packet["core"].get("communication_policy", {}).get("public_source_mode") == "shared_once"
                and packet["core"].get("public_information_policy", {}).get("enabled")
                and type(researcher).__name__ == "OpenAIProvider" and type(reviewer).__name__ == "OpenAIProvider")
            if shared_once:
                first, event = _call(researcher, packet, "researcher", run_id)
                events.append(event)
                if first is not None:
                    reviews.append(first)
                if first is None or not first.get("public_information"):
                    events.append({"packet_id": packet["packet_id"], "symbol": packet["core"]["symbol"],
                        "role": "reviewer", "model": reviewer.model, "status": "SKIPPED", "call_attempted": False,
                        "error": "SHARED_PUBLIC_SOURCES_UNAVAILABLE_NO_SECOND_SEARCH"})
                    continue
                second, event = _call(reviewer, packet, "reviewer", run_id,
                    first.get("public_information") if first else None)
                events.append(event)
                if second is not None:
                    reviews.append(second)
                continue
            futures = [executor.submit(_call, provider, packet, role, run_id)
                       for role, provider in (("researcher", researcher), ("reviewer", reviewer))]
            for future in futures:
                review, event = future.result()
                events.append(event)
                if review is not None:
                    reviews.append(review)
    final = run(snapshot, as_of=as_of, rules=rules, reviews=reviews)
    errors = sum(e["status"] in {"ERROR", "SKIPPED"} for e in events)
    final["desk"] = {"run_id": run_id, "protocol": TASK_PROTOCOL, "mode": researcher.mode,
        "status": "SKIPPED_DISCOVERY" if discovery_skipped else "DEGRADED" if errors else "PARTIAL_BUDGET" if deferred else "COMPLETE",
        "config": asdict(config), "call_count": sum(e.get("call_attempted", True) for e in events), "error_count": errors,
        "selected_symbols": [p["core"]["symbol"] for p in selected],
        "web_tool_call_count": sum(e.get("web_tool_call_count") or 0 for e in events),
        "web_tool_attempt_count": sum(e.get("web_tool_attempt_count") or 0 for e in events),
        "web_usage_complete": all(e.get("web_usage_status") != "UNKNOWN_UNTIL_RESPONSE_VALIDATED" for e in events),
        "deferred_symbols": [p["core"]["symbol"] for p in deferred], "events": events,
        "identity_binding": "RUNTIME_ASSIGNED_SEPARATE_CONTEXTS_NOT_INDEPENDENT_MODELS",
        "capabilities": ["READ_SNAPSHOT", "RESEARCH", "REVIEW", "REPORT"],
        "execution_enabled": False}
    final["reviews"] = reviews
    final["limitations"] = [s for s in final["limitations"] if s != "MANUAL_REVIEW_IDENTITIES_ARE_SELF_REPORTED"]
    final["limitations"].append("MODEL_ERRORS_MAY_BE_CORRELATED_DESPITE_SEPARATE_CONTEXTS")
    return final
