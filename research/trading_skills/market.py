"""Explicit market rules, Decimal costs and a read-only Capital snapshot adapter."""
from __future__ import annotations

from datetime import datetime, time, timezone, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import json
from pathlib import Path
import re


def decimal(value):
    if isinstance(value, bool):
        raise ValueError("BOOLEAN_NOT_MONEY")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("NONFINITE_MONEY")
    return result


def instant(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return result


class Market:
    def __init__(self, rules=None):
        self.rules = rules or json.loads(Path(__file__).with_name("market_rules.json").read_text(encoding="utf-8"))
        # Supported Taiwanese rules start in 2005, after Taiwan's last DST.
        self.tz = timezone(timedelta(minutes=self.rules["utc_offset_minutes"]), self.rules["timezone"])

    def effective(self, key, day):
        choices = [r for r in self.rules[key] if r["from"] <= day]
        if not choices:
            raise ValueError("MARKET_RULES_NOT_AVAILABLE_FOR_DATE")
        return max(choices, key=lambda r: r["from"])

    def tick(self, price, day):
        price = decimal(price)
        if price <= 0:
            raise ValueError("NONPOSITIVE_PRICE")
        for upper, step in self.effective("tick_tables", day)["bands"]:
            if upper is None or price < decimal(upper):
                return decimal(step)
        raise ValueError("MISSING_TICK_BAND")

    def round_price(self, price, day, up=False):
        price = decimal(price)
        step = self.tick(price, day)
        result = (price / step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * step
        # Re-evaluate at a band boundary (e.g. 999.9 rounds up to 1000).
        final_step = self.tick(result, day)
        return (result / final_step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * final_step

    def symbol(self, symbol, exchange):
        if exchange not in self.rules["markets"] or not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{4}", symbol):
            raise ValueError("UNSUPPORTED_COMMON_STOCK_ID")
        return f"{exchange}:{symbol}"

    def session_open(self, at, sessions):
        local = instant(at).astimezone(self.tz)
        day = local.date().isoformat()
        start = self.effective("oddlot_sessions", day)["first_match"]
        return day in sessions and time.fromisoformat(start) <= local.time().replace(tzinfo=None) < time.fromisoformat(self.rules["regular_close"])


class Costs:
    def __init__(self, config):
        self.rate = decimal(config["commission_rate"])
        self.minimum = decimal(config["minimum_commission_twd"])
        self.tax = decimal(config["sell_tax_rate"])
        self.slip = decimal(config["slippage_bps"]) / 10000
        if any(x < 0 for x in (self.rate, self.minimum, self.tax, self.slip)) or max(self.rate, self.tax, self.slip) >= 1:
            raise ValueError("INVALID_COSTS")

    def fee(self, gross):
        return max(self.minimum, decimal(gross) * self.rate).to_integral_value(rounding=ROUND_CEILING)

    def amount(self, price, qty, side):
        if type(qty) is not int or qty <= 0 or side not in ("BUY", "SELL"):
            raise ValueError("INVALID_FILL")
        gross = decimal(price) * qty
        if gross <= 0:
            raise ValueError("INVALID_FILL_PRICE")
        fee = self.fee(gross)
        tax = (gross * self.tax).to_integral_value(rounding=ROUND_CEILING) if side == "SELL" else Decimal(0)
        return gross + fee if side == "BUY" else gross - fee - tax

    def fill_price(self, price, side, market, day):
        return market.round_price(decimal(price) * (1 + self.slip if side == "BUY" else 1 - self.slip), day, up=side == "BUY")

    def size(self, price, cash, budget, risk_cash, stop, max_qty=999):
        if type(max_qty) is not int or max_qty < 0:
            raise ValueError("INVALID_QUANTITY_CAP")
        price, cash, budget, risk_cash, stop = map(decimal, (price, cash, budget, risk_cash, stop))
        if stop <= 0 or stop >= price:
            raise ValueError("INVALID_LONG_STOP")
        cap = min(cash, budget)
        qty = min(max_qty, max(0, int(cap // price)))
        # Include both fees and tax in nominal stop risk; gaps can still exceed it.
        while qty > 0 and (self.amount(price, qty, "BUY") > cap or
                           self.amount(price, qty, "BUY") - self.amount(stop, qty, "SELL") > risk_cash):
            qty -= 1
        return qty


def capital_quote(raw, at, sessions, protocol, market=None):
    """Consume an existing multi_market_snapshot quote; never connects to SKCOM.

    Forward use additionally requires exchange, actual price limits and side sizes
    in shares. Missing legacy fields are rejected, never guessed from board lots.
    """
    market = market or Market()
    if raw.get("source") != "CAPITAL_SKCOM" or raw.get("retrieval_mode") != "BROKER":
        raise ValueError("VERIFIED_CAPITAL_QUOTE_REQUIRED")
    market.symbol(raw["symbol"], raw["exchange"])
    if raw.get("quote_basis") != "capital_intraday_odd_lot" or raw.get("volume_unit") != "shares" or raw.get("instrument_type") != "COMMON_STOCK":
        raise ValueError("EXPLICIT_COMMON_STOCK_ODDLOT_SHARES_REQUIRED")
    if raw.get("quote_status") != "LIVE" or raw.get("is_trial") is not False:
        raise ValueError("FRESH_NONTRIAL_QUOTE_REQUIRED")
    now = instant(at)
    stamp = instant(raw["quote_timestamp"])
    received = instant(raw["received_at"])
    if not stamp <= received <= now or not 0 <= (now - stamp).total_seconds() <= protocol["forward_quote_max_age_seconds"]:
        raise ValueError("STALE_OR_FUTURE_QUOTE")
    if not market.session_open(at, sessions):
        raise ValueError("OUTSIDE_VERIFIED_SESSION")
    bid, ask, lower, upper = (decimal(raw[k]) for k in ("bid", "ask", "limit_down", "limit_up"))
    if not 0 < lower <= bid <= ask <= upper or (ask - bid) / ask > decimal(protocol["forward_max_spread_fraction"]):
        raise ValueError("INVALID_LIMITS_OR_SPREAD")
    day = now.astimezone(market.tz).date().isoformat()
    if any(market.round_price(p, day) != p for p in (bid, ask, lower, upper)):
        raise ValueError("OFF_TICK_QUOTE")
    for key in ("bid_size_shares", "ask_size_shares"):
        if type(raw.get(key)) is not int or raw[key] <= 0:
            raise ValueError("SIDE_DEPTH_IN_SHARES_REQUIRED")
    if not raw.get("evidence_id"):
        raise ValueError("QUOTE_EVIDENCE_REQUIRED")
    return dict(symbol=raw["symbol"], exchange=raw["exchange"], bid=bid, ask=ask,
                bid_size_shares=raw["bid_size_shares"], ask_size_shares=raw["ask_size_shares"],
                limit_down=lower, limit_up=upper,
                evidence_id=raw["evidence_id"], at=at)
