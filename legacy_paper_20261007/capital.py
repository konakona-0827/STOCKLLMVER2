"""Read-only Capital COM session: login, intraday odd-lot quotes and inventory.

Login/event registration follows 掛單測試/capital_oddlot_probe_simple_fixed.py.
Quote/inventory interfaces follow the supplied 2.13.59 SDK. No order API calls.
All COM calls and event pumping stay on the main STA thread.
"""
from datetime import datetime
import os
import time
from config import ROOT, TAIPEI, now
from capital_check import find_dll, parse_inventory, unpack, error_info
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
                fetched_at=received_at.isoformat(), price=price(stock.nClose),
                bid=price(stock.nBid), ask=price(stock.nAsk),
                previous_close=price(stock.nRef), volume_shares=int(stock.nTQty),
                is_trial=bool(stock.nSimulate), quality=quality(stamp.isoformat(), received_at),
                evidence_id=f'CAPITAL:ODDLOT:{symbol}:{stamp.isoformat()}')


class CapitalMarket:
    def __init__(self, ledger, stopped=lambda: False):
        self.ledger, self.stopped = ledger, stopped
        self.client = self.sk = None
        self.objects, self.connections, self.sinks = {}, [], []
        self.dll_handle = None
        self.connected = self.ready = self.entered = False
        self.retry_at = 0
        self.subscribed = ()
        self.accounts = []
        self.inventory_rows = []
        self.inventory_complete = False
        self.inventory_requested = False
        self.inventory_at = None
        self.inventory_query_at = 0
        self.inventory_status = 'NOT_QUERIED'
        self.inventory_errors = False
        self.status = 'DISCONNECTED'
        self.pending_indices = set()
        self.quotes = {}

    def event(self, stage, code):
        self.ledger.event('CAPITAL_' + stage, {'code': code})

    def pump(self, seconds=.1):
        if self.client and not self.stopped():
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
        for name in ('SKCenterLib', 'SKOrderLib', 'SKQuoteLib', 'SKReplyLib'):
            self.objects[name] = client.CreateObject(getattr(self.sk, name), interface=getattr(self.sk, 'I' + name))
        owner = self

        class ReplyEvents:
            def OnReplyMessage(self, user, message):
                return -1

        class OrderEvents:
            def OnAccount(self, login, data):
                fields = str(data).split(',')
                if len(fields) >= 4 and fields[0].strip() == 'TS':
                    account = fields[1].strip() + fields[3].strip()
                    if account not in owner.accounts:
                        owner.accounts.append(account)

            def OnRealBalanceReport(self, data):
                if not owner.inventory_requested:
                    return
                try:
                    row = parse_inventory(str(data))
                    if row.get('end'):
                        owner.inventory_complete = True
                        owner.inventory_at = now().isoformat()
                        owner.inventory_status = 'INCOMPLETE' if owner.inventory_errors else 'COMPLETE'
                        owner.inventory_requested = False
                    else:
                        owner.inventory_rows.append(row)
                except (ValueError, TypeError):
                    owner.inventory_errors = True

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

        self.sinks = [ReplyEvents(), OrderEvents(), QuoteEvents()]
        self.connections = [client.GetEvents(self.objects[name], sink) for name, sink in zip(
            ('SKReplyLib', 'SKOrderLib', 'SKQuoteLib'), self.sinks)]

    def connect(self):
        if self.connected:
            return
        if time.monotonic() < self.retry_at:
            raise RuntimeError('BROKER_DISCONNECTED_RETRY_INTERVAL')
        self.retry_at = time.monotonic() + 60
        self.close()
        self.accounts, self.pending_indices, self.quotes = [], set(), {}
        self.inventory_complete = self.inventory_requested = False
        self.inventory_status = 'NOT_QUERIED'
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
            order = self.objects['SKOrderLib']
            for name in ('SKOrderLib_Initialize', 'ReadCertByID', 'GetUserAccount'):
                rc = int(getattr(order, name)(user) if name == 'ReadCertByID' else getattr(order, name)())
                self.event(name, rc)
            self.wait(lambda: bool(self.accounts), 8)
            rc = int(self.objects['SKQuoteLib'].SKQuoteLib_EnterMonitorLONG())
            self.event('ENTER_MONITOR', rc)
            self.entered = rc == 0
            if rc or not self.wait(lambda: self.ready):
                raise RuntimeError('BROKER_QUOTE_NOT_READY')
            self.connected = True
            self.status = 'CONNECTED'
            print('CAPITAL CONNECTED | read-only odd-lot quotes and inventory', flush=True)
        except Exception as exc:
            self.status = 'DISCONNECTED'
            self.event('CONNECT_FAILED', error_info(exc))
            self.close()
            raise

    def inventory(self):
        return dict(source='CAPITAL_SKCOM', status=self.inventory_status,
                    received_at=self.inventory_at, account_count=len(self.accounts),
                    positions=list(self.inventory_rows) if self.inventory_status == 'COMPLETE' else [],
                    usable_for_strategy_sell=False)

    def query_inventory(self):
        if self.inventory_requested:
            if time.monotonic() - self.inventory_query_at > 20:
                self.inventory_status = 'TIMEOUT'
            return  # Do not overlap uncorrelated responses from timed-out queries.
        if time.monotonic() - self.inventory_query_at < 60:
            return
        self.inventory_query_at = time.monotonic()
        override = os.getenv('CAPITAL_ACCOUNT', '').strip()
        account = override if override in self.accounts else (self.accounts[0] if len(self.accounts) == 1 else None)
        if not account:
            self.inventory_status = 'ACCOUNT_UNAVAILABLE_OR_AMBIGUOUS'
            return
        self.inventory_rows, self.inventory_errors = [], False
        self.inventory_complete = False
        self.inventory_requested = True
        self.inventory_status = 'REQUESTED'
        rc = int(self.objects['SKOrderLib'].GetRealBalanceReport(os.environ['CAPITAL_USER_ID'], account))
        self.event('INVENTORY_QUERY', rc)
        if rc:
            self.inventory_status = 'REQUEST_REJECTED'
            self.inventory_requested = False

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
        try:
            self.query_inventory()
        except Exception as exc:
            self.inventory_status = 'ERROR'
            self.inventory_requested = False
            self.event('INVENTORY_ERROR', error_info(exc))
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
                if data['symbol'] in wanted:
                    self.quotes[data['symbol']] = data
            except (ValueError, TypeError):
                issues.append('CAPITAL_INVALID_ODD_LOT_SNAPSHOT')
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
