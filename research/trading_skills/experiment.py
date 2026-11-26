"""Matched daily experiments with real portfolio accounting and append-only runs."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import time

from research.selection_lab.data import load_dataset
from research.selection_lab.experiment import features, rank, DEFAULT_PROTOCOL
from . import VERSION
from .market import Costs, Market, decimal
from .simulation import (pool_blocks, simulate, setup_tags, exit_price, state,
                         calibrated_probability, calibration_metrics)

VARIANTS = ("baseline_selection_momentum", "skill_scanner", "iid_costs", "block_costs", "conditioned_block_costs")
BASELINE_FILES = ("quant_scanner.py", "analysis_service.py", "strategy_experiment.py", "paper_portfolio.py",
                  "strategy_catalog.py", "prompt_selection.md", "strategies/fixed_rules.md",
                  "llm.py", "agent_decision.py", "trading_agent.py", "position_analyzer.py", "pretrade_policy.py",
                  "config/scan_rules.json", "config/universe_tw.json", "research/selection_lab/experiment.py",
                  "research/selection_lab/protocol.json")


def digest(value):
    content = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(content).hexdigest()


def git_environment():
    # Incomplete inherited Git overrides must not redirect or break provenance.
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_") and k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")}


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def load_protocol(path=None):
    p = json.loads(Path(path or Path(__file__).with_name("protocol.json")).read_text(encoding="utf-8"))
    return validate_protocol(p)


def validate_protocol(p):
    if p["mode"] != "PAPER_ONLY":
        raise ValueError("PAPER_ONLY_REQUIRED")
    for k in ("top_k", "horizon", "history_sessions", "min_pool_blocks", "block_size", "paths", "max_positions", "calibration_minimum", "calibration_bins"):
        if type(p[k]) is not int or p[k] < 1:
            raise ValueError("INVALID_PROTOCOL_" + k)
    if p["block_size"] > p["history_sessions"] or p["paths"] > 100000 or p["top_k"] > 50 or p["horizon"] > 60:
        raise ValueError("UNBOUNDED_PROTOCOL")
    for k in ("initial_cash_twd", "notional_twd", "max_daily_buy_twd"):
        if decimal(p[k]) <= 0:
            raise ValueError("INVALID_PROTOCOL_" + k)
    for k in ("risk_fraction", "stop_fraction", "target_fraction", "max_participation"):
        if not 0 < decimal(p[k]) < 1:
            raise ValueError("INVALID_PROTOCOL_" + k)
    if not 0 < p["volatility_ratio_low"] <= p["volatility_ratio_high"] or p["minimum_turnover_twd"] < 0:
        raise ValueError("INVALID_CONDITIONING")
    Costs(p["costs"])
    return p


def freeze_baseline(root, output):
    root, output = Path(root).resolve(), Path(output)
    manifest = {}
    filenames = sorted(set(BASELINE_FILES) | {p.relative_to(root).as_posix() for p in (root / "strategies").glob("*.md")})
    for filename in filenames:
        source = root / filename
        if source.is_file():
            content = source.read_bytes()
            target = output / "baseline_source" / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(content)
            manifest[filename] = digest(content)
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, env=git_environment()).strip()
    save(output / "baseline_manifest.json", dict(commit=commit, files=manifest,
         comparison_baseline="existing selection_lab momentum ranking with shared portfolio execution",
         production_llm_replayed=False, production_strategy_unchanged=True))


def market_regimes(grouped, sessions):
    """Causal equal-weight research-universe regime; NOT the TAIEX index."""
    lookups = {s: {b["date"]: b for b in bars} for s, bars in grouped.items()}
    levels, result = [1.0], {}
    for prev, day in zip(sessions, sessions[1:]):
        changes = [m[day]["close"] / m[prev]["close"] - 1 for m in lookups.values() if day in m and prev in m]
        levels.append(levels[-1] * (1 + statistics.fmean(changes)) if changes else levels[-1])
        info = state(levels)
        result[day] = None if info is None else info["regime"] + ("_HIGH_VOL" if info["volatility"] >= .02 else "_LOW_VOL")
    return result


def forecast_events(grouped, sessions, protocol):
    """Freeze all candidate inputs before looking at any future label."""
    base_protocol = json.loads(DEFAULT_PROTOCOL.read_text(encoding="utf-8"))
    base_protocol["minimum_average_turnover_twd"] = protocol["minimum_turnover_twd"]
    regimes = market_regimes(grouped, sessions)
    by_day, exclusions = {}, Counter()
    for symbol, bars in sorted(grouped.items()):
        for i, b in enumerate(bars):
            if b["date"] < protocol["test_from"]:
                continue
            row, reason = features(bars, i, base_protocol)
            if reason:
                exclusions[reason] += 1
                continue
            row["setups"] = setup_tags([x["close"] for x in bars[max(0, i - 60):i + 1]])
            row["previous_volume"] = b["volume"]
            row["source_sha256"] = b["source_sha256"]
            row["simulations"] = {}
            by_day.setdefault(b["date"], []).append(row)
    events, latency = {}, []
    model_latency = {v: [] for v in VARIANTS[2:]}
    for day, rows in sorted(by_day.items()):
        # Freeze top K once. Exclusions by a new module never backfill rank K+1.
        selected = rank(rows, "momentum")[:protocol["top_k"]]
        for row in selected:
            started = time.monotonic()
            for variant, block, conditioned in (("iid_costs", 1, False), ("block_costs", protocol["block_size"], False),
                                                ("conditioned_block_costs", protocol["block_size"], True)):
                model_started = time.monotonic()
                pool = pool_blocks(grouped[row["symbol"]], day, protocol, block, regimes, conditioned)
                seed = protocol["seed"] + int(digest([day, row["symbol"]])[:8], 16)
                row["simulations"][variant] = simulate(pool, row["close"], day, protocol, seed=seed)
                model_latency[variant].append(time.monotonic() - model_started)
            elapsed = time.monotonic() - started
            latency.append(elapsed)
            row["decision_latency_seconds"] = elapsed
        events[day] = selected
    return events, dict(feature_exclusions=dict(exclusions), total_frozen_events=sum(map(len, events.values())),
                        forecasting_wall_seconds=sum(latency), latency_mean_seconds=statistics.fmean(latency) if latency else None,
                        latency_max_seconds=max(latency) if latency else None,
                        latency_scope="ALL_THREE_MODELS_PER_FROZEN_CANDIDATE",
                        by_model={k: dict(mean_seconds=statistics.fmean(v) if v else None, max_seconds=max(v) if v else None) for k, v in model_latency.items()})


def label_event(event, grouped, sessions, protocol):
    """Offline scoring only. Full maturity required, even if a barrier hits early."""
    lookup = {b["date"]: b for b in grouped[event["symbol"]]}
    index = sessions.index(event["signal_date"])
    dates = sessions[index + 1:index + protocol["horizon"] + 2]
    if len(dates) != protocol["horizon"] + 1:
        return dict(status="IMMATURE", ready_at=None, outcome=None)
    if any(day not in lookup for day in dates):
        return dict(status="FUTURE_DATA_GAP", ready_at=dates[-1], outcome=None)
    if any(abs(lookup[b]["close"] / lookup[a]["close"] - 1) > .18 for a, b in zip(dates, dates[1:])):
        return dict(status="UNVERIFIED_ACTION", ready_at=dates[-1], outcome=None)
    market, costs = Market(), Costs(protocol["costs"])
    first = lookup[dates[0]]
    entry = costs.fill_price(first["open"], "BUY", market, dates[0])
    stop = market.round_price(entry * (1 - decimal(protocol["stop_fraction"])), dates[0])
    target = market.round_price(entry * (1 + decimal(protocol["target_fraction"])), dates[0], up=True)
    qty = costs.size(entry, protocol["notional_twd"], protocol["notional_twd"],
                     decimal(protocol["initial_cash_twd"]) * decimal(protocol["risk_fraction"]), stop)
    if not qty:
        return dict(status="UNSIZEABLE", ready_at=dates[-1], outcome=None)
    for j, day in enumerate(dates[1:]):
        px, reason = exit_price(lookup[day], stop, target, expired=j == protocol["horizon"] - 1)
        if px is not None:
            px = costs.fill_price(px, "SELL", market, day)
            pnl = costs.amount(px, qty, "SELL") - costs.amount(entry, qty, "BUY")
            return dict(status="MATURED_SCENARIO", ready_at=dates[-1], outcome=int(pnl > 0), net_pnl_twd=float(pnl), exit_reason=reason)
    raise AssertionError("TIME_EXIT_REQUIRED")


def score_forecasts(events, grouped, sessions, protocol):
    history = {v: [] for v in VARIANTS[2:]}
    labels, output = {}, []
    for day, rows in sorted(events.items()):
        pending = []
        for event in rows:
            key = (day, event["symbol"])
            label = label_event(event, grouped, sessions, protocol)
            labels[key] = label
            for variant, result in event["simulations"].items():
                raw = result["p_net_positive"]
                if raw is None:
                    continue
                cal = calibrated_probability(history[variant], raw, day, protocol)
                row = dict(variant=variant, signal_date=day, symbol=event["symbol"], evidence_id=event["evidence_id"],
                           probability=raw, calibrated_probability=cal["probability"], calibration=cal, **label)
                output.append(row)
                if label["outcome"] is not None:
                    pending.append((variant, row))
        # These contain labels, but the calibrator only accepts ready_at < t.
        for variant, row in pending:
            history[variant].append(row)
    metrics = {}
    common_keys = set.intersection(*[{(r["signal_date"], r["symbol"]) for r in output if r["variant"] == v and r["outcome"] is not None} for v in history])
    for variant in history:
        rows = [r for r in output if r["variant"] == variant]
        metrics[variant] = dict(raw=calibration_metrics([(r["probability"], r["outcome"]) for r in rows]),
                               prequential=calibration_metrics([(r["calibrated_probability"], r["outcome"]) for r in rows]),
                               matched_raw_for_prequential=calibration_metrics([(r["probability"], r["outcome"]) for r in rows if r["calibrated_probability"] is not None]),
                               common_model_coverage_raw=calibration_metrics([(r["probability"], r["outcome"]) for r in rows if (r["signal_date"], r["symbol"]) in common_keys]),
                               scope="ALL_FROZEN_TOP_K_SCENARIOS_NOT_ACCOUNT_TRADE_WIN_RATE")
    return labels, output, metrics


def portfolio_metrics(equity, trades, initial, benchmark):
    pnl = [t["net_pnl_twd"] for t in trades if t["side"] == "SELL"]
    gains, losses = sum(max(0, x) for x in pnl), -sum(min(0, x) for x in pnl)
    peak, drawdown, prev, returns = float(initial), 0.0, float(initial), []
    for row in equity:
        value = row["liquidation_equity_twd"]
        returns.append(value / prev - 1)
        peak = max(peak, value)
        drawdown = max(drawdown, (peak - value) / peak)
        prev = value
    deviation = statistics.stdev(returns) if len(returns) > 1 else 0
    net = prev / float(initial) - 1
    return dict(net_return=net, win_rate=sum(x > 0 for x in pnl) / len(pnl) if pnl else None,
                expectancy_twd=statistics.fmean(pnl) if pnl else None, profit_factor=gains / losses if losses else None,
                profit_factor_infinite=bool(gains and not losses), maximum_drawdown=drawdown,
                sharpe_ratio=statistics.fmean(returns) / deviation * (252 ** .5) if deviation else None,
                sharpe_assumption="252 daily sessions, zero risk-free rate, overlapping positions kept in NAV",
                benchmark_relative_return=net - benchmark, closed_trades=len(pnl), llm_api_cost_usd=0,
                valuation="cash plus unsettled net proceeds plus marked positions net of assumed liquidation costs")


def replay(grouped, sessions, events, protocol, variant):
    if variant not in VARIANTS:
        raise ValueError("UNKNOWN_VARIANT")
    costs, market = Costs(protocol["costs"]), Market()
    initial = decimal(protocol["initial_cash_twd"])
    cash, held, receivables, equity, trades, orders = initial, {}, [], [], [], []
    lookup = {s: {b["date"]: b for b in bars} for s, bars in grouped.items()}
    rejects, stale_marks = Counter(), 0
    for index, day in enumerate(sessions):
        if day < protocol["test_from"]:
            continue
        cash += sum((r["amount"] for r in receivables if r["release_index"] <= index), Decimal(0))
        receivables = [r for r in receivables if r["release_index"] > index]
        daily_spent = Decimal(0)
        previous_day = sessions[index - 1] if index else None
        for event in events.get(previous_day, []):
            symbol = event["symbol"]
            reason = None
            if variant == "skill_scanner" and not event["setups"]:
                reason = "NO_SCANNER_SETUP"
            if variant in VARIANTS[2:]:
                result = event["simulations"][variant]
                if result["status"] != "OK":
                    reason = result["status"]
                elif result["expected_net_twd"] <= 0:
                    reason = "NONPOSITIVE_SIMULATED_EXPECTANCY"
            row = lookup[symbol].get(day)
            if symbol in held:
                reason = reason or "ALREADY_HELD"
            if len(held) >= protocol["max_positions"]:
                reason = reason or "MAX_POSITIONS"
            if row is None:
                reason = reason or "NO_NEXT_SESSION_OPEN"
            if reason:
                rejects[reason] += 1
                orders.append(dict(signal_date=previous_day, date=day, symbol=symbol, status=reason))
                continue
            # Only t+1 open is used at entry. Never use t+1 high/low/volume to size.
            limit = decimal(market.rules["standard_daily_limit_fraction"])
            lower = market.round_price(decimal(event["close"]) * (1 - limit), day, up=True)
            upper = market.round_price(decimal(event["close"]) * (1 + limit), day)
            entry = costs.fill_price(row["open"], "BUY", market, day)
            if decimal(row["open"]) <= lower or entry >= upper:
                rejects["AT_INFERRED_LIMIT_OR_ACTION"] += 1
                orders.append(dict(signal_date=previous_day, date=day, symbol=symbol, status="AT_INFERRED_LIMIT_OR_ACTION"))
                continue
            stop = market.round_price(entry * (1 - decimal(protocol["stop_fraction"])), day)
            target = market.round_price(entry * (1 + decimal(protocol["target_fraction"])), day, up=True)
            budget = min(decimal(protocol["notional_twd"]), decimal(protocol["max_daily_buy_twd"]) - daily_spent)
            max_qty = min(market.rules["odd_lot_max"], int(decimal(event["previous_volume"]) * decimal(protocol["max_participation"])))
            qty = costs.size(entry, cash, budget, initial * decimal(protocol["risk_fraction"]), stop, max_qty=max_qty)
            if not qty:
                rejects["BUDGET_OR_LIQUIDITY"] += 1
                orders.append(dict(signal_date=previous_day, date=day, symbol=symbol, status="BUDGET_OR_LIQUIDITY"))
                continue
            paid = costs.amount(entry, qty, "BUY")
            cash -= paid
            daily_spent += paid
            held[symbol] = dict(qty=qty, paid=paid, stop=stop, target=target, entered=index,
                                expires=index + protocol["horizon"], mark=entry, mark_date=day)
            trades.append(dict(date=day, symbol=symbol, side="BUY", qty=qty, price=float(entry),
                               cash_delta_twd=-float(paid), net_pnl_twd=None))
            orders.append(dict(signal_date=previous_day, date=day, symbol=symbol, status="SIMULATED_FILLED"))
        for symbol, position in list(held.items()):
            bar = lookup[symbol].get(day)
            if bar is None:
                stale_marks += 1
                continue
            position.update(mark=decimal(bar["close"]), mark_date=day)
            if index <= position["entered"]:
                continue
            # A daily bar cannot certify liquidity at a locked limit; defer sale.
            if bar["high"] == bar["low"]:
                rejects["LOCKED_OR_NO_RANGE_EXIT_DEFERRED"] += 1
                continue
            px, reason = exit_price(bar, position["stop"], position["target"], expired=index >= position["expires"])
            if px is None:
                continue
            fill = costs.fill_price(px, "SELL", market, day)
            proceeds = costs.amount(fill, position["qty"], "SELL")
            receivables.append(dict(release_index=index + market.rules["settlement_sessions"], amount=proceeds))
            trades.append(dict(date=day, symbol=symbol, side="SELL", qty=position["qty"], price=float(fill),
                               cash_delta_twd=0, unsettled_proceeds_twd=float(proceeds),
                               net_pnl_twd=float(proceeds - position["paid"]), reason=reason))
            del held[symbol]
        unsettled = sum((r["amount"] for r in receivables), Decimal(0))
        liquidation = sum((costs.amount(costs.fill_price(p["mark"], "SELL", market, day), p["qty"], "SELL") for p in held.values()), Decimal(0))
        if cash < 0 or any(p["qty"] <= 0 for p in held.values()):
            raise AssertionError("PORTFOLIO_INVARIANT_BROKEN")
        equity.append(dict(date=day, cash_twd=float(cash), unsettled_twd=float(unsettled),
                           liquidation_value_twd=float(liquidation), liquidation_equity_twd=float(cash + unsettled + liquidation),
                           positions=len(held), stale_symbols=[s for s, p in held.items() if p["mark_date"] != day]))
    return dict(variant=variant, equity=equity, trades=trades, orders=orders, rejected=dict(rejects),
                open_positions=[dict(symbol=s, qty=p["qty"], mark_date=p["mark_date"]) for s, p in held.items()],
                stale_mark_days=stale_marks)


def benchmark(grouped, sessions, protocol):
    market, costs = Market(), Costs(protocol["costs"])
    first = next(d for d in sessions if d >= protocol["test_from"])
    cash = decimal(protocol["initial_cash_twd"])
    per_symbol = cash / len(grouped)
    positions, equity = {}, []
    maps = {s: {b["date"]: b for b in bars} for s, bars in grouped.items()}
    for symbol, rows in maps.items():
        if first not in rows:
            continue
        price = costs.fill_price(rows[first]["open"], "BUY", market, first)
        qty = min(999, int(per_symbol // price))
        while qty and costs.amount(price, qty, "BUY") > per_symbol:
            qty -= 1
        if qty:
            cash -= costs.amount(price, qty, "BUY")
            positions[symbol] = dict(qty=qty, mark=price)
    for day in sessions:
        if day < first:
            continue
        for s, p in positions.items():
            if day in maps[s]:
                p["mark"] = maps[s][day]["close"]
        value = cash + sum((costs.amount(costs.fill_price(p["mark"], "SELL", market, day), p["qty"], "SELL") for p in positions.values()), Decimal(0))
        equity.append(dict(date=day, liquidation_equity_twd=float(value)))
    return dict(name="FIXED_CURRENT_UNIVERSE_EQUAL_WEIGHT_BUY_HOLD_NOT_TAIEX", equity=equity,
                net_return=equity[-1]["liquidation_equity_twd"] / float(decimal(protocol["initial_cash_twd"])) - 1)


def run(dataset, output, protocol=None):
    protocol = validate_protocol(deepcopy(protocol or load_protocol()))
    # Validate caller-supplied config too, without opening production config.
    if protocol["mode"] != "PAPER_ONLY":
        raise ValueError("PAPER_ONLY_REQUIRED")
    out = Path(output).resolve()
    root = Path(__file__).resolve().parents[2]
    if not out.is_relative_to(root / "research-runs"):
        raise ValueError("OUTPUT_MUST_BE_UNDER_RESEARCH_RUNS")
    grouped, sessions, manifest = load_dataset(dataset)
    if not any(day >= protocol["test_from"] for day in sessions):
        raise ValueError("NO_TEST_SESSIONS")
    out.mkdir(parents=True, exist_ok=False)
    save(out / "protocol.json", protocol)
    freeze_baseline(root, out)
    source = {str(p.relative_to(root)): digest(p.read_bytes()) for p in Path(__file__).parent.glob("*") if p.is_file()}
    save(out / "experiment_source.json", source)
    for filename in source:
        target = out / "source_snapshot" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / filename).read_bytes())
    skill = root / "skills/tw-trading-decision/SKILL.md"
    save(out / "skill_manifest.json", dict(path=str(skill.relative_to(root)), sha256=digest(skill.read_bytes())))
    (out / "skill.md").write_bytes(skill.read_bytes())
    events, timing = forecast_events(grouped, sessions, protocol)
    save(out / "decision_events.json", events)  # No labels in model input artifact.
    labels, forecasts, calibration = score_forecasts(events, grouped, sessions, protocol)
    save(out / "labels.json", [dict(signal_date=k[0], symbol=k[1], **v) for k, v in labels.items()])
    save(out / "forecasts_scored.json", forecasts)
    ref = benchmark(grouped, sessions, protocol)
    save(out / "benchmark.json", ref)
    results = {}
    for variant in VARIANTS:
        replayed = replay(grouped, sessions, events, protocol, variant)
        metrics = portfolio_metrics(replayed["equity"], replayed["trades"], protocol["initial_cash_twd"], ref["net_return"])
        metrics["probability_calibration"] = calibration.get(variant, dict(status="NO_PROBABILITY_FORECAST"))
        metrics["decision_latency_seconds"] = timing["by_model"][variant] if variant in VARIANTS[2:] else dict(status="RANKING_ONLY_NOT_SEPARATELY_TIMED")
        # Contiguous OOS folds; no tuning or future labels at fold boundaries.
        folds = []
        for quarter in sorted({r["date"][:4] + "-Q" + str((int(r["date"][5:7]) - 1) // 3 + 1) for r in replayed["equity"]}):
            rows = [r for r in replayed["equity"] if r["date"][:4] + "-Q" + str((int(r["date"][5:7]) - 1) // 3 + 1) == quarter]
            before = next((replayed["equity"][i - 1]["liquidation_equity_twd"] for i, r in enumerate(replayed["equity"]) if r is rows[0] and i > 0), float(decimal(protocol["initial_cash_twd"])))
            folds.append(dict(fold=quarter, start=rows[0]["date"], end=rows[-1]["date"],
                              return_fraction=rows[-1]["liquidation_equity_twd"] / before - 1,
                              method="EXPANDING_PAST_POOL_PREQUENTIAL_CALIBRATION_NO_PARAMETER_SEARCH"))
        metrics["walk_forward_folds"] = folds
        save(out / (variant + ".json"), dict(metrics=metrics, **replayed))
        results[variant] = metrics
    summary = dict(version=VERSION, protocol_sha256=digest(protocol), data_manifest_sha256=digest(Path(dataset, "manifest.json").read_bytes()),
                   dataset_rows=manifest["rows"], date_from=next(d for d in sessions if d >= protocol["test_from"]), date_to=sessions[-1],
                   data_role="RETROSPECTIVE_DEVELOPMENT_DIAGNOSTIC_NOT_CERTIFIED_OOS", point_in_time_certified=False,
                   warnings=["CURRENT_SURVIVOR_UNIVERSE", "RAW_UNADJUSTED_UNVERIFIED_ACTIONS", "HISTORICAL_PUBLICATION_TIMES_UNVERIFIED",
                             "DAILY_OPEN_PROXY_NOT_ODDLOT_FILL", "PRICE_LIMITS_INFERRED_FROM_PREVIOUS_CLOSE_NOT_EXCHANGE_REFERENCES",
                             "SCENARIO_CALIBRATION_NOT_EXECUTION_WIN_RATE", "FIVE_VARIANTS_NOT_A_TUNED_WINNER_SELECTION",
                             "FORWARD_VALIDATION_PENDING", "PRODUCTION_LLM_BASELINE_NOT_REPLAYED"],
                   benchmark_name=ref["name"], benchmark_net_return=ref["net_return"], label_status=dict(Counter(r["status"] for r in labels.values())),
                   timing=timing, variants=results)
    save(out / "summary.json", summary)
    if any(digest((root / name).read_bytes()) != expected for name, expected in source.items()):
        # Preserve the attempt, but never report it as a reproducible source run.
        save(out / "SOURCE_CHANGED_DURING_RUN.json", dict(status="INVALID_SOURCE_PROVENANCE"))
        raise ValueError("SOURCE_CHANGED_DURING_RUN")
    return summary
