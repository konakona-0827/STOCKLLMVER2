"""Versioned opportunity hypotheses, separate from trading permission and probability."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import timedelta
import math
from pathlib import Path
import re
from statistics import fmean

from .contracts import DataError, TOKYO, digest, strict_json_loads, timestamp

DEFAULT_REGISTRY = Path(__file__).resolve().parent.parent / "config" / "patterns.json"
STAGE_LABELS = {"NO_MATCH": "尚無此型態", "DECLINE": "下跌中，尚未穩定",
    "EARLY_WATCH": "初期：低點穩定", "SUPPORT_STRENGTHENED": "強化：支撐與縮量證據",
    "CATALYST_AWAITING_PRICE": "催化已知，等待公布後價格確認",
    "CATALYST_CONFIRMED": "催化：超預期與放量突破", "INVALIDATED": "固定支撐失效",
    "EXTENDED": "價格已延伸，避免追高", "UNDERCUT_REVIEW": "低點下探，重新核對",
    "CONTRADICTED": "基本面下跌反證", "EXPIRED": "觀察情境過期"}
RESEARCH_STAGES = {"EARLY_WATCH", "SUPPORT_STRENGTHENED", "CATALYST_AWAITING_PRICE", "CATALYST_CONFIRMED"}


@dataclass(frozen=True)
class H001Parameters:
    decline_lookback_sessions: int = 20
    min_decline_fraction: float = 0.08
    stabilization_min_sessions: int = 3
    stabilization_window_sessions: int = 5
    max_stabilization_range_fraction: float = 0.08
    historical_lookback_sessions: int = 60
    min_support_tests: int = 2
    test_separation_sessions: int = 2
    support_band_fraction: float = 0.01
    volume_reference_sessions: int = 10
    volume_contraction_ratio: float = 0.8
    max_distance_fraction: float = 0.05
    breakout_lookback_sessions: int = 5
    breakout_volume_multiple: float = 1.3
    catalyst_max_age_days: int = 7
    min_surprise_fraction: float = 0.05
    max_episode_sessions: int = 30

    def __post_init__(self):
        for key, value in asdict(self).items():
            if key.endswith("sessions") or key.endswith("days") or key == "min_support_tests":
                if type(value) is not int or not 1 <= value <= 250:
                    raise DataError("INVALID_PATTERN_PARAMETER:" + key)
            elif type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
                raise DataError("INVALID_PATTERN_PARAMETER:" + key)
        if (self.stabilization_min_sessions > self.stabilization_window_sessions or
                any(getattr(self, k) >= 1 for k in ("min_decline_fraction", "support_band_fraction",
                    "max_distance_fraction", "max_stabilization_range_fraction", "min_surprise_fraction")) or
                self.volume_contraction_ratio > 1 or self.breakout_volume_multiple < 1):
            raise DataError("INCONSISTENT_PATTERN_PARAMETERS")


def load_registry(value=None):
    value = strict_json_loads(DEFAULT_REGISTRY.read_text(encoding="utf-8")) if value is None else deepcopy(value)
    if not isinstance(value, dict) or set(value) != {"schema_version", "definitions"} or value["schema_version"] != 1:
        raise DataError("INVALID_PATTERN_REGISTRY")
    definitions = value["definitions"]
    if not isinstance(definitions, list) or len(definitions) > 8:
        raise DataError("INVALID_PATTERN_DEFINITIONS")
    seen = set()
    fields = {"id", "version", "handler", "enabled", "name", "hypotheses",
              "evaluation_horizons_sessions", "agent_guidance", "parameters"}
    for definition in definitions:
        if not isinstance(definition, dict) or set(definition) != fields:
            raise DataError("INVALID_PATTERN_DEFINITION_FIELDS")
        identifier = definition["id"]
        if not isinstance(identifier, str) or not re.fullmatch(r"H-[0-9]{3}", identifier) or identifier in seen:
            raise DataError("DUPLICATE_OR_INVALID_PATTERN_ID")
        seen.add(identifier)
        if (type(definition["enabled"]) is not bool or not isinstance(definition["handler"], str) or definition["handler"] not in HANDLERS or
                any(not isinstance(definition[k], str) or not definition[k].strip() for k in ("version", "name", "agent_guidance"))):
            raise DataError("INVALID_PATTERN_DEFINITION")
        for key in ("hypotheses", "evaluation_horizons_sessions"):
            if not isinstance(definition[key], list) or not definition[key]:
                raise DataError("INVALID_PATTERN_RESEARCH_CONTRACT")
        if any(not isinstance(x, str) or not x.strip() for x in definition["hypotheses"]):
            raise DataError("INVALID_PATTERN_HYPOTHESES")
        if any(type(x) is not int or not 1 <= x <= 250 for x in definition["evaluation_horizons_sessions"]):
            raise DataError("INVALID_PATTERN_HORIZONS")
        try:
            definition["parameters"] = asdict(H001Parameters(**definition["parameters"]))
        except TypeError:
            raise DataError("INVALID_PATTERN_PARAMETER_FIELDS") from None
    return value


def _known(value, cutoff):
    try:
        return (isinstance(value, dict) and isinstance(value.get("source"), str) and bool(value["source"].strip())
                and timestamp(value.get("available_at")) <= cutoff)
    except DataError:
        return False


def _catalysts(context, cutoff, params, symbol):
    evidence, accepted, reasons = [], [], []
    events = context.get("catalysts", [])
    if not isinstance(events, list):
        return [], [], ["CATALYST_INPUT_INVALID"]
    def visible(event):
        try:
            return _known(event, cutoff) and timestamp(event.get("announced_at")) <= cutoff
        except DataError:
            return False
    visible_ids = [e.get("id") for e in events if visible(e) and isinstance(e.get("id"), str)]
    duplicates = {identifier for identifier in visible_ids if visible_ids.count(identifier) > 1}
    seen = set()
    for event in events:
        # Future or not-yet-available event bodies never enter a model packet.
        if not _known(event, cutoff):
            continue
        try:
            announced = timestamp(event.get("announced_at"))
            available = timestamp(event["available_at"])
            if announced > cutoff:
                continue
            if announced > available or cutoff - announced > timedelta(days=params.catalyst_max_age_days):
                reasons.append("CATALYST_TIMING_INVALID_OR_STALE")
                continue
            identifier = event.get("id")
            if not isinstance(identifier, str) or not identifier.strip() or identifier in seen or identifier in duplicates:
                raise DataError("CATALYST_ID_INVALID_OR_DUPLICATE")
            seen.add(identifier)
            comparison = event.get("comparison")
            positive = False
            comparison_status = "NO_PRE_EVENT_EXPECTATION"
            surprise = None
            if isinstance(comparison, dict):
                actual, expected = comparison.get("actual"), comparison.get("consensus")
                numeric = all(type(v) in (float, int) and math.isfinite(v) for v in (actual, expected))
                expectation_time = timestamp(comparison.get("expectation_available_at"))
                same_units = (isinstance(comparison.get("actual_unit"), str) and bool(comparison["actual_unit"])
                              and comparison["actual_unit"] == comparison.get("consensus_unit"))
                comparable = (numeric and expected != 0 and same_units and expectation_time < announced
                    and comparison.get("direction") == "HIGHER_IS_BETTER"
                    and all(isinstance(comparison.get(k), str) and comparison[k].strip()
                            for k in ("metric", "actual_source", "consensus_source")))
                comparison_status = "COMPARABLE" if comparable else "INVALID_EXPECTATION_COMPARISON"
                if comparable:
                    surprise = (actual - expected) / abs(expected)
                    positive = (event.get("kind") in {"EARNINGS", "GUIDANCE", "SECTOR_SURPRISE"}
                                and event.get("verified") is True and surprise >= params.min_surprise_fraction)
            ev = {"id": f"{symbol}:H001_CATALYST:{identifier}", "kind": "H001_CATALYST",
                "available_at": event["available_at"], "values": {"event_id": identifier,
                    "kind": event.get("kind"), "announced_at": event["announced_at"],
                    "source": event["source"], "verified_input_claim": event.get("verified") is True,
                    "comparison_status": comparison_status, "surprise_fraction": surprise,
                    "positive_surprise": positive, "comparison": deepcopy(comparison)}}
            evidence.append(ev)
            if positive:
                accepted.append(ev["id"])
        except (DataError, TypeError, ValueError):
            reasons.append("CATALYST_INPUT_INVALID")
    if not accepted:
        reasons.append("NO_VERIFIED_POSITIVE_SURPRISE")
    return evidence, accepted, reasons


def detect_h001(series, cutoff, definition, context):
    params = H001Parameters(**definition["parameters"])
    bars = series.bars
    active, episodes = None, []
    for i, bar in enumerate(bars):
        if i < params.decline_lookback_sessions:
            continue
        if active and active["terminal"]:
            # A later setup has a new anchor/identity; the old episode stays terminal.
            active = None
        if active and i - active["anchor_index"] > params.max_episode_sessions:
            active.update(terminal=True, state="EXPIRED")
            continue
        high = max(b.high for b in bars[i - params.decline_lookback_sessions:i])
        drop = 1 - bar.low / high
        recent_low = min(b.low for b in bars[max(0, i - params.stabilization_window_sessions):i + 1])
        if active is None and drop >= params.min_decline_fraction and bar.low <= recent_low:
            active = {"anchor_index": i, "anchor_session": bar.session, "anchor": bar.low,
                "decline_fraction": drop, "stable_sessions": 0, "frozen_at": None,
                "lower": bar.low * (1 - params.support_band_fraction),
                "upper": bar.low * (1 + params.support_band_fraction), "terminal": False, "state": "DECLINE"}
            episodes.append(active)
            continue
        if active is None:
            continue
        if active["frozen_at"] and bar.close < active["lower"]:
            active.update(terminal=True, state="INVALIDATED", invalidated_at=bar.session)
            continue
        if bar.low < active["anchor"]:
            active["stable_sessions"] = 0
            if not active["frozen_at"]:
                active.update(anchor_index=i, anchor_session=bar.session, anchor=bar.low,
                    lower=bar.low * (1 - params.support_band_fraction),
                    upper=bar.low * (1 + params.support_band_fraction), decline_fraction=drop)
            else:
                active["state"] = "UNDERCUT_REVIEW"
        else:
            active["stable_sessions"] += 1
        if active["stable_sessions"] >= params.stabilization_min_sessions and not active["frozen_at"]:
            active["frozen_at"] = bar.session
            active["state"] = "EARLY_WATCH"
    result = {"pattern_id": definition["id"], "version": definition["version"],
        "definition_digest": digest(definition), "definition": deepcopy(definition),
        "stage": "NO_MATCH", "stage_label": STAGE_LABELS["NO_MATCH"], "research_candidate": False,
        "support": None, "metrics": {}, "evidence": [], "required_evidence_ids": [],
        "reason_codes": [], "empirical_status": "UNVALIDATED_HYPOTHESIS_NOT_WIN_PROBABILITY",
        "execution_enabled": False}
    if active is None and episodes:
        active = episodes[-1]
    if active is None:
        return result
    latest = bars[-1]
    anchor_i = active["anchor_index"]
    recent = bars[-params.stabilization_window_sessions:]
    span = (max(b.high for b in recent) - min(b.low for b in recent)) / active["anchor"]
    tests, last_test = [], -params.test_separation_sessions
    for i in range(max(0, anchor_i - params.historical_lookback_sessions), len(bars)):
        b = bars[i]
        if active["lower"] <= b.low <= active["upper"] and b.close >= active["lower"] and i - last_test >= params.test_separation_sessions:
            tests.append(b.session)
            last_test = i
    reference = bars[max(0, anchor_i - params.volume_reference_sessions):anchor_i]
    base_volume = fmean(b.volume for b in reference) if reference else None
    test_bars = [b for b in bars[anchor_i:] if active["lower"] <= b.low <= active["upper"]]
    test_volume = fmean(b.volume for b in test_bars[-params.stabilization_window_sessions:]) if test_bars else None
    contraction = test_volume / base_volume if test_volume is not None and base_volume else None
    comparable = series.metadata.get("volume_comparable") is True
    dry = comparable and contraction is not None and contraction <= params.volume_contraction_ratio
    stable = active["stable_sessions"] >= params.stabilization_min_sessions and span <= params.max_stabilization_range_fraction
    strengthened = stable and len(tests) >= params.min_support_tests and dry
    prior = bars[-params.breakout_lookback_sessions - 1:-1]
    volume_multiple = latest.volume / fmean(b.volume for b in prior) if prior else None
    breakout = bool(len(prior) == params.breakout_lookback_sessions and comparable and
        latest.close > max(b.high for b in prior) and volume_multiple >= params.breakout_volume_multiple)
    distance = latest.close / active["anchor"] - 1
    catalyst_evidence, positive_ids, reasons = _catalysts(context, cutoff, params, series.symbol)
    driver = context.get("decline_driver", {})
    classification = driver.get("classification", "UNKNOWN") if _known(driver, cutoff) and driver.get("verified") is True else "UNKNOWN"
    if classification not in {"SENTIMENT_OR_LIQUIDITY", "FUNDAMENTAL_DETERIORATION", "UNKNOWN"}:
        classification = "UNKNOWN"
    eligible_event_times = [max(timestamp(e["available_at"]), timestamp(e["values"]["announced_at"]))
                           for e in catalyst_evidence if e["id"] in positive_ids]
    # Require a new daily session beginning after the announcement/availability.
    # Same-session OHLC cannot identify which prices occurred after an intraday event.
    post_event_price = bool(eligible_event_times and any(
        latest.session > when.astimezone(TOKYO).date().isoformat() for when in eligible_event_times))
    state = active["state"]
    if not active["terminal"]:
        state = ("CATALYST_CONFIRMED" if strengthened and positive_ids and breakout and post_event_price
            else "CATALYST_AWAITING_PRICE" if strengthened and positive_ids
            else "SUPPORT_STRENGTHENED" if strengthened else "EARLY_WATCH" if stable
            else "UNDERCUT_REVIEW" if active["frozen_at"] else "DECLINE")
        if distance > params.max_distance_fraction:
            state = "EXTENDED"
        if classification == "FUNDAMENTAL_DETERIORATION":
            state = "CONTRADICTED"
    if classification == "UNKNOWN":
        reasons.append("DECLINE_DRIVER_UNVERIFIED")
    if not comparable:
        reasons.append("PATTERN_VOLUME_BASIS_UNVERIFIED")
    if not stable:
        reasons.append("STABILITY_NOT_CONFIRMED")
    if len(tests) < params.min_support_tests:
        reasons.append("INSUFFICIENT_SUPPORT_TESTS")
    if not dry:
        reasons.append("VOLUME_CONTRACTION_NOT_CONFIRMED")
    if not breakout:
        reasons.append("VOLUME_BREAKOUT_NOT_CONFIRMED")
    if positive_ids and not post_event_price:
        reasons.append("POST_EVENT_PRICE_CONFIRMATION_REQUIRED")
    support = {k: active[k] for k in ("anchor_session", "anchor", "lower", "upper", "frozen_at")}
    metrics = {"decline_fraction": active["decline_fraction"], "no_new_low_sessions": active["stable_sessions"],
        "stabilization_range_fraction": span, "support_test_sessions": tests, "support_test_count": len(tests),
        "volume_contraction_ratio": contraction, "volume_basis_comparable": comparable,
        "breakout": breakout, "breakout_volume_multiple": volume_multiple,
        "distance_from_support_fraction": distance, "positive_catalyst_evidence_ids": positive_ids,
        "post_event_price_available": post_event_price,
        "decline_driver": classification}
    ev = {"id": f"{series.symbol}:H001_METRICS:{latest.session}", "kind": "H001_METRICS",
        "available_at": latest.available_at, "values": {"stage": state, "support": support, **metrics}}
    evidence = [ev, *catalyst_evidence]
    if classification != "UNKNOWN":
        evidence.append({"id": f"{series.symbol}:H001_DECLINE_DRIVER", "kind": "H001_DECLINE_DRIVER",
            "available_at": driver["available_at"], "values": deepcopy(driver)})
    result.update(stage=state, stage_label=STAGE_LABELS[state], research_candidate=state in RESEARCH_STAGES,
        support=support, metrics=metrics, reason_codes=sorted(set(reasons)), evidence=evidence,
        required_evidence_ids=[e["id"] for e in evidence],
        episodes=[{k: deepcopy(v) for k, v in episode.items() if k != "anchor_index"} for episode in episodes],
        limitations=["DAILY_OHLC_DOES_NOT_PROVE_INTRADAY_ORDER", "SUPPORT_TESTS_NOT_INDEPENDENT_SAMPLES",
            "INPUT_SOURCE_CLAIMS_REQUIRE_EXTERNAL_VERIFICATION", "PARAMETERS_NOT_CALIBRATED_ON_ADVANTEST"])
    return result


HANDLERS = {"support_catalyst_reversal": detect_h001}


def evaluate_patterns(series, cutoff, registry, context=None):
    if context is not None and not isinstance(context, dict):
        raise DataError("INVALID_PATTERN_CONTEXT")
    return [HANDLERS[d["handler"]](series, cutoff, d, context or {})
            for d in registry["definitions"] if d["enabled"]]
