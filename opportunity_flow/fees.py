"""Separate published Capital electronic odd-lot fees from a cash funding reserve."""
from datetime import datetime
from decimal import Decimal
import hashlib
from pathlib import Path

from .common import TAIPEI,read,save

SOURCE='https://www.capital.com.tw/event/stock/oddLot/default.asp'
PUBLISHED_MINIMUM=Decimal('1')
PUBLISHED_RATE=Decimal('0.001425')


def fee_status(root):
    cfg=read(Path(root)/'config/execution_live.json')
    explicit=cfg.get('minimum_commission_twd')
    return dict(minimum_commission_twd=explicit if explicit is not None else cfg.get('min_buy_fee_reserve_twd',20),
        cash_reserve_twd=cfg.get('min_buy_fee_reserve_twd',20),commission_rate=str(PUBLISHED_RATE),
        source_url=cfg.get('commission_assumption_source'),
        basis='EXPLICIT_CONFIG' if explicit is not None else 'LEGACY_RESERVE_USED_AS_COMMISSION',
        account_discount_verified=cfg.get('commission_account_verified') is True,
        limitation='官方未折扣電子盤中零股費率；個別帳戶折扣及實收尚需對帳單核對。')


def correct_legacy_capital_fee(root,audit_folder):
    """Only fill an absent fee field; preserve explicit contracts and all risk limits.

    Own the same broker lease while changing shared configuration. A running
    account process must never see an unaudited partial or competing edit.
    """
    from state_lock import StateLease
    root=Path(root);path=root/'config/execution_live.json'
    with StateLease(root,'broker-runtime'):
        before=path.read_bytes();cfg=read(path)
        if cfg.get('minimum_commission_twd') is not None:
            return dict(status='EXPLICIT_FEE_PRESERVED',changed=False,fee=fee_status(root))
        updated={**cfg,'minimum_commission_twd':1,'commission_assumption_source':SOURCE,
            'commission_account_verified':False,
            'commission_assumption_note':'群益官方電子盤中零股：0.1425%，最低1元；未套個別折扣。20元僅資金預留，風控門檻不變。',
            'commission_reviewed_at':datetime.now(TAIPEI).isoformat()}
        audit=Path(audit_folder);audit.mkdir(parents=True,exist_ok=True)
        (audit/'execution_live.before.json').write_bytes(before)
        if path.read_bytes()!=before:raise RuntimeError('FEE_CONFIG_CHANGED_DURING_REVIEW')
        save(path,updated)
        report=dict(status='CORRECTED_TO_PUBLISHED_CAPITAL_ODDLOT_FEE',changed=True,
            before_sha256=hashlib.sha256(before).hexdigest(),after_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            changed_fields=sorted(k for k in updated if updated.get(k)!=cfg.get(k)),
            fee=fee_status(root),risk_thresholds_changed=False,orders_sent=0)
        save(audit/'review.json',report)
        return report
