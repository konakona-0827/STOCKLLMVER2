"""Causal short-cycle scouting: pullback, reclaim trigger, and momentum."""
from copy import deepcopy
from dataclasses import dataclass, asdict
from statistics import fmean
import math

from .contracts import DataError, digest, timestamp

BUY_TYPES = {"TREND_PULLBACK": "趨勢回檔承接", "RECLAIM_BREAKOUT": "站回／突破承接", "MOMENTUM_CONTINUATION": "動能延續"}
READY_STAGES = {"SHORT_PULLBACK_READY", "SHORT_RECLAIM_READY", "SHORT_RECLAIM_TRIGGER", "SHORT_MOMENTUM_READY"}


@dataclass(frozen=True)
class ShortCycleParameters:
    setup_kind: str = "TREND_PULLBACK"
    trend_window_sessions: int = 20
    short_window_sessions: int = 5
    signal_window_sessions: int = 3
    volume_window_sessions: int = 5
    min_relative_volume: float = .65
    min_momentum_fraction: float = .008
    max_momentum_fraction: float = .25
    max_daily_gain_fraction: float = .12
    min_close_location: float = .35
    max_pullback_fraction: float = .10
    reclaim_buffer_fraction: float = .001
    max_trigger_gap_fraction: float = .10
    max_opening_gap_fraction: float = .015
    stop_price_fraction: float = .015
    minimum_turnover_jpy: float = 500_000_000
    min_horizon_range_fraction: float = .02

    def __post_init__(self):
        if self.setup_kind not in BUY_TYPES:
            raise DataError("INVALID_SHORT_CYCLE_KIND")
        for k,v in asdict(self).items():
            if k == "setup_kind":continue
            if k.endswith("sessions"):
                if type(v) is not int or not 2 <= v <= 60:raise DataError("INVALID_SHORT_CYCLE_WINDOW")
            elif type(v) not in (int,float) or not math.isfinite(v) or v <= 0:
                raise DataError("INVALID_SHORT_CYCLE_PARAMETER:" + k)
        for k in ("min_momentum_fraction","max_momentum_fraction","max_daily_gain_fraction","min_close_location",
                "max_pullback_fraction","reclaim_buffer_fraction","max_trigger_gap_fraction","max_opening_gap_fraction","stop_price_fraction"):
            if getattr(self,k) >= 1:raise DataError("INVALID_SHORT_CYCLE_FRACTION")
        if self.short_window_sessions > self.trend_window_sessions or self.min_momentum_fraction >= self.max_momentum_fraction:
            raise DataError("INCONSISTENT_SHORT_CYCLE_PARAMETERS")


