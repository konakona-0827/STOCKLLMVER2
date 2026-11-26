"""One shared limit-order, tax/fee, settlement and depth model for both arms."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, ROUND_CEILING
import json
import statistics

from .common import decimal, instant, costs, fill_price, round_price, digest, Market


class Book:
    def __init__(self, config, state=None):
        if config.get("mode")!="PAPER_ONLY":
            raise ValueError("PAPER_ONLY_REQUIRED")
        self.config = config
        self.cash = decimal(config["initial_cash_twd"])
        self.positions, self.pending, self.receivables = {}, [], []
        self.orders, self.fills, self.equity, self.decisions = [], [], [], []
        self.closed_trades = []
        self.depth_used, self.bought_by_day = {}, {}
        self.current_at = None
        self.performance_peak=float(self.cash)
        self.performance_drawdown=0.0
        self.daily_closes={}
        if state:
            self.__dict__.update(deepcopy(state))
            self.config = config
            self.cash = decimal(self.cash)

    def state(self):
        return json.loads(json.dumps(self.__dict__, default=str, allow_nan=False))

    def cost(self, symbol):
        instrument = self.positions.get(symbol, {}).get("asset_type", "STOCK")
        return costs(self.config, instrument)

    def quantity(self, symbol):
        return sum(x["qty"] for x in self.positions.get(symbol, {}).get("lots", []))

    def basis(self, symbol):
        return sum((decimal(x["basis"]) for x in self.positions.get(symbol, {}).get("lots", [])), Decimal(0))

    def sellable(self, symbol, day):
        return sum(x["qty"] for x in self.positions.get(symbol, {}).get("lots", []) if x["day"] < day)

    def reserved(self):
        return sum((decimal(o["reserve"]) for o in self.pending if o["side"] == "BUY"), Decimal(0))

    def available(self, day):
        if self.config.get('funding_mode')=='LATEST_BRANCH_CUMULATIVE':
            daily=sum((decimal(o['funding_required']) for o in self.orders if o['side']=='BUY' and o['day']==day and o['status']!='REJECTED'),Decimal(0))
            committed=sum((decimal(p['funding_committed']) for p in self.positions.values()),Decimal(0))
            return max(Decimal(0),min(self.cash-decimal(self.config['min_available_to_buy_twd'])-self.reserved(),
                decimal(self.config['strategy_cap_twd'])-committed-self.reserved(),decimal(self.config['max_daily_buy_twd'])-daily))
        daily = decimal(self.config["max_daily_buy_twd"]) - decimal(self.bought_by_day.get(day, 0)) - self.reserved()
        return max(Decimal(0), min(self.cash - self.reserved(), daily))

    def funding_required(self,price,qty,instrument='STOCK'):
        gross=decimal(price)*qty
        if self.config.get('funding_mode')=='LATEST_BRANCH_CUMULATIVE':
            return gross+max(gross*decimal(self.config['buy_cash_buffer_rate']),decimal(self.config['min_buy_fee_reserve_twd']))
        return costs(self.config,instrument).amount(price,qty,'BUY')

    def close_session(self,day,at):
        if instant(at).astimezone(Market().tz).date().isoformat()!=day:raise ValueError('SESSION_CLOSE_DATE_MISMATCH')
        for order in list(self.pending):
            if order['day']<=day:
                order['status']='PARTIALLY_FILLED_EXPIRED_ROD' if order['remaining_qty']<order['qty'] else 'EXPIRED_ROD'
                order['reserve']='0';self.pending.remove(order)
        self.mark(at)

    def advance(self, quotes, at, sessions, *, strict_depth):
        if self.current_at and instant(at) < instant(self.current_at):
            raise ValueError("NON_MONOTONIC_MARKET")
        self.current_at = at
        day = instant(at).astimezone(Market().tz).date().isoformat()
        if day not in sessions:
            raise ValueError("UNKNOWN_SESSION")
        self.cash += sum((decimal(r["amount"]) for r in self.receivables if r["release_date"] <= day), Decimal(0))
        self.receivables = [r for r in self.receivables if r["release_date"] > day]
        qmap = {q["symbol"]: q for q in quotes}
        for order in list(self.pending):
            if day != order["day"]:
                order["status"] = 'PARTIALLY_FILLED_EXPIRED_ROD' if order['remaining_qty']<order['qty'] else "EXPIRED_ROD"
                order['reserve']='0'
                self.pending.remove(order)
                continue
            q = qmap.get(order["symbol"])
            if not q or q.get("is_trial") is not False or q.get("quote_status") != "LIVE":
                order["last_unfilled_reason"] = "NO_FRESH_NONTRIAL_SIDE_QUOTE"
                continue
            observed = q.get("depth_received_at") if strict_depth else q.get("quote_timestamp")
            if not observed or instant(observed) <= instant(order["ready_at"]) or instant(observed) > instant(at):
                order["last_unfilled_reason"] = "NO_POST_DECISION_OBSERVATION"
                continue
            if not q.get("quote_timestamp") or not 0 <= (instant(at) - instant(q["quote_timestamp"])).total_seconds() <= self.config["max_quote_age_seconds"]:
                order["last_unfilled_reason"] = "STALE_TRADE_TIMESTAMP"
                continue
            side = order["side"]
            field = "ask" if side == "BUY" else "bid"
            if q.get(field) is None or decimal(q[field]) <= 0:
                continue
            if strict_depth:
                if not q.get('received_at') or not 0<=(instant(at)-instant(q['received_at'])).total_seconds()<=self.config['max_quote_age_seconds']:
                    order['last_unfilled_reason']='INVALID_OR_STALE_LOCAL_RECEIPT'
                    continue
                if q.get('bid') is None or q.get('ask') is None or not 0<decimal(q['bid'])<=decimal(q['ask']):
                    order['last_unfilled_reason']='INVALID_OR_CROSSED_BOOK'
                    continue
                if q.get("source")!="CAPITAL_SKCOM" or q.get("quote_basis")!="capital_intraday_odd_lot" or q.get("volume_unit")!="shares":
                    order["last_unfilled_reason"]="VERIFIED_CAPITAL_ODDLOT_REQUIRED"
                    continue
                if q.get("depth_status") != "ODDLOT_CALLBACK_SHARES" or (instant(at) - instant(observed)).total_seconds() > self.config["max_depth_age_seconds"]:
                    order["last_unfilled_reason"] = "MISSING_OR_STALE_DEPTH"
                    continue
                if not q.get("limit_up") or not q.get("limit_down"):
                    order["last_unfilled_reason"] = "ACTUAL_PRICE_LIMITS_UNAVAILABLE"
                    continue
            instrument = order["asset_type"]
            px = fill_price(q[field], side, day, self.config, instrument)
            limit = decimal(order["limit"])
            if (side == "BUY" and px > limit) or (side == "SELL" and px < limit):
                order["last_unfilled_reason"] = "LIMIT_NOT_CROSSED_AFTER_SLIPPAGE"
                continue
            if strict_depth and not decimal(q["limit_down"]) <= px <= decimal(q["limit_up"]):
                order["last_unfilled_reason"] = "SLIPPAGE_OUTSIDE_EXCHANGE_LIMIT"
                continue
            quantity = order["remaining_qty"]
            depth_key = str((order["symbol"], side, observed, q.get(field), q.get(field + "_size_shares")))
            if strict_depth:
                depth = q.get(field + "_size_shares")
                if type(depth) is not int or depth <= 0:
                    continue
                quantity = min(quantity, max(0, depth - self.depth_used.get(depth_key, 0)))
            if side == "SELL":
                quantity = min(quantity, self.sellable(order["symbol"], day))
            if quantity <= 0:
                order["last_unfilled_reason"] = "NO_REMAINING_DEPTH_OR_SELLABLE_SHARES"
                continue
            cost_model = costs(self.config, instrument)
            gross = px * quantity
            total_gross = decimal(order.get("gross_filled", "0")) + gross
            total_fee = cost_model.fee(total_gross)
            fee = total_fee - decimal(order.get("fees_paid", "0"))
            total_tax = (total_gross * cost_model.tax).to_integral_value(rounding=ROUND_CEILING) if side == "SELL" else Decimal(0)
            tax = total_tax - decimal(order.get("tax_paid", "0"))
            amount = gross + fee if side == "BUY" else gross - fee - tax
            symbol = order["symbol"]
            if side == "BUY":
                if amount > self.cash:
                    raise AssertionError("RESERVED_BUY_EXCEEDS_CASH")
                self.cash -= amount
                self.bought_by_day[day] = str(decimal(self.bought_by_day.get(day, 0)) + amount)
                position = self.positions.setdefault(symbol, dict(asset_type=instrument, lots=[], mark=str(q.get("bid") or px),opened_at=at,realized="0"))
                if self.config.get('funding_mode')=='LATEST_BRANCH_CUMULATIVE':
                    prior_gross=decimal(order.get('gross_filled','0'))
                    prior_funding=self.funding_required(prior_gross,1,instrument) if prior_gross else Decimal(0)
                    new_funding=self.funding_required(total_gross,1,instrument)
                    position['funding_committed']=str(decimal(position.get('funding_committed',0))+new_funding-prior_funding)
                position["lots"].append(dict(qty=quantity, basis=str(amount), day=day, at=at, plan=order["plan"]))
                pnl, basis = None, amount
            else:
                if self.config.get('funding_mode')=='LATEST_BRANCH_CUMULATIVE':
                    p=self.positions[symbol]
                    p['funding_committed']=str(decimal(p['funding_committed'])*(self.quantity(symbol)-quantity)/self.quantity(symbol))
                remaining, basis = quantity, Decimal(0)
                for lot in self.positions[symbol]["lots"]:
                    if lot["day"] >= day or remaining == 0:
                        continue
                    take = min(remaining, lot["qty"])
                    used = decimal(lot["basis"]) * take / lot["qty"]
                    basis += used
                    lot["basis"] = str(decimal(lot["basis"]) - used)
                    lot["qty"] -= take
                    remaining -= take
                self.positions[symbol]["lots"] = [x for x in self.positions[symbol]["lots"] if x["qty"]]
                position=self.positions[symbol]
                pnl = amount - basis
                position["realized"]=str(decimal(position.get("realized",0))+pnl)
                if not position["lots"]:
                    self.closed_trades.append(dict(symbol=symbol,opened_at=position["opened_at"],closed_at=at,net_pnl_twd=float(decimal(position["realized"]))))
                    del self.positions[symbol]
                index = sessions.index(day)
                if index + self.config["settlement_sessions"] >= len(sessions):
                    raise ValueError("SETTLEMENT_CALENDAR_INCOMPLETE")
                self.receivables.append(dict(amount=str(amount), release_date=sessions[index + self.config["settlement_sessions"]]))
            self.fills.append(dict(order_id=order["id"], symbol=symbol, side=side, qty=quantity, price=str(px),
                                   amount=str(amount), basis=str(basis), net_pnl_twd=None if pnl is None else float(pnl),
                                   at=at, quote_at=observed, evidence_id=q.get("evidence_id"),
                                   commission_twd=str(fee), tax_twd=str(tax),
                                   fill_model=self.config["forward_fill_model"] if strict_depth else self.config["historical_fill_model"]))
            self.depth_used[depth_key] = self.depth_used.get(depth_key, 0) + quantity
            order["remaining_qty"] -= quantity
            order.update(gross_filled=str(total_gross), fees_paid=str(total_fee), tax_paid=str(total_tax))
            order["status"] = "FILLED" if order["remaining_qty"] == 0 else "PARTIALLY_FILLED"
            remaining_gross = limit * order["remaining_qty"]
            if self.config.get('funding_mode')=='LATEST_BRANCH_CUMULATIVE':
                order['reserve']=str(self.funding_required(limit,order['remaining_qty'],instrument)) if side=='BUY' and order['remaining_qty'] else '0'
            else:
                order["reserve"] = str(remaining_gross + max(Decimal(0), cost_model.fee(total_gross + remaining_gross) - total_fee)) if side == "BUY" and order["remaining_qty"] else "0"
            if order["remaining_qty"] == 0:
                self.pending.remove(order)
        for symbol, p in self.positions.items():
            q = qmap.get(symbol)
            if q and q.get("bid") and q.get("quote_status") == "LIVE" and not q.get("is_trial"):
                p.update(mark=str(q["bid"]), mark_at=at)
        if self.cash < 0 or self.reserved() > self.cash:
            raise AssertionError("NEGATIVE_AVAILABLE_CASH")
        self.mark(at)

    def mark(self, at):
        day = instant(at).astimezone(Market().tz).date().isoformat()
        value = self.cash + sum((decimal(x["amount"]) for x in self.receivables), Decimal(0))
        for symbol, p in self.positions.items():
            px = fill_price(p["mark"], "SELL", day, self.config, p["asset_type"])
            value += costs(self.config, p["asset_type"]).amount(px, self.quantity(symbol), "SELL")
        row = dict(at=at, cash_twd=float(self.cash), unsettled_twd=float(sum((decimal(x["amount"]) for x in self.receivables), Decimal(0))),
                   reserved_twd=float(self.reserved()), liquidation_equity_twd=float(value), positions=len(self.positions))
        self.equity.append(row)
        self.performance_peak=max(self.performance_peak,float(value))
        self.performance_drawdown=max(self.performance_drawdown,(self.performance_peak-float(value))/self.performance_peak)
        self.daily_closes[day]=float(value)
        return row

    def submit(self, decisions, ready_at, cycle_id):
        day = instant(ready_at).astimezone(Market().tz).date().isoformat()
        for d in decisions:
            if d["decision"] not in ("BUY", "SELL"):
                continue
            side, symbol, qty = d["decision"], d["symbol"], d["suggested_qty"]
            record = dict(id=digest([cycle_id, symbol, side])[:24], symbol=symbol, side=side, qty=qty,
                          remaining_qty=qty, day=day, ready_at=ready_at, limit=d["suggested_price"],
                          asset_type=d.get("asset_type", "STOCK"), status="REJECTED", plan={k:d.get(k) for k in
                          ("strategy_id", "target_price", "invalidation_price", "holding_days", "reason")}, reserve="0")
            reason = None
            if type(qty) is not int or not 1 <= qty <= self.config["max_order_quantity"] or record["limit"] is None:
                reason = "INVALID_ORDER"
            elif round_price(record["limit"], day, record["asset_type"]) != decimal(record["limit"]):
                reason = "OFF_TICK_LIMIT"
            elif any(x["symbol"] == symbol for x in self.pending):
                reason = "PENDING_ORDER_EXISTS"
            elif side == "SELL" and qty > self.sellable(symbol, day):
                reason = "NO_SAME_DAY_ODDLOT_RESALE"
            elif side == "BUY":
                if decimal(record['limit'])*qty>decimal(self.config.get('max_order_twd','1e15')):
                    reason='MAX_ORDER_VALUE'
                elif symbol not in self.positions and len(set(self.positions) | {x["symbol"] for x in self.pending if x["side"] == "BUY"}) >= self.config["max_positions"]:
                    reason = "MAX_POSITIONS"
                else:
                    reserved = self.funding_required(record['limit'],qty,record['asset_type'])
                    if reserved > self.available(day):
                        reason = "CASH_OR_DAILY_CAP"
                    else:
                        record["reserve"] = str(reserved)
                        record['funding_required']=str(reserved)
            if reason:
                record["reason"] = reason
            else:
                record["status"] = "PENDING"
                self.pending.append(record)
            self.orders.append(record)

    def metrics(self):
        initial = decimal(self.config["initial_cash_twd"])
        pnl = [x["net_pnl_twd"] for x in self.closed_trades]
        gains, losses = sum(max(0, x) for x in pnl), -sum(min(0, x) for x in pnl)
        peak, dd = float(initial), self.performance_drawdown
        daily = dict(self.daily_closes)
        for row in self.equity:
            value = row["liquidation_equity_twd"]
            peak = max(peak, value)
            dd = max(dd, (peak - value) / peak)
            daily[row["at"][:10]] = value
        prev, returns = float(initial), []
        for _, value in sorted(daily.items()):
            returns.append(value / prev - 1)
            prev = value
        vol = statistics.stdev(returns) if len(returns) > 1 else 0
        return dict(net_return=prev / float(initial) - 1, closed_trades=len(pnl), closed_fills=sum(x["side"]=="SELL" for x in self.fills), buy_fills=sum(x["side"] == "BUY" for x in self.fills),
                    win_rate=sum(x > 0 for x in pnl) / len(pnl) if pnl else None,
                    expectancy_twd=statistics.fmean(pnl) if pnl else None, profit_factor=gains / losses if losses else None,
                    profit_factor_infinite=bool(gains and not losses), maximum_drawdown=dd,
                    sharpe_ratio=statistics.fmean(returns) / vol * 252 ** .5 if vol else None,
                    open_positions={s:self.quantity(s) for s in self.positions}, pending_orders=len(self.pending),
                    probability_calibration=None, probability_calibration_reason="LLM_CONFIDENCE_IS_NOT_A_TRADE_WIN_PROBABILITY")
