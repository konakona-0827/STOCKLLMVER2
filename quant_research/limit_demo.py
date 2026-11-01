"""Reproducible SYNTHETIC demonstration; no real quotes or broker operations."""
from pathlib import Path
import argparse
import json
import tempfile
from .core import Policy
from .limit_orders import LimitOrderBook, PricePlan


def run():
    def quote(at, ask=100, bid=99.9, size=100, last=100):
        return {'2330': dict(symbol='2330', quote_status='LIVE', quote_timestamp=at,
                    bid=bid, ask=ask, bid_size=size, ask_size=size, last_price=last,
                    lot_type='INTRADAY_ODD_LOT', book_size_unit='shares')}
    with tempfile.TemporaryDirectory() as temp:
        book = LimitOrderBook(Path(temp)/'demo.sqlite3', Policy('A'))
        stages = []
        def capture(label):
            stages.append(dict(stage=label, **book.snapshot()))
        at = '2026-11-02T10:00:00+08:00'
        p = PricePlan('demo-buy', '2330', 'BUY', 50, '98.5', '103', '97', at,
                      '2026-11-02T13:30:00+08:00', 'SYNTHETIC example only', 'demo-v1', 'demo-no-model')
        book.create(p)
        book.submit(p.plan_id, at, quote(at))
        capture('accepted_at_100_resting_at_98_5')
        at = '2026-11-02T10:01:00+08:00'
        book.process('last-touch', at, quote(at, ask=99, bid=98.5, last=98.5))
        capture('last_touched_but_no_executable_offer')
        at = '2026-11-02T10:02:00+08:00'
        book.process('partial', at, quote(at, ask=98.5, bid=98.4, size=20, last=98.5))
        capture('confirmed_partial_20')
        at = '2026-11-02T10:03:00+08:00'
        book.process('complete', at, quote(at, ask=98.4, bid=98.3, size=30, last=98.4))
        capture('confirmed_total_50')
        at = '2026-11-03T10:00:00+08:00'
        sell = PricePlan('demo-sell', '2330', 'SELL', 50, '102', '103', '97', at,
                         '2026-11-03T13:30:00+08:00', 'SYNTHETIC exit', 'demo-v1', 'demo-no-model')
        book.create(sell)
        book.submit(sell.plan_id, at, quote(at, ask=102.5, bid=102))
        at = '2026-11-03T10:01:00+08:00'
        book.process('sell', at, quote(at, ask=102.5, bid=102), sessions=['2026-11-04', '2026-11-05'])
        capture('sell_confirmed_proceeds_unsettled')
        book.process('settlement', '2026-11-05T09:00:00+08:00', {})
        capture('t_plus_2_settled')
        return dict(data_kind='SYNTHETIC_TEST_ONLY', live_enabled=False, stages=stages)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True)
    a = p.parse_args()
    result = run()
    Path(a.output).write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps([dict(stage=s['stage'], cash=s['cash'], reserved=s['reserved'],
                           held=s['positions'].get('2330', {}).get('qty', 0)) for s in result['stages']]))
