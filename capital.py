"""Read-only Capital COM session: login, intraday odd-lot quotes only.

Login/event registration follows 掛單測試/capital_oddlot_probe_simple_fixed.py.
Quote interfaces follow the supplied 2.13.59 SDK. No order or inventory calls.
All COM calls and event pumping stay on the main STA thread.
"""
from datetime import datetime
import os
import time
import ctypes
from ctypes import wintypes
from config import ROOT, TAIPEI, now
from capital_check import find_dll, unpack, error_info
from market import quality


def normalize_stock(stock, received_at):
    if int(stock.nTradingLotFlag) != 1:
        raise ValueError('NOT_INTRADAY_ODD_LOT')
    stamp = datetime.strptime(f'{int(stock.nTradingDay):08d}{int(stock.nDealTime):06d}',
                              '%Y%m%d%H%M%S').replace(tzinfo=TAIPEI)
    decimals = int(stock.sDecimal)
    if not 0 <= decimals <= 6:
        raise ValueError('INVALID_PRICE_DECIMALS')
    scale = 10 ** decimals
    def price(value):
        return value / scale if value > 0 else None
    symbol = str(stock.bstrStockNo)
    return dict(symbol=symbol, name=str(stock.bstrStockName), source='CAPITAL_SKCOM',
                quote_basis='capital_intraday_odd_lot', exchange_time=stamp.isoformat(),
                fetched_at=received_at.isoformat(), received_at=received_at.isoformat(), price=price(stock.nClose),
                bid=price(stock.nBid), ask=price(stock.nAsk),
                previous_close=price(stock.nRef), volume_shares=int(stock.nTQty),
                open=price(getattr(stock, 'nOpen', 0)), high=price(getattr(stock, 'nHigh', 0)),
                low=price(getattr(stock, 'nLow', 0)),
                is_trial=bool(stock.nSimulate), quality=quality(stamp.isoformat(), received_at),
                evidence_id=f'CAPITAL:ODDLOT:{symbol}:{stamp.isoformat()}',
                raw_fields={name: getattr(stock, name, None) for name in (
                    'bstrStockNo', 'bstrStockName', 'sDecimal', 'nClose', 'nBid', 'nAsk',
                    'nRef', 'nOpen', 'nHigh', 'nLow', 'nTQty', 'nTradingDay', 'nDealTime', 'nTradingLotFlag', 'nSimulate')})


