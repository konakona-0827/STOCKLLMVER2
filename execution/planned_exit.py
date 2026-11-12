"""Reviewed ROD profit-taking SELL; existing ownership and pending guards apply."""
from datetime import datetime
from decimal import Decimal

from config import TAIPEI
from .planned_entry import validate_price
from .risk_guard import RiskRejected


def matches(candidate, decision):
    draft=(candidate.get('exit_context') or {}).get('resting_order') or {}
    return bool(decision.get('decision')=='SELL' and candidate.get('pricing_policy')=='PLANNED_LIMIT'
        and not candidate.get('pending_execution') and draft
        and decision.get('suggested_price')==draft.get('limit_price')
        and 0 < (decision.get('suggested_qty') or 0) <= draft.get('quantity',0))


def contract(candidate, decision, generated_at):
    if not matches(candidate,decision):
        raise RiskRejected('SELL_MUST_MATCH_PREPARED_EXIT')
    stamp=datetime.fromisoformat(candidate['quote_timestamp']).astimezone(TAIPEI)
    stops=[(lot.get('plan') or {}).get('invalidation_price')
           for lot in (candidate.get('buy_plans') or {}).get('lots',[])]
    return dict(version='formal-planned-exit-v1',created_at=generated_at.isoformat(),
        expires_at=stamp.replace(hour=13,minute=30,second=0,microsecond=0).isoformat(),
        limit_price=decision['suggested_price'],quantity=decision['suggested_qty'],
        invalidation_price=max((x for x in stops if x is not None),default=None),
        target_reference=candidate['exit_context']['resting_order']['target_reference'],
        minimum_net_profit_twd=candidate['exit_context']['resting_order']['minimum_net_profit_twd'],
        time_in_force='ROD',replacement='VERIFY_BROKER_CANCEL_AND_FILL_BEFORE_REPLACEMENT')


def verify(intent, recommendation, quote, at, minimum_commission_twd):
    from market_calendar import twse_closed_reason
    from trade_economics import estimate_sell
    p=recommendation.get('exit_plan') or {}
    try:
        created,expires=(datetime.fromisoformat(p[k]) for k in ('created_at','expires_at'))
        local=at.astimezone(TAIPEI)
        if (intent.side.value!='SELL' or created.tzinfo is None or expires.tzinfo is None
                or not created<=at<expires or expires.astimezone(TAIPEI).date()!=local.date()
                or expires.astimezone(TAIPEI).strftime('%H:%M:%S')!='13:30:00'
                or twse_closed_reason(local.date()) or not '09:00:00'<=local.strftime('%H:%M:%S')<'13:30:00'):
            raise RiskRejected('PLANNED_EXIT_EXPIRED_OR_OUTSIDE_SESSION')
        limit=validate_price(p['limit_price'],recommendation['asset_type'])
        if limit!=intent.suggested_price or intent.suggested_qty!=p['quantity']:
            raise RiskRejected('PLANNED_EXIT_PRICE_OR_QUANTITY_CHANGED')
        if (quote.bid is None or not quote.bid.is_finite() or quote.bid<=0
                or (p['invalidation_price'] is not None and quote.bid<=Decimal(str(p['invalidation_price'])))):
            raise RiskRejected('PLANNED_EXIT_REQUIRES_IMMEDIATE_RISK_REVIEW')
        qty=int(recommendation['ai_managed_qty'])
        estimated=estimate_sell(dict(bid=float(limit),asset_type=recommendation['asset_type'],
            ai_cost_basis=recommendation.get('existing_cost_basis'),
            buy_plans=recommendation.get('existing_buy_plans'),paper_position=dict(qty=qty)),
            intent.suggested_qty,minimum_commission_twd=minimum_commission_twd)
        if estimated['estimated_realized_pnl_twd'] is None or estimated['estimated_realized_pnl_twd']<max(1,p['minimum_net_profit_twd']):
            raise RiskRejected('PLANNED_EXIT_NOT_NET_PROFITABLE')
    except (KeyError,ValueError,TypeError,ArithmeticError) as exc:
        raise RiskRejected(f'INVALID_PLANNED_EXIT: {exc}') from exc
