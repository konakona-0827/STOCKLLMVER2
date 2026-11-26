"""Causal ranking and matched, conservative per-opportunity comparisons."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import statistics

from research.multi_horizon.engine import label_path
from . import VERSION
from .data import digest, load_dataset, new_directory, read_json, write_csv, write_json

DEFAULT_PROTOCOL = Path(__file__).with_name("protocol.json")


def validate_protocol(protocol):
    for key, low, high in (("top_k", 1, 50), ("warmup_sessions", 61, 500), ("horizon_sessions_after_entry", 1, 60)):
        if type(protocol.get(key)) is not int or not low <= protocol[key] <= high:
            raise ValueError("INVALID_PROTOCOL: " + key)
    if date.fromisoformat(protocol["train_through"]) >= date.fromisoformat(protocol["test_from"]):
        raise ValueError("TRAIN_TEST_PERIODS_MUST_NOT_OVERLAP")
    return protocol


def mean(values):
    return statistics.fmean(values)


def contiguous(bars):
    return all(b["session_index"] == a["session_index"] + 1 for a, b in zip(bars, bars[1:]))


def features(bars, index, protocol):
    size = protocol["warmup_sessions"]
    if size < 61:
        raise ValueError("WARMUP_REQUIRES_AT_LEAST_61_SESSIONS")
    window = bars[max(0, index - size + 1):index + 1]
    if len(window) < size or not contiguous(window):
        return None, "INSUFFICIENT_OR_GAPPED_WARMUP"
    if any(b["volume"] == 0 for b in window):
        return None, "ZERO_VOLUME_IN_WARMUP"
    if any(b["turnover_twd"] is None for b in window[-20:]):
        return None, "ACTUAL_TURNOVER_MISSING"
    if any(abs(b["close"] / a["close"] - 1) > .18 for a, b in zip(window, window[1:])):
        return None, "POSSIBLE_CORPORATE_ACTION_OR_PRICE_DISCONTINUITY"
    last, previous = window[-1], window[-2]
    closes = [b["close"] for b in window]
    amount = mean([b["turnover_twd"] for b in window[-20:]])
    if amount < protocol["minimum_average_turnover_twd"]:
        return None, "AVERAGE_TURNOVER_BELOW_RESEARCH_THRESHOLD"
    atr = mean([max(b["high"] - b["low"], abs(b["high"] - a["close"]), abs(b["low"] - a["close"]))
                for a, b in zip(window[-15:-1], window[-14:])])
    result = dict(symbol=last["symbol"], exchange=last["exchange"], signal_date=last["date"],
                  close=last["close"], return_5d=closes[-1] / closes[-6] - 1,
                  return_20d=closes[-1] / closes[-21] - 1,
                  distance_ma20=closes[-1] / mean(closes[-20:]) - 1,
                  distance_ma60=closes[-1] / mean(closes[-60:]) - 1,
                  distance_high20=closes[-1] / max(b["high"] for b in window[-20:]) - 1,
                  volume_ratio_5_20=mean([b["volume"] for b in window[-5:]]) / mean([b["volume"] for b in window[-20:]]),
                  atr14_pct=atr / closes[-1], turnover_20d_twd=amount,
                  recovery=last["close"] > last["open"] and last["close"] > previous["high"],
                  price_basis=last["price_basis"], available_at=last["retrieved_at"])
    result["evidence_id"] = "ev-" + digest(json.dumps(result, sort_keys=True).encode())[:16]
    return result, None


def percentile(rows, field, reverse=False):
    # Average ranks for ties; a tied pool must not acquire symbol-order bias.
    values = sorted(r[field] for r in rows)
    result = {}
    for row in rows:
        value = row[field]
        rank = (sum(x < value for x in values) + .5 * sum(x == value for x in values)) / len(values)
        result[row["symbol"]] = 1 - rank if reverse else rank
    return result


def rank(rows, mode):
    rows = [dict(row) for row in rows]
    if not rows:
        return rows
    if mode == "momentum":
        scores = percentile(rows, "return_20d")
    elif mode == "composite":
        momentum = percentile(rows, "return_20d")
        volume = percentile(rows, "volume_ratio_5_20")
        near_high = percentile(rows, "distance_high20")
        low_vol = percentile(rows, "atr14_pct", reverse=True)
        scores = {r["symbol"]: .35 * momentum[r["symbol"]] + .20 * volume[r["symbol"]]
                  + .20 * int(r["recovery"]) + .15 * near_high[r["symbol"]] + .10 * low_vol[r["symbol"]] for r in rows}
    else:
        raise ValueError("UNKNOWN_RANKING_MODE")
    for row in rows:
        row.update(rank_score=round(scores[row["symbol"]], 8), ranking=mode)
    return sorted(rows, key=lambda r: (-r["rank_score"], r["symbol"]))


def snapshot(dataset, as_of, protocol=None, scores_folder=None):
    protocol = validate_protocol(protocol or read_json(DEFAULT_PROTOCOL))
    grouped, sessions, manifest = load_dataset(dataset)
    if as_of not in sessions:
        raise ValueError("AS_OF_MUST_BE_IN_DATASET_CALENDAR")
    candidates, excluded = [], []
    for symbol, bars in grouped.items():
        indices = [i for i, b in enumerate(bars) if b["date"] == as_of]
        if not indices:
            excluded.append(dict(symbol=symbol, reason="LATEST_SESSION_MISSING"))
            continue
        row, reason = features(bars, indices[0], protocol)
        if row:
            row["recent_bars"] = [{k: b[k] for k in ("date", "open", "high", "low", "close", "volume")}
                                  for b in bars[max(0, indices[0] - 4):indices[0] + 1]]
            candidates.append(row)
        else:
            excluded.append(dict(symbol=symbol, reason=reason))
    core = dict(version=VERSION, signal_date=as_of, decision_scope="EOD_RESEARCH_ONLY_NO_EXECUTABLE_TRADE_PLAN",
                dataset_manifest_sha256=digest((Path(dataset) / "manifest.json").read_bytes()),
                protocol_sha256=digest(json.dumps(protocol, sort_keys=True).encode()),
                point_in_time_certified=False,
                warnings=["RAW_PRICES_ACTIONS_UNVERIFIED", "CURRENT_FIXED_UNIVERSE", "NO_LIVE_ODDLOT_QUOTES",
                          "NO_VERIFIED_COMPANY_EVENTS", "NO_CALIBRATED_PROBABILITIES", "NOT_A_BUY_SIGNAL"],
                rankings={m: rank(candidates, m)[:protocol["top_k"]] for m in ("momentum", "composite")},
                eligible_candidates=candidates, excluded=excluded)
    if scores_folder is not None:
        import csv
        folder = Path(scores_folder)
        metadata = read_json(folder / "scores_manifest.json")
        if (metadata["dataset_manifest_sha256"] != core["dataset_manifest_sha256"] or
                metadata["protocol_sha256"] != core["protocol_sha256"] or
                digest((folder / "scores.csv").read_bytes()) != metadata["scores_sha256"] or
                metadata["train_labels_mature_through"] >= as_of):
            raise ValueError("MODEL_SCORES_PROVENANCE_OR_TIME_MISMATCH")
        with (folder / "scores.csv").open(encoding="utf-8", newline="") as stream:
            scores = {r["symbol"]: float(r["score"]) for r in csv.DictReader(stream) if r["date"] == as_of}
        import math
        if set(scores) != {r["symbol"] for r in candidates} or any(not math.isfinite(v) for v in scores.values()):
            raise ValueError("MODEL_SCORE_COVERAGE_OR_NUMERIC_ERROR")
        ranked = [dict(r, rank_score=scores[r["symbol"]], ranking="qlib_lightgbm_diagnostic") for r in candidates]
        ranked.sort(key=lambda r: (-r["rank_score"], r["symbol"]))
        core["rankings"]["qlib_lightgbm_diagnostic"] = ranked[:protocol["top_k"]]
        core["model"] = metadata
    core["packet_id"] = digest(json.dumps(core, sort_keys=True, ensure_ascii=False).encode())
    return core


def scenario_protocol(protocol):
    return dict(notional_per_opportunity_twd=protocol["notional_per_opportunity_twd"],
                max_oddlot_quantity=999, costs=protocol["costs"])


def build_observations(grouped, sessions, protocol):
    """Features at t; labels are held separately and never sent to a reviewer."""
    observations, exclusions = [], {}
    horizon = protocol["horizon_sessions_after_entry"]
    barrier = dict(take_profit_rate=protocol["take_profit_rate"], stop_loss_rate=protocol["stop_loss_rate"])
    for symbol, bars in grouped.items():
        decimal_bars = [dict(b, date=date.fromisoformat(b["date"]),
                             **{k: Decimal(str(b[k])) for k in ("open", "high", "low", "close")}) for b in bars]
        for index in range(len(bars)):
            row, reason = features(bars, index, protocol)
            if not row:
                exclusions[reason] = exclusions.get(reason, 0) + 1
                continue
            path = bars[index:index + horizon + 2]
            if len(path) < horizon + 2:
                reason = "IMMATURE_FULL_WINDOW"
            elif not contiguous(path):
                reason = "FUTURE_SESSION_GAP"
            elif any(abs(b["close"] / a["close"] - 1) > .18 for a, b in zip(path, path[1:])):
                reason = "FUTURE_PRICE_DISCONTINUITY_UNSCORABLE"
            else:
                reason = None
            if reason:
                exclusions[reason] = exclusions.get(reason, 0) + 1
                # Eligibility and ranking are frozen WITHOUT consulting future availability.
                observations.append(dict(features=row, label=None, unscored_reason=reason))
                continue
            label = label_path(decimal_bars, index, horizon, barrier, scenario_protocol(protocol))
            observations.append(dict(features=row, label=label,
                                     label_ready_at=path[-1]["date"]))
    return observations, exclusions


def summarize(observations, selections):
    # Unscoreable selected events remain visible; never replace them with the next rank.
    selected = [r for r in observations if (r["features"]["symbol"], r["features"]["signal_date"]) in selections]
    matured = [r for r in observations if r.get("label") and r["label"]["status"] == "COMPLETE"]
    scored = [r for r in selected if r.get("label") and r["label"]["status"] == "COMPLETE"]
    positives = [r for r in matured if Decimal(r["label"]["net_pnl_twd"]) > 0]
    picked_positive = [r for r in scored if Decimal(r["label"]["net_pnl_twd"]) > 0]
    # Summing overlapping hypothetical opportunities is not an account equity curve.
    pnl = sum((Decimal(r["label"]["net_pnl_twd"]) for r in scored), Decimal(0))
    denominator = sum((Decimal(r["label"]["buy_cost_twd"]) * r["label"]["holding_trading_days"] for r in scored), Decimal(0))
    return dict(selected=len(selected), scored=len(scored), unscored=len(selected) - len(scored),
                positive_picks=len(picked_positive), nonpositive_picks=len(scored) - len(picked_positive),
                available_positive_opportunities=len(positives), missed_positive_opportunities=len(positives) - len(picked_positive),
                positive_pick_fraction=len(picked_positive) / len(scored) if scored else None,
                positive_capture_fraction=len(picked_positive) / len(positives) if positives else None,
                mean_scenario_net_return=mean([float(r["label"]["net_return"]) for r in scored]) if scored else None,
                scenario_pnl_per_10000_capital_day=float(pnl / denominator * 10000) if denominator else None,
                ambiguous_daily_bars=sum(r["label"]["ambiguous_bar"] for r in scored),
                status_counts={status: sum(bool(r.get("label")) and r["label"]["status"] == status for r in selected)
                               for status in ("COMPLETE", "CENSORED", "NOT_ENTERED")})


def evaluate(dataset, output, protocol=None, external_scores=None):
    protocol = validate_protocol(protocol or read_json(DEFAULT_PROTOCOL))
    grouped, sessions, manifest = load_dataset(dataset)
    observations, exclusions = build_observations(grouped, sessions, protocol)
    by_day = {}
    for row in observations:
        by_day.setdefault(row["features"]["signal_date"], []).append(row["features"])
    selections = {m: set() for m in ("momentum", "composite")}
    if external_scores is not None:
        selections["qlib_lightgbm_diagnostic"] = set()
    selection_rows = []
    for day, rows in sorted(by_day.items()):
        for mode in selections:
            if mode == "qlib_lightgbm_diagnostic":
                ranked = [dict(r, rank_score=external_scores[(r["symbol"], day)]) for r in rows
                          if (r["symbol"], day) in external_scores]
                ranked.sort(key=lambda r: (-r["rank_score"], r["symbol"]))
            else:
                ranked = rank(rows, mode)
            for order, row in enumerate(ranked[:protocol["top_k"]], 1):
                selections[mode].add((row["symbol"], day))
                selection_rows.append({**row, "ranking": mode, "rank": order})
    test_rows = [r for r in observations if r["features"]["signal_date"] >= protocol["test_from"]]
    if external_scores is not None:
        # Matched availability: a Qlib diagnostic comparison uses the same stock/date
        # opportunity universe for every ranking. Missing predictions are not zero scores.
        test_rows = [r for r in test_rows if (r["features"]["symbol"], r["features"]["signal_date"]) in external_scores]
        matched = {}
        for row in test_rows:
            matched.setdefault(row["features"]["signal_date"], []).append(row["features"])
        for mode in ("momentum", "composite"):
            selections[mode] = {(r["symbol"], day) for day, rows in matched.items()
                                for r in rank(rows, mode)[:protocol["top_k"]]}
    summary = dict(version=VERSION, scope=protocol["scope"], test_from=protocol["test_from"],
                   observations=len(observations), exclusions=exclusions,
                   selection_rule="FROZEN_BEFORE_FUTURE_LABEL_AVAILABILITY_NO_BACKFILL",
                   positive_definition="FULL_WINDOW_MATURED_SCENARIO_NET_PNL_GREATER_THAN_ZERO",
                   comparison={mode: summarize(test_rows, chosen) for mode, chosen in selections.items()},
                   superiority_proven=False, portfolio_performance=False,
                   point_in_time_certified=False, corporate_actions_verified=False,
                   notes=protocol["notes"])
    out = new_directory(output)
    write_json(out / "summary.json", summary)
    write_json(out / "protocol.json", protocol)
    write_json(out / "dataset_manifest.json", manifest)
    write_json(out / "observations.json", observations)
    write_csv(out / "selections.csv", sorted({k for r in selection_rows for k in r}), selection_rows)
    write_json(out / "run.json", dict(version=VERSION,
               dataset_manifest_sha256=digest((Path(dataset) / "manifest.json").read_bytes()),
               implementation_sha256={p.name: digest(p.read_bytes()) for p in Path(__file__).parent.glob("*.py")},
               protocol_sha256=digest((out / "protocol.json").read_bytes())))
    return summary