class CapitalMarket:
    def __init__(self, audit, stopped=lambda: False):
        self.audit, self.stopped = audit, stopped
        self.client = self.sk = None
        self.objects, self.connections, self.sinks = {}, [], []
        self.dll_handle = None
        self.connected = self.ready = self.entered = False
        self.retry_at = 0
        self.subscribed = ()
        self.status = 'DISCONNECTED'
        self.pending_indices = set()
        self.callback_times = {}
        self.quotes = {}

    def event(self, stage, code):
        self.audit.event('CAPITAL_' + stage, {'code': code})

    def pump(self, seconds=.1):
        if self.client and not self.stopped():
            # SKCOM also posts native Windows messages. CoWait/PumpEvents alone
            # may not dispatch its hidden-window socket notifications.
            user32 = ctypes.windll.user32
            user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                            wintypes.UINT, wintypes.UINT, wintypes.UINT]
            user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user32.DispatchMessageW.restype = ctypes.c_ssize_t
            msg = wintypes.MSG()
            for _ in range(1000):
                if not user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            self.client.PumpEvents(seconds)

    def wait(self, predicate, seconds=15):
        end = time.monotonic() + seconds
        while not self.stopped() and time.monotonic() < end and not predicate():
            self.pump(.2)
        return bool(predicate())

    def initialize(self):
        import comtypes.client as client
        self.client = client
        dll, warnings = find_dll()
        for warning in warnings:
            self.event(warning, None)
        self.dll_handle = os.add_dll_directory(str(dll.parent))
        self.sk = client.GetModule(str(dll))
        for name in ('SKCenterLib', 'SKQuoteLib', 'SKReplyLib'):
            self.objects[name] = client.CreateObject(getattr(self.sk, name), interface=getattr(self.sk, 'I' + name))
        owner = self

        class ReplyEvents:
            def OnReplyMessage(self, user, message):
                return -1

        class QuoteEvents:
            def OnConnection(self, kind, code):
                owner.event('QUOTE_CONNECTION', {'kind': int(kind), 'return_code': int(code)})
                if kind == 3003 and code == 0:
                    owner.ready = True
                elif kind == 3002 or code != 0:
                    owner.ready = owner.connected = False
                    owner.status = 'DISCONNECTED'

            def OnNotifyQuoteLONG(self, market, index):
                # Never subscribe/reconnect inside callbacks. Read snapshots in main thread.
                owner.pending_indices.add((int(market), int(index)))
                owner.callback_times[(int(market), int(index))] = now().isoformat()

        self.sinks = [ReplyEvents(), QuoteEvents()]
        self.connections = [client.GetEvents(self.objects[name], sink) for name, sink in zip(
            ('SKReplyLib', 'SKQuoteLib'), self.sinks)]

    def connect(self):
        if self.connected:
            return
        if time.monotonic() < self.retry_at:
            raise RuntimeError('BROKER_DISCONNECTED_RETRY_INTERVAL')
        self.retry_at = time.monotonic() + 60
        self.close()
        self.pending_indices, self.quotes = set(), {}
        try:
            self.initialize()
            user, password = os.getenv('CAPITAL_USER_ID'), os.getenv('CAPITAL_PASSWORD')
            if not user or not password:
                raise RuntimeError('BROKER_CREDENTIALS_MISSING')
            center = self.objects['SKCenterLib']
            rc = int(center.SKCenterLib_Login(user, password))
            self.event('LOGIN', rc)
            if rc:
                raise RuntimeError(f'BROKER_LOGIN_RETURN_CODE_{rc}')
            rc = int(self.objects['SKQuoteLib'].SKQuoteLib_EnterMonitorLONG())
            self.event('ENTER_MONITOR', rc)
            self.entered = rc == 0
            if rc or not self.wait(lambda: self.ready, 30):
                raise RuntimeError('BROKER_QUOTE_NOT_READY')
            self.connected = True
            self.status = 'CONNECTED'
            print('CAPITAL CONNECTED | read-only odd-lot quotes only', flush=True)
        except Exception as exc:
            self.status = 'DISCONNECTED'
            self.event('CONNECT_FAILED', error_info(exc))
            self.close()
            raise

    def fetch(self, symbols):
        self.connect()
        quote = self.objects['SKQuoteLib']
        wanted = tuple(sorted(symbols))
        if wanted != self.subscribed:
            rc = unpack(quote.SKQuoteLib_RequestStocksWithMarketNo(1, 5, ','.join(wanted)))
            self.event('SUBSCRIBE_ODD_LOT', rc)
            if rc:
                raise RuntimeError(f'ODD_LOT_SUBSCRIPTION_REJECTED_{rc}')
            self.subscribed = wanted
            self.wait(lambda: bool(self.pending_indices), 10)
        self.pump(.2)
        issues = []
        for market, index in list(self.pending_indices):
            self.pending_indices.discard((market, index))
            try:
                stock = self.sk.SKSTOCKLONG()
                result = quote.SKQuoteLib_GetStockByIndexLONG(market, index, stock)
                if unpack(result):
                    continue
                if isinstance(result, (tuple, list)):
                    stock = next((v for v in result if hasattr(v, 'nClose')), stock)
                data = normalize_stock(stock, now())
                data['callback_received_at'] = self.callback_times.get((market, index))
                data['retrieval_method'] = 'LONG_CALLBACK_THEN_GET_STOCK_BY_INDEX'
                if data['symbol'] in wanted:
                    self.quotes[data['symbol']] = data
            except (ValueError, TypeError):
                issues.append('CAPITAL_INVALID_ODD_LOT_SNAPSHOT')
        # A post-close subscription may not emit a new quote event. The SDK
        # permits a subscribed symbol lookup; retain its original exchange time.
        for symbol in wanted:
            if symbol in self.quotes:
                continue
            try:
                stock = self.sk.SKSTOCKLONG()
                value = quote.SKQuoteLib_GetStockByMarketAndNo(5, symbol, stock)
                rc = unpack(value)
                self.event('SYMBOL_SNAPSHOT', {'symbol': symbol, 'return_code': rc})
                if rc:
                    continue
                if isinstance(value, (tuple, list)):
                    stock = next((v for v in value if hasattr(v, 'nClose')), stock)
                data = normalize_stock(stock, now())
                data['callback_received_at'] = None
                data['retrieval_method'] = 'SUBSCRIBED_SYMBOL_LOOKUP'
                if data['symbol'] == symbol:
                    self.quotes[symbol] = data
            except (ValueError, TypeError):
                issues.append('CAPITAL_INVALID_LAST_KNOWN_SNAPSHOT')
        result = []
        for symbol in wanted:
            if symbol in self.quotes:
                q = dict(self.quotes[symbol])
                q['quality'] = quality(q['exchange_time'], now())
                result.append(q)
            else:
                issues.append(f'CAPITAL_NO_QUOTE_{symbol}')
        if not result:
            raise RuntimeError('CAPITAL_NO_ODD_LOT_CALLBACK_DATA')
        return result, issues

    def close(self):
        if self.entered and 'SKQuoteLib' in self.objects:
            try:
                self.objects['SKQuoteLib'].SKQuoteLib_LeaveMonitor()
            except Exception:
                pass
        self.entered = self.connected = self.ready = False
        self.subscribed = ()
        for connection in self.connections:
            try:
                connection.disconnect()
            except Exception:
                pass
        self.connections, self.sinks, self.objects = [], [], {}
        if self.dll_handle:
            self.dll_handle.close()
            self.dll_handle = None
