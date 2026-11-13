"""Inspect real SKCOM order structures locally. No login, order or cancellation.

Uses the same structure builder as CapitalOddLotExecutor.send_limit_order.
The account is deliberately non-submittable. A successful inspection proves
native field compatibility only, not account/risk approval or broker acceptance.
"""
import argparse
import ctypes
from datetime import datetime
from pathlib import Path

from execution.capital_executor import prepare_limit_order
from execution.capital_reply_session import find_skcom_dll
from opportunity_flow.common import TAIPEI, save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--symbol', default='0050')
    parser.add_argument('--qty', type=int, default=1)
    parser.add_argument('--price', required=True, help='field test value, not a live executable quote')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    import comtypes.client
    dll = find_skcom_dll(args.project_root)
    comtypes.client.GetModule(str(dll))
    import comtypes.gen.SKCOMLib as sk
    fields = ('bstrStockNo', 'sPeriod', 'sFlag', 'sBuySell', 'bstrPrice', 'nQty',
              'sPrime', 'nTradeType', 'nSpecialTradeType')
    orders = []
    for side in ('BUY', 'SELL'):
        order = prepare_limit_order(sk, 'STRUCTURE_CHECK_ONLY', symbol=args.symbol,
                                    side=side, quantity=args.qty, price=args.price)
        orders.append(dict(side=side, fields={k: getattr(order, k) for k in fields if hasattr(order, k)}))
    result = dict(checked_at=datetime.now(TAIPEI).isoformat(), status='NATIVE_STRUCTURE_COMPATIBLE',
                  sdk_file=dll.name, sdk_structure_bytes=ctypes.sizeof(sk.STOCKORDER),
                  account='NON_SUBMITTABLE_PLACEHOLDER', login_attempts=0, submission_calls=0,
                  price_is_field_test_value=True,
                  broker_acceptance_verified=False, positive_live_execution_verified=False, orders=orders)
    save(args.output, result)
    import json
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