def detect_short_cycle(series, cutoff, definition, context):
    p = ShortCycleParameters(**definition["parameters"])
    bars = [b for b in series.bars if timestamp(b.closed_at) <= cutoff and timestamp(b.available_at) <= cutoff]
    result = {"pattern_id":definition["id"],"version":definition["version"],"definition_digest":digest(definition),
        "definition":deepcopy(definition),"stage":"NO_MATCH","stage_label":"尚無短線條件","research_candidate":False,
        "support":None,"metrics":{},"evidence":[],"required_evidence_ids":[],"reason_codes":[],
        "buy_type":p.setup_kind,"buy_type_label":BUY_TYPES[p.setup_kind],
        "empirical_status":"EXPLORATORY_SHORT_CYCLE_NOT_PREDICTED_WIN_RATE","execution_enabled":False}
    if len(bars) < max(p.trend_window_sessions,p.volume_window_sessions)+2:return result
    last, prev = bars[-1], bars[-2]
    recent = bars[-p.short_window_sessions:]
    before = bars[-p.signal_window_sessions-1:-1]
    ma_short = fmean(b.close for b in recent)
    ma_before = fmean(b.close for b in bars[-p.short_window_sessions-1:-1])
    ma_long = fmean(b.close for b in bars[-p.trend_window_sessions:])
    volume_base = fmean(b.volume for b in bars[-p.volume_window_sessions-1:-1])
    rel_volume = last.volume/volume_base if volume_base else 0
    momentum = last.close/bars[-p.signal_window_sessions-1].close-1
    daily = last.close/prev.close-1
    location = (last.close-last.low)/(last.high-last.low) if last.high > last.low else .5
    prior_close_top = max(b.close for b in before)
    prior_high = max(b.high for b in before)
    recent_low = min(b.low for b in recent)
    pullback = 1-last.close/max(b.high for b in recent)
    range_fraction = max(b.high for b in recent)/min(b.low for b in recent)-1
    turnover = fmean(b.close*b.volume for b in bars[-20:])
    true_ranges = [max(b.high-b.low,abs(b.high-a.close),abs(b.low-a.close)) for a,b in zip(bars[-15:-1],bars[-14:])]
    atr_fraction = fmean(true_ranges)/last.close
    held = last.low >= min(b.low for b in before)*(1-.005)
    trigger, ready, stage = None, False, "NO_MATCH"
    if p.setup_kind == "TREND_PULLBACK":
        ready = (last.close >= ma_long*.98 and ma_short >= ma_long*.98 and .003 <= pullback <= p.max_pullback_fraction
            and held and location >= p.min_close_location)
        stage = "SHORT_PULLBACK_READY" if ready else stage
    elif p.setup_kind == "RECLAIM_BREAKOUT":
        ready = last.close > prior_close_top*(1+p.reclaim_buffer_fraction) and prev.close <= ma_before and location >= .5
        stage = "SHORT_RECLAIM_READY" if ready else stage
        # A completed daily hold can nominate a conditional next-session trigger.
        # It does not claim that a breakout has happened or that today's low held intraday first.
        if not ready and held and rel_volume <= 1.05 and last.close < last.high and pullback <= p.max_pullback_fraction:
            trigger = last.high*(1+p.reclaim_buffer_fraction)
            ready, stage = True, "SHORT_RECLAIM_TRIGGER"
    else:
        ready = (last.close > ma_short and last.close >= prior_high*.995 and momentum >= p.min_momentum_fraction
            and location >= .5)
        stage = "SHORT_MOMENTUM_READY" if ready else stage
    reasons=[]
    if rel_volume < p.min_relative_volume:reasons.append("SHORT_RELATIVE_VOLUME_TOO_LOW")
    if momentum > p.max_momentum_fraction or daily > p.max_daily_gain_fraction:reasons.append("SHORT_MOVE_OVERHEATED")
    if turnover < p.minimum_turnover_jpy:reasons.append("SHORT_LIQUIDITY_TOO_LOW")
    if range_fraction < p.min_horizon_range_fraction:reasons.append("SHORT_RECENT_RANGE_TOO_SMALL")
    if not ready:reasons.append("SHORT_ENTRY_TYPE_NOT_FORMED")
    if reasons:stage="SHORT_WATCH" if ready else "NO_MATCH"
    entry = trigger or last.close
    lower = entry*(1-p.stop_price_fraction)
    anchor_session = last.session
    support = {"anchor_session":anchor_session,"anchor":entry,"lower":lower,"upper":entry*(1+p.max_trigger_gap_fraction),
        "frozen_at":last.session,"invalidation_basis":"TACTICAL_PRICE_STOP_REPRICED_BEFORE_ENTRY_FIXED_AFTER_CONFIRMED_FILL",
        "risk_level_meaning":"短線戰術風險線，不宣稱它是已確認結構支撐。","structural_recent_low":recent_low}
    metrics={"latest_close":last.close,"ma_short":ma_short,"ma_long":ma_long,"relative_volume":rel_volume,
        "momentum_fraction":momentum,"daily_return_fraction":daily,"close_location":location,"pullback_fraction":pullback,
        "horizon_range_fraction":range_fraction,"atr_fraction":atr_fraction,"average_turnover_jpy":turnover,
        "entry_trigger_reference":trigger,"entry_trigger_required":trigger is not None,
        "entry_ceiling_reference":min(entry*(1+p.max_trigger_gap_fraction),last.close*(1+p.max_trigger_gap_fraction)) if trigger else entry*(1+p.max_opening_gap_fraction),
        "stop_price_fraction":p.stop_price_fraction,"distance_from_support_fraction":0,
        "support_test_sessions":[last.session],"volume_contraction_ratio":rel_volume}
    if p.stop_price_fraction < atr_fraction:reasons.append("TACTICAL_STOP_INSIDE_ATR_NORMAL_NOISE")
    if series.metadata.get("volume_comparable") is not True:reasons.append("RAW_VOLUME_COMPARABILITY_UNVERIFIED")
    blocked={"SHORT_RELATIVE_VOLUME_TOO_LOW","SHORT_MOVE_OVERHEATED","SHORT_LIQUIDITY_TOO_LOW","SHORT_RECENT_RANGE_TOO_SMALL","SHORT_ENTRY_TYPE_NOT_FORMED"}
    candidate=ready and not blocked.intersection(reasons)
    if not candidate and not ready and range_fraction>=max(.06,p.min_horizon_range_fraction) and turnover>=p.minimum_turnover_jpy:
        stage="VOLATILE_WATCH"
        reasons.append("SHORT_DIRECTION_UNCONFIRMED")
        metrics["watch_trigger_reference"]=prior_close_top*(1+p.reclaim_buffer_fraction)
    evidence=[{"id":f"{series.symbol}:SHORT_SIGNAL:{definition['id']}:{last.session}","kind":"SHORT_SIGNAL","session":last.session,
        "available_at":last.available_at.isoformat() if hasattr(last.available_at,"isoformat") else last.available_at,
        "values":{"buy_type":p.setup_kind,"stage":stage,"latest_close":last.close,"previous_close":prev.close,
            "relative_volume":rel_volume,"ma_short":ma_short,"ma_long":ma_long,"trigger_reference":trigger,
            "stop_price_fraction":p.stop_price_fraction,"horizon_range_fraction":range_fraction}}]
    result.update(stage=stage,stage_label=BUY_TYPES[p.setup_kind]+("：條件式突破待觸發" if trigger else "：短線研究候選") if candidate else "高波動觀察：方向待確認" if stage=="VOLATILE_WATCH" else "短線條件待確認",
        research_candidate=candidate,support=support,metrics=metrics,evidence=evidence,required_evidence_ids=[evidence[0]["id"]],
        reason_codes=reasons,limitations=["TARGET_IS_OBJECTIVE_NOT_PRICE_FORECAST","DAILY_BARS_CANNOT_CONFIRM_INTRADAY_TRIGGER_ORDER",
            "TACTICAL_STOP_MAY_BE_HIT_BY_NORMAL_VOLATILITY","NO_OUT_OF_SAMPLE_VALIDATED_EDGE"])
    return result
