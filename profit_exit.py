"""Point-in-time exit evidence and a prepared, unsubmitted profit-taking order."""
from datetime import datetime, timedelta
from decimal import Decimal
import json

from config import CODE_ROOT, TAIPEI, now
from trade_economics import estimate_sell


def policy():
    p = json.loads((CODE_ROOT/'config/profit_exit.json').read_text('utf-8'))
    if (type(p['enabled']) is not bool or not 0 < p['minimum_net_profit_twd'] <= 50
            or not .001 <= p['profit_giveback_fraction'] <= .03
            or not 60 <= p['minimum_observation_seconds'] <= p['maximum_observation_seconds'] <= 3600):
        raise ValueError('INVALID_PROFIT_EXIT_POLICY')
    return p


def tick_below(price, asset_type):
    """Find the immediately preceding legal price, including tick-band changes."""
    from execution.planned_entry import validate_price
    price = Decimal(str(price)) - Decimal('.01')
    for _ in range(1000):
        try:
            return validate_price(price, asset_type)
        except ValueError:
            price -= Decimal('.01')
    raise ValueError('NO_LEGAL_EXIT_TICK')


def prepare(candidate, setup, rules):
    """Attach to the BUY thesis; a prepared exit is never a broker order."""
    p = policy()
    limit = tick_below(setup['target'], candidate['asset_type'])
    return dict(version=p['version'], prepared_at=candidate['quote_timestamp'],
        target_reference=setup['target'], limit_price=float(limit),
        invalidation_price=setup['invalidation'], holding_days=setup['holding_days'],
        minimum_net_profit_twd=p['minimum_net_profit_twd'],
        activation='AFTER_CONFIRMED_FILL_AND_BROKER_SELLABLE',
        time_in_force='ROD', quantity='CONFIRMED_AI_SELLABLE_ONLY',
        review_before_send=True, broker_order_sent=False,
        early_exit='POSITIVE_NET_PROFIT_AND_OBSERVED_REVERSAL',
        note='零股買入當日不賣；預備退出價不代表已掛單，也不保證成交。')


