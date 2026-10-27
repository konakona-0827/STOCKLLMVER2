"""Conservative evidence from one-account balance changes around settlement."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation


def _amount(value):
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() else None
    except (TypeError, ValueError, InvalidOperation):
        return None


def _stamp(value):
    try:
        stamp = datetime.fromisoformat(str(value))
        return stamp if stamp.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def assess_settlement_balance(previous: dict, current: dict) -> dict:
    """Compare due-day net amounts with balance movements, without confirming payment.

    A matching change can also be caused by unrelated cash activity. The result
    is evidence for the UI, never an authoritative settlement status.
    """
    old_stamp = _stamp(previous.get('checked_at'))
    new_stamp = _stamp(current.get('checked_at'))
    old_balance = _amount(previous.get('broker_one_account_balance_twd'))
    new_balance = _amount(current.get('broker_one_account_balance_twd'))
    earlier = previous.get('settlement_balance_evidence') or {}
    if current.get('settlement_query_error') or not current.get('broker_settlement_dues'):
        return dict(earlier)
    nets = {}
    for row in current.get('broker_settlement_dues') or []:
        try:
            due_day = date.fromisoformat(str(row['settlement_date']))
            amount = _amount(row['net_twd'])
            if amount is None:
                return dict(earlier)
        except (KeyError, TypeError, ValueError):
            return dict(earlier)
        key = due_day.isoformat()
        nets[key] = nets.get(key, Decimal('0')) + amount
    evidence = {}
    for key, expected in nets.items():
        if expected == 0:
            continue
        due_day = date.fromisoformat(key)
        prior = earlier.get(key) or {}
        if _amount(prior.get('expected_net_twd')) != expected:
            prior = {}
        baseline = _amount(prior.get('baseline_balance_twd'))
        baseline_at = _stamp(prior.get('baseline_at'))
        if (new_stamp is not None and new_balance is not None
                and new_stamp.date() < due_day):
            baseline = new_balance
            baseline_at = new_stamp
        elif (baseline is None and old_stamp is not None and old_balance is not None
              and old_stamp.date() < due_day):
            baseline = old_balance
            baseline_at = old_stamp
        item = {'expected_net_twd': str(expected),
                'status': 'NO_BASELINE'}
        if baseline is not None and baseline_at is not None:
            item['baseline_balance_twd'] = str(baseline)
            item['baseline_at'] = baseline_at.isoformat()
        if prior.get('status') == 'BALANCE_MATCH' and baseline is not None:
            item.update(status='BALANCE_MATCH',
                        observed_delta_twd=prior.get('observed_delta_twd'),
                        observed_at=prior.get('observed_at'),
                        observed_balance_twd=prior.get('observed_balance_twd'))
        elif (new_stamp is not None and new_balance is not None
              and baseline is not None and baseline_at is not None
              and baseline_at < new_stamp and new_stamp.date() >= due_day):
            intervening = [other for other, net in nets.items()
                           if net != 0 and baseline_at.date() < date.fromisoformat(other)
                           <= new_stamp.date()]
            if len(intervening) == 1:
                delta = new_balance - baseline
                item.update(status=('BALANCE_MATCH' if delta == expected
                                    else 'BALANCE_DIFFERENT'),
                            observed_delta_twd=str(delta),
                            observed_at=new_stamp.isoformat(),
                            observed_balance_twd=str(new_balance))
            else:
                item['status'] = 'MULTIPLE_DUE_DATES'
        evidence[key] = item
    return evidence
