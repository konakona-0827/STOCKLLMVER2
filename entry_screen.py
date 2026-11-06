"""Numerical scenarios for the decision agent; never signals or order approval."""
from decimal import Decimal, ROUND_FLOOR
from datetime import datetime

from pretrade_policy import MIN_NET_RETURN, MIN_REWARD_RISK, MAX_POSITION_RISK, buy_block
from trade_economics import evaluate_buy


def entry_screen(candidate, account, rules):
    """Compare market and observed pullback levels under unchanged risk limits.

    The search is intentionally finite, not an exhaustive proof that no trade
    exists. Nearest visible resistance cannot be skipped for a better ratio.
    """
    if candidate.get('opportunity_policy'):
        from copy import deepcopy
        from opportunity_engine import refresh
        candidate = deepcopy(candidate)
        refresh(candidate, account, rules)
        return dict(status='SCENARIOS_AVAILABLE' if candidate['trade_setups'] else 'NO_SCENARIO_PASSED',
                    scenarios=[{key:s[key] for key in ('setup_id','tier','entry','max_qty')} for s in candidate['trade_setups']],
                    blockers=candidate['opportunity_status']['blockers'],
                    scope='ACCOUNT_SIZED_SIGNAL_AND_STRUCTURE_SCENARIOS_REQUIRING_INDEPENDENT_REVIEW')
    result = dict(status='NOT_EVALUATED', scenarios=[], blockers=[],
                  scope='NUMERICAL_SCENARIOS_ONLY_NOT_A_SIGNAL_OR_EXHAUSTIVE_SEARCH')
    try:
        if candidate.get('quote_status') != 'LIVE' or candidate.get('is_trial'):
            result['blockers'] = ['LIVE_NON_TRIAL_QUOTE_REQUIRED']
            return result
        if candidate.get('asset_type') not in ('STOCK', 'ETF'):
            result['blockers'] = ['ASSET_TAX_TYPE_UNKNOWN']
            return result
        blocked = buy_block(candidate)
        if blocked:
            result['blockers'] = [blocked]
            return result
        if not candidate.get('eligible_strategies'):
            result['blockers'] = ['NO_ELIGIBLE_STRATEGY']
            return result
        budget = Decimal(account['available_cash_cents']) / 100
        capital = Decimal(str(account['capital_limit_twd']))
        ask, bid = Decimal(str(candidate['ask'])), Decimal(str(candidate['bid']))
        if not 0 < bid <= ask or budget <= 0 or capital <= 0:
            raise ValueError('INVALID_BUDGET_OR_BOOK')
        levels = candidate.get('price_levels') or []
        prices = sorted({Decimal(str(row['price'])) for row in levels if row['price'] > 0})
        entries = [(ask, 'CURRENT_ASK')]
        planned = candidate.get('pricing_policy') == 'PLANNED_LIMIT'
        if planned:
            from execution.planned_entry import validate_price
            entries += [(Decimal(str(row['price'])), row['id']) for row in levels
                        if row['id'].startswith('low_') or row['id'] in ('previous_close', 'today_open', 'today_low')]
            entries = [(price, source) for price, source in entries
                       if 0 < price <= ask and validate_price(price, candidate['asset_type'])]
        session = datetime.fromisoformat(candidate['quote_timestamp']).date().isoformat()
        # Raw completed daily highs include nearby barriers not present in the
        # 5/10/20-day extrema. They are barriers, not invented legal targets.
        barriers = set(prices)
        for bar in candidate.get('daily_bars') or []:
            if (bar.get('complete') is True and bar.get('source') in ('TWSE_DAILY', 'BROKER_DAILY')
                    and bar.get('date', '') < session and bar.get('high', 0) > 0):
                barriers.add(Decimal(str(bar['high'])))
        minimum = rules.economic_minimum_commission_twd
        found = []
        counts = {}
        for entry, source in dict(entries).items():
            target = min((p for p in barriers if p > entry), default=None)
            if target is None or target not in prices:
                code = 'NO_SUPPORTED_TARGET_BEFORE_NEAREST_RESISTANCE'
                counts[code] = counts.get(code, 0) + 1
                continue
            stops = [p for p in prices if p < entry and p < Decimal(str(candidate['last_price']))]
            for tier, fraction in (('Early', '.15'), ('Confirmed', '.25'), ('Strong', '.30')):
                allocation = budget * Decimal(fraction)
                max_qty = min(999, int(candidate.get('max_buy_qty') or 0),
                              int(allocation / entry), int(rules.max_buy_qty or 999))
                best = None
                for stop in stops:
                    for qty in range(max_qty, 0, -1):
                        gross = entry * qty
                        reserve = max(rules.min_buy_fee_reserve_twd, gross * rules.buy_cash_buffer_rate,
                                      (gross * Decimal('.001425')).to_integral_value(rounding=ROUND_FLOOR))
                        if gross + reserve > allocation or (rules.max_order_twd is not None and gross > rules.max_order_twd):
                            continue
                        econ = evaluate_buy(candidate, qty=qty, limit_price=float(entry),
                            target_price=float(target), invalidation_price=float(stop),
                            minimum_commission_twd=minimum)
                        if Decimal(str(econ['estimated_net_downside_twd'])) > capital * MAX_POSITION_RISK:
                            continue
                        net_return = Decimal(str(econ['estimated_net_upside_twd'])) / Decimal(str(econ['estimated_buy_cost_twd']))
                        if net_return < MIN_NET_RETURN or (econ['reward_risk_ratio'] or 0) < MIN_REWARD_RISK:
                            break  # Smaller size cannot amortize fixed costs better.
                        row = dict(tier=tier, entry_source=source, entry=float(entry),
                            target=float(target), invalidation=float(stop), qty=qty,
                            budget_with_reserve_twd=float(gross + reserve),
                            allocation_fraction=float((gross + reserve) / budget),
                            net_return=float(net_return), **econ)
                        if best is None or row['estimated_net_upside_twd'] > best['estimated_net_upside_twd']:
                            best = row
                        break
                if best:
                    found.append(best)
                else:
                    counts['SIZE_COST_OR_RISK_LIMIT'] = counts.get('SIZE_COST_OR_RISK_LIMIT', 0) + 1
        # Keep a market scenario and the best pullback scenario, with each tier
        # retained. Signal strength still has to justify the chosen tier/stop.
        for tier in ('Early', 'Confirmed', 'Strong'):
            rows = [r for r in found if r['tier'] == tier]
            for market in (True, False):
                matches = [r for r in rows if (r['entry_source'] == 'CURRENT_ASK') == market]
                if matches:
                    result['scenarios'].append(max(matches, key=lambda r: r['estimated_net_upside_twd']))
        result.update(status='SCENARIOS_AVAILABLE' if found else 'NO_SCENARIO_PASSED',
                      blockers=[] if found else sorted(counts), checked_failures=counts)
    except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
        result.update(status='UNAVAILABLE', blockers=[str(exc)])
    return result
