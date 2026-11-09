"""Versioned research ordering, using only frozen pre-decision evidence."""
import math

from .contracts import DataError, timestamp

LEGACY = "legacy-v1"
SUPPORT_EVIDENCE = "support-evidence-v1-20261105"
VERSIONS = {LEGACY, SUPPORT_EVIDENCE}


def legacy_key(candidate):
    t = candidate["technical"]
    return (not (t["technical_candidate"] and not candidate["context_blockers"]),
        not t["technical_candidate"],
        -max(({"CATALYST_CONFIRMED": 4, "CATALYST_AWAITING_PRICE": 3, "SUPPORT_STRENGTHENED": 2, "EARLY_WATCH": 1,
            "SUPPORT_RECLAIMED": 3, "SUPPORT_HELD": 2, "SHORT_RECLAIM_READY": 4, "SHORT_RECLAIM_TRIGGER": 2,
            "SHORT_PULLBACK_READY": 3, "SHORT_MOMENTUM_READY": 3}.get(p["stage"], 0)
            for p in candidate.get("patterns", [])), default=0),
        -(t.get("priority_score") or 0), candidate["symbol"])


def support_evidence(candidate, cutoff):
    """Confirmation is local to its support model; raw volume is not verified.

    Reuse each frozen model's proximity limits. An expired ATR episode cannot
    confirm a new percentage support, or veto its independent research queue.
    """
    t = candidate["technical"]
    evidence = {e["id"]: e for e in candidate["evidence"] if timestamp(e["available_at"]) <= timestamp(cutoff)}
    signals = []
    support = t.get("support") or {}
    if t.get("technical_candidate") and t.get("state") == "RULE_CONFIRMED" and t.get("position") == "NEAR_SUPPORT":
        refs = [r for r in support.get("evidence_ids", []) if r in evidence and evidence[r]["kind"] == "CONFIRMATION"]
        if refs:
            signals.append({"model": "ATR", "grade": 3, "evidence_ids": refs,
                "meaning": "CURRENT_ATR_EPISODE_CONFIRMED_NEAR_SUPPORT"})
    for p in candidate.get("patterns", []):
        if p["pattern_id"] != "H-002" or not p.get("research_candidate") or p["stage"] not in {"SUPPORT_HELD", "SUPPORT_RECLAIMED"}:
            continue
        distance = p.get("metrics", {}).get("distance_from_support_fraction")
        limit = p.get("definition", {}).get("parameters", {}).get("max_distance_fraction")
        refs = p.get("required_evidence_ids", [])
        zone = p.get("support") or {}
        if (type(distance) not in (int, float) or not math.isfinite(distance) or type(limit) not in (int, float)
                or not 0 <= distance <= limit or not zone or not refs or not set(refs) <= evidence.keys()):
            continue
        signals.append({"model": "H-002", "grade": 2 if p["stage"] == "SUPPORT_RECLAIMED" else 1,
            "evidence_ids": list(refs), "distance_fraction": distance, "frozen_proximity_limit": limit,
            "meaning": "SUPPORT_RECLAIMED" if p["stage"] == "SUPPORT_RECLAIMED" else "SUPPORT_HELD_RAW_VOLUME_ONLY",
            "volume_verified": p.get("metrics", {}).get("volume_basis_comparable") is True})
    return {"grade": max((x["grade"] for x in signals), default=0), "signals": signals,
        "meaning": "RESEARCH_PRIORITY_ONLY_NOT_TRADE_QUALIFICATION"}


def rank_candidates(candidates, *, version=SUPPORT_EVIDENCE, cutoff):
    if version not in VERSIONS:
        raise DataError("UNKNOWN_SEARCH_RANKING_VERSION")
    if version == LEGACY:
        return sorted(candidates, key=legacy_key)
    def key(c):
        old = legacy_key(c)
        return (*old[:2], -support_evidence(c, cutoff)["grade"], *old[2:])
    return sorted(candidates, key=key)
