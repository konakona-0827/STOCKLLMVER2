"""Ownership tags for user-originated diagnostic orders in the shared ledger."""
MANUAL_DIAGNOSTIC='MANUAL_DIAGNOSTIC'
RECORD_ONLY='RECORD_ONLY_NO_AI_ACTION'


def is_manual_diagnostic(detail):
    return isinstance(detail,dict) and detail.get('ownership')==MANUAL_DIAGNOSTIC
