"""Fixed user-confirmed diagnostic contract; never a strategy recommendation."""
from datetime import datetime,timedelta
from decimal import Decimal

from .models import Side
from .risk_guard import RiskRejected

POLICY = 'USER_HEALTH_LIMIT'
SOURCE = 'USER_CONFIRMED_HEALTH_ASSUMPTION'


def trading_date(at):
    from config import TAIPEI
    from market_calendar import twse_closed_reason
    at=at.astimezone(TAIPEI);day=at.date()
    if at.strftime('%H:%M:%S')>='13:30:00':day+=timedelta(days=1)
    while twse_closed_reason(day):day+=timedelta(days=1)
    if day.year!=2026:raise RiskRejected('PREORDER_TRADING_CALENDAR_NOT_VERIFIED')
    return day.isoformat()


def validate(payload, intents, at):
    from config import TAIPEI
    local = at.astimezone(TAIPEI)
    rows = payload.get('recommendations') or []
    if len(rows) != 1 or len(intents) != 1:
        raise RiskRejected('HEALTH_TEST_REQUIRES_EXACTLY_ONE_ORDER')
    row, intent = rows[0], intents[0]
    if (payload.get('health_order_test') != dict(symbol='0050',side='BUY',quantity=1,limit_price='80',
            time_in_force='ROD',user_confirmed=True)
            or row.get('analysis_source') != SOURCE or row.get('pricing_policy') != POLICY
            or type(row.get('suggested_qty')) is not int or row['suggested_qty'] != 1
            or intent.symbol != '0050' or intent.side != Side.BUY or intent.suggested_price != Decimal(80)
            or intent.suggested_qty != 1 or intent.decision_id != 'health-0050-1-80-'+local.date().isoformat()
            or payload.get('valid_trading_date') != trading_date(at)
            or type(payload.get('requested_outside_session')) is not bool
            or payload.get('pretrade_review_status') is not None):
        raise RiskRejected('HEALTH_TEST_CONTRACT_CHANGED')
    try:
        created = datetime.fromisoformat(payload['generated_at'])
        if created.tzinfo is None or not 0 <= (at-created).total_seconds() <= 120:
            raise ValueError('stale diagnostic')
    except (KeyError,TypeError,ValueError) as exc:
        raise RiskRejected('HEALTH_TEST_EXPIRED') from exc
