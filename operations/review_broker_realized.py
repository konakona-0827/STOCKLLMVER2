"""Read-only dated Capital realized P/L query; never constructs an executor."""
import argparse
import json
import os
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-root', type=Path, required=True)
    parser.add_argument('--date', required=True)
    parser.add_argument('--symbol', default='1815')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--market-history', action='store_true')
    parser.add_argument('--market-no', type=int, choices=(5,6), default=6)
    args = parser.parse_args()
    from datetime import date
    day = date.fromisoformat(args.date)
    from execution.capital_reply_session import build_live_readonly_session_from_project
    session = build_live_readonly_session_from_project(args.state_root)
    # Quote's native dependencies need their directory retained for this process.
    dll_directory = os.add_dll_directory(str(session.dll_path.parent))
    result = dict(date=args.date, symbol=args.symbol, read_only=True, queries=[])
    try:
        session.connect()
        import comtypes.client
        from execution.capital_crosscheck import parse_reply_row
        result['fills'] = []
        for raw in session.snapshot.tc_rows:
            row = parse_reply_row(raw)
            if row.is_fill and row.symbol == args.symbol:
                result['fills'].append(dict(symbol=row.symbol, side=row.buy_sell,
                    price=str(row.price), quantity=row.quantity,
                    date=row.parts[23], time=row.parts[24]))
        rows = []

        class Events:
            def OnProfitLossGWReport(self, data):
                rows.append(str(data))

        connection = comtypes.client.GetEvents(session._skO, Events())
        for mode in (0, 3):
            query = session._sk.TSPROFITLOSSGWQUERY()
            query.bstrFullAccount = session.account
            query.nTPQueryType = 1
            query.nFunc = mode
            query.bstrStockNo = args.symbol if mode == 3 else ''
            query.bstrStartDate = (day.strftime('%Y%m%d') if mode == 0 else
                                  f'{day.year-1911:03d}{day.month:02d}{day.day:02d}')
            query.bstrEndDate = query.bstrStartDate
            rows.clear()
            rc = session._skO.GetProfitLossGWReport(session.user, query)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                comtypes.client.PumpEvents(.25)
            # Redact account identifiers even in the saved local report.
            safe = []
            for row in rows:
                parts = row.replace(session.account, '[ACCOUNT]').replace(session.user, '[USER]').split(',')
                if len(parts) > 10 and not row.startswith('##'):
                    for index in ((3, 4) if mode == 0 else (2, 3)):
                        parts[index] = '[ACCOUNT]'
                safe.append(','.join(parts))
            result['queries'].append(dict(mode=mode, return_code=rc, rows=safe))
        connection.disconnect()
        if args.market_history:
            from capital_multi_quote import MultiCapitalMarket
            from main import Audit
            market = dict(connection=[], kline_rows=[], quotes=[], complete=False)
            monitor = MultiCapitalMarket(Audit(args.output.parent/'quote_probe'),login_session=session)
            monitor.connect()

            class QuoteEvents:
                def OnConnection(self, kind, code):
                    market['connection'].append([int(kind), int(code)])

                def OnNotifyKLineData(self, symbol, data):
                    if str(symbol) == args.symbol:
                        market['kline_rows'].append(str(data))

                def OnKLineComplete(self, *unused):
                    market['complete'] = True

            quote_sink = QuoteEvents()
            quote_connection = comtypes.client.GetEvents(session._skQ, quote_sink)
            monitor.oddlot_market_no=args.market_no
            quotes,errors=monitor._fetch_one_batch((args.symbol,),timeout=12,batch_index=1,batch_count=1)
            market['quotes']=list(quotes.values())
            market['quote_errors']=errors
            market['kline_rc'] = session._skQ.SKQuoteLib_RequestKLineAMByDate(
                args.symbol, 0, 1, 0, day.strftime('%Y%m%d'), day.strftime('%Y%m%d'), 1)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not market['complete']:
                monitor.pump(.25)
            quote_connection.disconnect()
            monitor.close()
            result['market_history'] = market
        result['status'] = 'QUERIED'
    except Exception as exc:
        # Avoid logging raw exceptions that might include login/account values.
        result['status'] = 'UNAVAILABLE'
        result['error_type'] = type(exc).__name__
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(status=result['status'], fills=result.get('fills'),
        query_count=len(result['queries']),
        kline_rows=len(result.get('market_history',{}).get('kline_rows',[])),
        quote_count=len(result.get('market_history',{}).get('quotes',[]))),ensure_ascii=False))


if __name__ == '__main__':
    main()
