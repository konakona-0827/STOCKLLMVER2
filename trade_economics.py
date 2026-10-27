"""Conservative arithmetic check for a proposed BUY's net upside and downside."""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR


def evaluate_buy(candidate, *, qty, limit_price, target_price, invalidation_price,
                 minimum_commission_twd=1, commission_rate='0.001425'):
    def dec(value):
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0:
            raise ValueError('INVALID_TRADE_PLAN_PRICE')
        return number

    if type(qty) is not int or qty < 1:
        raise ValueError('INVALID_TRADE_PLAN_QTY')
    ask, bid = dec(candidate.get('ask')), dec(candidate.get('bid'))
    entry = max(ask, dec(limit_price)) if limit_price is not None else ask
    target, stop = dec(target_price), dec(invalidation_price)
    if not bid <= ask or not stop < entry < target:
        raise ValueError('INVALID_TRADE_PLAN_LEVELS')
    spread = ask - bid
    # Future executable bid is unknown. Reserve one current spread on exit.
    exit_target = target - spread
    exit_stop = stop - spread
    if exit_stop <= 0 or exit_target <= 0:
        raise ValueError('INVALID_TRADE_PLAN_EXIT')
    gross_buy = entry * qty
    gross_target = exit_target * qty
    gross_stop = exit_stop * qty
    rate = Decimal(str(commission_rate))
    minimum = Decimal(str(minimum_commission_twd))
    if not rate.is_finite() or rate < 0 or not minimum.is_finite() or minimum < 0:
        raise ValueError('INVALID_COMMISSION_ASSUMPTION')
    fee = lambda gross: max(minimum, (gross * rate).to_integral_value(rounding=ROUND_FLOOR))
    tax_rate = Decimal('0.001') if candidate.get('asset_type') == 'ETF' else Decimal('0.003')
    tax = lambda gross: (gross * tax_rate).to_integral_value(rounding=ROUND_CEILING)
    buy_fee = fee(gross_buy)
    net_upside = gross_target - fee(gross_target) - tax(gross_target) - gross_buy - buy_fee
    net_downside = gross_buy + buy_fee - (gross_stop - fee(gross_stop) - tax(gross_stop))
    return dict(entry_twd=float(entry), target_exit_after_spread_twd=float(exit_target),
                stop_exit_after_spread_twd=float(exit_stop), estimated_net_upside_twd=float(net_upside),
                estimated_net_downside_twd=float(net_downside),
                estimated_buy_cost_twd=float(gross_buy + buy_fee),
                reward_risk_ratio=float(net_upside / net_downside) if net_downside > 0 else None,
                tax_rate=float(tax_rate), commission_rate=float(rate),
                minimum_commission_twd=float(minimum), exit_spread_reserve_twd=float(spread))


def estimate_sell(candidate, qty, *, minimum_commission_twd=1,
                  commission_rate='0.001425'):
    """Estimate immediate net proceeds and cost-basis P/L; unknown basis stays unknown."""
    bid = Decimal(str(candidate.get('bid')))
    if not bid.is_finite() or bid <= 0 or type(qty) is not int or qty <= 0:
        raise ValueError('INVALID_SELL_ESTIMATE')
    gross = bid * qty
    fee = max(Decimal(str(minimum_commission_twd)),
              (gross * Decimal(str(commission_rate))).to_integral_value(rounding=ROUND_FLOOR))
    tax_rate = Decimal('0.001') if candidate.get('asset_type') == 'ETF' else Decimal('0.003')
    tax = (gross * tax_rate).to_integral_value(rounding=ROUND_CEILING)
    proceeds = gross - fee - tax
    held = int((candidate.get('paper_position') or {}).get('qty') or 0)
    basis = candidate.get('ai_cost_basis') or {}
    if basis.get('available'):
        cost = Decimal(str(basis['average_unit_cost_twd'])) * qty
        cost_basis_source = 'RECONSTRUCTED_EXCLUDES_BUY_FEES'
    else:
        cents = (candidate.get('paper_position') or {}).get('cost_cents')
        cost = Decimal(str(cents)) * qty / (100 * held) if cents and held else None
        cost_basis_source = 'PAPER_INCLUDES_BUY_FEES' if cost is not None else 'UNKNOWN'
    return dict(estimated_net_proceeds_twd=float(proceeds),
                estimated_realized_pnl_twd=float(proceeds-cost) if cost is not None else None,
                cost_basis_source=cost_basis_source,
                excludes_future_execution_slippage=True)
