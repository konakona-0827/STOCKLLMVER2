"""Optional, read-only Capital SDK diagnostic. Never imported by the paper bot.

Default: local DLL/COM checks. --connect: one login, quote and inventory queries.
No submission, modification, cancellation or account-to-paper position import.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import struct
import sys
import time
from config import ROOT, load_env, now, redact


def find_dll():
    configured = os.getenv('CAPITAL_COM_DLL', '').strip()
    if configured and Path(configured).is_file():
        return Path(configured).resolve(), []
    arch = 'x64' if struct.calcsize('P') == 8 else 'x86'
    matches = [p for p in (ROOT / 'CapitalAPI_2.13.59').rglob('SKCOM.dll')
               if p.parent.name == arch and 'CapitalAPI_v5' not in str(p)]
    if len(matches) != 1:
        raise RuntimeError('SDK_DLL_NOT_FOUND_OR_AMBIGUOUS')
    return matches[0].resolve(), ['ENV_DLL_PATH_INVALID_USING_LOCAL_SDK'] if configured else []


def error_info(exc):
    code = getattr(exc, 'hresult', None)
    if code is None:
        code = getattr(exc, 'winerror', 0) or 0
    return dict(type=type(exc).__name__, hresult=f'0x{code & 0xffffffff:08X}')


def parse_inventory(raw):
    """V2.13.59 section 4-2-c; omit login/account fields entirely."""
    if raw.startswith('##'):
        return {'end': True}
    parts = [p.strip() for p in raw.split(',')]
    if len(parts) < 15 or parts[1] not in {'T', 'C', 'L'}:
        raise ValueError('INVENTORY_ROW_FORMAT')
    return dict(symbol=parts[0], inventory_type=parts[1], yesterday_qty=int(parts[6]),
                bought_today=int(parts[9]), sold_today=int(parts[10]),
                sellable_qty=int(parts[11]), current_qty=int(parts[14]))


def unpack(result):
    return int(result[-1]) if isinstance(result, (tuple, list)) else int(result)


def connect_read_only(client, sk, objects, result, timeout, symbol):
    center, quote, order, reply = [objects[n] for n in ('SKCenterLib', 'SKQuoteLib', 'SKOrderLib', 'SKReplyLib')]
    state = dict(accounts=[], inventory=[], inventory_complete=False,
                 quote_ready=False, quote_callbacks=0, quote_indices=set(), connections=[], callback_errors=[])

    class ReplyEvents:
        def OnReplyMessage(self, user, message):
            return -1

    class OrderEvents:
        def OnAccount(self, login, data):
            parts = str(data).split(',')
            if len(parts) >= 4 and parts[0].strip() == 'TS':
                account = parts[1].strip() + parts[3].strip()
                if account not in state['accounts']:
                    state['accounts'].append(account)

        def OnRealBalanceReport(self, data):
            try:
                row = parse_inventory(str(data))
                if row.get('end'):
                    state['inventory_complete'] = True
                else:
                    state['inventory'].append(row)
            except (ValueError, TypeError):
                state['callback_errors'].append('INVENTORY_ROW_FORMAT')

    class QuoteEvents:
        def OnConnection(self, kind, code):
            state['connections'].append(dict(kind=int(kind), code=int(code)))
            if kind == 3003 and code == 0:
                state['quote_ready'] = True

        def OnNotifyQuoteLONG(self, market, index):
            state['quote_callbacks'] += 1
            state['quote_indices'].add((int(market), int(index)))

    # Same announcement/account registration pattern as the proven order probe.
    sinks = [ReplyEvents(), OrderEvents(), QuoteEvents()]
    connections = [client.GetEvents(obj, sink) for obj, sink in zip((reply, order, quote), sinks)]
    result['callback_registration'] = 'OK'

    def pump_until(predicate):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not predicate():
            client.PumpEvents(.2)
        return bool(predicate())

    user, password = os.getenv('CAPITAL_USER_ID', ''), os.getenv('CAPITAL_PASSWORD', '')
    if not user or not password:
        result['login'] = 'CREDENTIALS_MISSING'
        return
    # One attempt only. SDK 2.13.59 requires >=5 sec between failed login attempts.
    attempt_file = ROOT / 'data' / 'capital_last_login_attempt.json'
    attempt_file.parent.mkdir(exist_ok=True)
    if attempt_file.exists():
        try:
            previous = float(json.loads(attempt_file.read_text())['epoch'])
            delay = 5.1 - (time.time() - previous)
            if 0 < delay <= 5.1:
                time.sleep(delay)
        except (ValueError, KeyError):
            pass
    attempt_file.write_text(json.dumps({'epoch': time.time()}), encoding='utf-8')
    code = int(center.SKCenterLib_Login(user, password))
    result['login_return_code'] = code
    if code != 0:
        result['login'] = 'FAILED_NO_RETRY'
        return
    result['login'] = 'OK'
    result['order_initialize_return_code'] = int(order.SKOrderLib_Initialize())
    result['certificate_return_code'] = int(order.ReadCertByID(user))
    result['account_request_return_code'] = int(order.GetUserAccount())
    pump_until(lambda: bool(state['accounts']))
    result['stock_account_count'] = len(state['accounts'])
    override = os.getenv('CAPITAL_ACCOUNT', '').strip()
    account = override if override in state['accounts'] else (state['accounts'][0] if len(state['accounts']) == 1 else None)
    result['inventory'] = 'NOT_QUERIED'
    if account and result['order_initialize_return_code'] == 0 and result['certificate_return_code'] == 0:
        code = int(order.GetRealBalanceReport(user, account))
        result['inventory_return_code'] = code
        if code == 0:
            pump_until(lambda: state['inventory_complete'])
            result['inventory'] = 'COMPLETE' if state['inventory_complete'] and not state['callback_errors'] else 'INCOMPLETE'
            result['inventory_rows'] = state['inventory']
            result['inventory_end_received'] = state['inventory_complete']
        else:
            result['inventory'] = 'REQUEST_REJECTED'
    elif len(state['accounts']) > 1:
        result['inventory'] = 'ACCOUNT_SELECTION_REQUIRED'
    result['quote_enter_return_code'] = int(quote.SKQuoteLib_EnterMonitorLONG())
    try:
        if result['quote_enter_return_code'] == 0 and pump_until(lambda: state['quote_ready']):
            code = unpack(quote.SKQuoteLib_RequestStocksWithMarketNo(1, 5, symbol))
            result['quote_subscribe_return_code'] = code
            if code == 0:
                pump_until(lambda: bool(state['quote_callbacks']))
                stock = sk.SKSTOCKLONG()
                value = quote.SKQuoteLib_GetStockByMarketAndNo(5, symbol, stock)
                result['quote_snapshot_return_code'] = unpack(value)
                if isinstance(value, (tuple, list)):
                    stock = next((v for v in value if hasattr(v, 'nClose')), stock)
                if result['quote_snapshot_return_code'] == 0:
                    scale = 10 ** int(stock.sDecimal)
                    result['quote_snapshot'] = dict(symbol=str(stock.bstrStockNo),
                        bid=stock.nBid / scale, ask=stock.nAsk / scale, last=stock.nClose / scale,
                        trading_day=int(stock.nTradingDay), deal_time=int(stock.nDealTime),
                        trading_lot_flag=int(stock.nTradingLotFlag), is_trial=bool(stock.nSimulate),
                        received_at=now().isoformat())
    finally:
        if result['quote_enter_return_code'] == 0:
            result['quote_leave_return_code'] = int(quote.SKQuoteLib_LeaveMonitor())
    result['quote_connections'] = state['connections']
    result['quote_callback_count'] = state['quote_callbacks']
    result['callback_errors'] = state['callback_errors']
    # Keep strong references until the last COM/message-pump operation.
    _keepalive = (sinks, connections)


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--connect', action='store_true')
    parser.add_argument('--timeout', type=float, default=15)
    parser.add_argument('--symbol', default='2330')
    args = parser.parse_args()
    if not args.symbol.isdigit() or len(args.symbol) != 4 or not 1 <= args.timeout <= 30:
        parser.error('symbol must be four digits; timeout must be 1..30 seconds')
    load_env()
    result = dict(timestamp=now().isoformat(), mode='READ_ONLY', real_order_sent=False,
                  python_bits=struct.calcsize('P') * 8, login='NOT_ATTEMPTED',
                  inventory='NOT_QUERIED', inventory_imported_to_paper=False,
                  paper_market_source='CAPITAL_SKCOM', paper_inventory_source='LOCAL_SIMULATED_FILLS')
    directory_handles = []
    try:
        result['is_admin'] = bool(ctypes.windll.shell32.IsUserAnAdmin())
        dll, result['warnings'] = find_dll()
        result['dll'] = str(dll)
        directory_handles.append(os.add_dll_directory(str(dll.parent)))
        import comtypes.client as client
        sk = client.GetModule(str(dll))
        result['type_library'] = 'OK'
        objects = {}
        result['com_activation'] = {}
        for name in ('SKCenterLib', 'SKQuoteLib', 'SKOrderLib', 'SKReplyLib'):
            try:
                objects[name] = client.CreateObject(getattr(sk, name), interface=getattr(sk, 'I' + name))
                result['com_activation'][name] = 'OK'
            except Exception as exc:
                result['com_activation'][name] = error_info(exc)
        if args.connect and len(objects) == 4:
            connect_read_only(client, sk, objects, result, args.timeout, args.symbol)
        elif len(objects) < 4:
            result['blocker'] = 'COM_ACTIVATION_FAILED'
    except Exception as exc:
        result['error'] = error_info(exc)
    finally:
        (ROOT / 'data').mkdir(exist_ok=True)
        text = redact(json.dumps(result, ensure_ascii=False, indent=2))
        (ROOT / 'data' / 'capital_check.json').write_text(text, encoding='utf-8')
        print(text)
    verified = (result['login'] == 'OK' and result['inventory'] == 'COMPLETE'
                and result.get('quote_callback_count', 0) > 0
                and result.get('quote_snapshot_return_code') == 0)
    return 1 if result.get('error') or result.get('blocker') or (args.connect and not verified) else 0


if __name__ == '__main__':
    raise SystemExit(main())