def refresh(candidate, rules, *, at=None):
    """Read-only evidence. A model and existing executor still approve every trade."""
    p = policy()
    result = dict(version=p['version'], status='NOT_HELD', triggers=[],
                  can_sell=False, resting_order=None)
    candidate['exit_context'] = result
    held = int((candidate.get('paper_position') or {}).get('qty') or 0)
    if not p['enabled'] or held <= 0:
        return result
    at = at or now()
    try:
        stamp = datetime.fromisoformat(candidate['quote_timestamp'])
        if (stamp.tzinfo is None or at.tzinfo is None or not 0 <= (at-stamp).total_seconds() <= 120
                or candidate.get('quote_status') != 'LIVE' or candidate.get('is_trial') is not False):
            raise ValueError('FRESH_LIVE_NON_TRIAL_QUOTE_REQUIRED')
        bid = Decimal(str(candidate['bid']))
        ask = Decimal(str(candidate['ask'])) if candidate.get('ask') is not None else bid
        if not bid.is_finite() or not ask.is_finite() or not 0 < bid <= ask:
            raise ValueError('VALID_BOOK_REQUIRED')
        if candidate.get('ask') is None:
            result['book_limitation']='ASK_UNAVAILABLE_SELL_ONLY'
        qty = min(held, max(0, int(candidate.get('max_sell_qty') or 0)))
        result.update(status='OBSERVING', can_sell=qty > 0 and not candidate.get('pending_execution'),
                      sellable_qty=qty, observation_timestamp=stamp.isoformat())
        if qty == 0:
            result['status'] = 'WAIT_FOR_BROKER_SELLABLE'
        if candidate.get('pending_execution'):
            result['status'] = 'PENDING_ORDER_REQUIRES_RECONCILIATION'
        economics = estimate_sell(candidate, qty or held,
                                 minimum_commission_twd=rules.economic_minimum_commission_twd)
        result['economics'] = economics
        pnl = economics['estimated_realized_pnl_twd']
        profitable = pnl is not None and pnl >= p['minimum_net_profit_twd']
        lots = (candidate.get('buy_plans') or {}).get('lots', [])
        targets = []
        for lot in lots:
            plan = lot.get('plan') or {}
            stop, target = plan.get('invalidation_price'), plan.get('target_price')
            if stop is not None and bid <= Decimal(str(stop)):
                result['triggers'].append('ORIGINAL_INVALIDATION_TOUCHED')
            if target is not None:
                targets.append(Decimal(str(target)))
                if bid >= Decimal(str(target)):
                    result['triggers'].append('ORIGINAL_TARGET_REACHED')
        # Consecutive actual observations, not a fabricated intraday candle.
        observations = {}
        for row in candidate.get('market_history') or []:
            try:
                t = datetime.fromisoformat(row['exchange_time'])
                price = Decimal(str(row['price']))
                if (row.get('symbol') == candidate['symbol'] and row.get('quote_status') == 'LIVE'
                        and row.get('is_trial') is False and row.get('retrieval_mode') != 'CACHE'
                        and t.tzinfo is not None and t.astimezone(TAIPEI).date() == stamp.astimezone(TAIPEI).date()
                        and 0 < (stamp-t).total_seconds() <= 900 and price.is_finite() and price > 0):
                    observations[t] = price
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
        observations[stamp] = Decimal(str(candidate['last_price']))
        sequence = sorted(observations.items())
        recent = sequence[-3:]
        if (profitable and len(recent) == 3
                and all(60 <= (b[0]-a[0]).total_seconds() <= 300 for a,b in zip(recent,recent[1:]))
                and recent[0][1] > recent[1][1] > recent[2][1]):
            peak = max(price for _,price in sequence)
            drawdown = peak - recent[-1][1]
            tick = bid - tick_below(bid,candidate['asset_type'])
            threshold = max(peak*Decimal(str(p['profit_giveback_fraction'])), 2*tick, ask-bid)
            result['reversal_evidence'] = dict(observations=[dict(at=t.isoformat(),price=float(v)) for t,v in recent],
                observed_peak=float(peak), drawdown=float(drawdown), threshold=float(threshold),
                source='DISTINCT_LIVE_QUOTES_NOT_INTRADAY_CANDLES')
            if drawdown >= threshold:
                result['triggers'].append('POSITIVE_NET_PROFIT_WITH_CONFIRMED_PULLBACK')
        # Draft a near-resistance take-profit on remaining legal AI shares.
        if targets and result['can_sell']:
            limit = tick_below(min(targets), candidate['asset_type'])
            draft_economics = estimate_sell({**candidate,'bid':float(limit)},qty,
                minimum_commission_twd=rules.economic_minimum_commission_twd)
            if (draft_economics['estimated_realized_pnl_twd'] is not None
                    and draft_economics['estimated_realized_pnl_twd'] >= p['minimum_net_profit_twd']):
                if bid >= limit and profitable:
                    result['triggers'].append('POSITIVE_NET_PROFIT_NEAR_ORIGINAL_TARGET')
                elif bid < limit and not result['triggers'] and candidate.get('pricing_policy')=='PLANNED_LIMIT':
                    result['resting_order'] = dict(limit_price=float(limit), quantity=qty,
                        minimum_net_profit_twd=p['minimum_net_profit_twd'], economics=draft_economics,
                        target_reference=float(min(targets)), time_in_force='ROD',
                        requires_review=True, broker_order_sent=False)
        result['triggers'] = sorted(set(result['triggers']))
        result['urgent_review'] = bool(result['can_sell'] and result['triggers'])
    except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
        result.update(status=str(exc), can_sell=False, urgent_review=False, resting_order=None)
    return result
