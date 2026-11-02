"""Planned BUY prices on the EXISTING formal ledger, budget and broker path."""
from datetime import datetime
from decimal import Decimal
from config import TAIPEI
from .risk_guard import RiskRejected

POLICY = 'PLANNED_LIMIT'
VERSION = 'formal-planned-entry-v1'
INSTRUCTIONS = '''
正式帳戶的計畫限價買進模式已啟用。BUY 的 suggested_price 是你願意買進的最高委託價格，
請依走勢、price_levels、交易成本與風險提出具體價格和理由；不必因現價尚未回落就一律 WAIT。
價格須符合股票或 ETF 的合法升降單位。既有成本後淨報酬、風險報酬比、預算、持倉歸屬仍不可覆寫。
程式保留此價格送出當日有效限價委託，不會改成重新查價後的 ask，也不會為未成交自動追價。
接受委託不是成交；只有券商確認的實際成交股數才增加持倉。已有未結委託的標的不重複下單。
SELL 仍按原有持倉／可賣量及退出風控執行。觸及失效價不是已送出的停損單，不能保證退出。
'''


def validate_price(price, asset_type):
    price = Decimal(str(price))
    if not price.is_finite() or price <= 0:
        raise RiskRejected('INVALID_PLANNED_PRICE')
    if asset_type == 'ETF':
        tick = Decimal('.01') if price < 50 else Decimal('.05')
    elif asset_type == 'STOCK':
        tick = Decimal(5)
        for ceiling, step in ((10, '.01'), (50, '.05'), (100, '.1'), (500, '.5'), (1000, '1')):
            if price < ceiling:
                tick = Decimal(step)
                break
    else:
        raise RiskRejected('UNSUPPORTED_PLANNED_ASSET_TYPE')
    if price % tick:
        raise RiskRejected('INVALID_PLANNED_TICK')
    return price


def contract(candidate, decision, generated_at):
    stamp = datetime.fromisoformat(candidate['quote_timestamp'])
    if stamp.tzinfo is None:
        raise RiskRejected('PLANNED_QUOTE_TIMEZONE_REQUIRED')
    expiry = stamp.astimezone(TAIPEI).replace(hour=13, minute=30, second=0, microsecond=0)
    return dict(version=VERSION, created_at=generated_at.isoformat(), expires_at=expiry.isoformat(),
                limit_price=decision['suggested_price'], target_price=decision['target_price'],
                invalidation_price=decision['invalidation_price'], reason=decision['reason'],
                time_in_force='ROD', repricing='NEVER_AUTOMATIC', invalidation='PRE_SEND; PENDING_REQUIRES_BROKER_CANCEL')


def verify(intent, recommendation, quote, at):
    """Fail closed immediately before a planned send; never change its price."""
    if intent.pricing_policy != POLICY:
        return
    if intent.side.value != 'BUY':
        raise RiskRejected('FORMAL_PLANNED_MODE_SUPPORTS_BUY_ONLY')
    p = recommendation.get('entry_plan') or {}
    try:
        stamp, expires = datetime.fromisoformat(p['created_at']), datetime.fromisoformat(p['expires_at'])
        local = at.astimezone(TAIPEI)
        from market_calendar import twse_closed_reason
        if (stamp.tzinfo is None or expires.tzinfo is None or not stamp <= at < expires
                or expires.astimezone(TAIPEI).date() != local.date()
                or expires.astimezone(TAIPEI).strftime('%H:%M:%S') != '13:30:00'
                or twse_closed_reason(local.date()) or not '09:00:00' <= local.strftime('%H:%M:%S') < '13:30:00'):
            raise RiskRejected('PLANNED_ENTRY_EXPIRED_OR_OUTSIDE_SESSION')
        limit = validate_price(p['limit_price'], recommendation['asset_type'])
        if limit != intent.suggested_price:
            raise RiskRejected('PLANNED_ENTRY_PRICE_CHANGED')
        target, stop = Decimal(str(p['target_price'])), Decimal(str(p['invalidation_price']))
        if not target.is_finite() or not stop.is_finite() or not 0 < stop < limit < target:
            raise RiskRejected('INVALID_PLANNED_BRACKET')
        buy = recommendation.get('buy_plan') or {}
        if target != Decimal(str(buy['target_price'])) or stop != Decimal(str(buy['invalidation_price'])):
            raise RiskRejected('PLANNED_BRACKET_CHANGED')
        if quote.last_price is None or not quote.last_price.is_finite() or quote.last_price <= stop:
            raise RiskRejected('PLANNED_ENTRY_ALREADY_INVALIDATED')
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise RiskRejected(f'INVALID_PLANNED_ENTRY_CONTRACT: {exc}') from exc
