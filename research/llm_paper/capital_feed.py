"""Only Center/Reply/Quote COM objects. No Order object, certificate or submission."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import time

from .common import Market, stamp, digest, save, read


def normalize(stock, market_number, received_at, depth=None, depth_received=None):
    """SDK fields are retained separately; never convert board-lot sizes to shares."""
    if market_number not in (5, 6) or int(stock.nTradingLotFlag) != 1:
        raise ValueError("ODDLOT_MARKET_REQUIRED")
    scale = 10 ** int(stock.sDecimal)
    if not 1 <= scale <= 1000000:
        raise ValueError("INVALID_DECIMALS")
    price = lambda x: float(x) / scale if x > 0 else None
    market = Market()
    exchange = datetime.strptime(f"{int(stock.nTradingDay):08d}{int(stock.nDealTime):06d}", "%Y%m%d%H%M%S").replace(tzinfo=market.tz).isoformat()
    raw = {key: getattr(stock, key) for key in ("nStockIdx", "sDecimal", "sTypeNo", "bstrMarketNo", "bstrStockNo",
                "nOpen", "nHigh", "nLow", "nClose", "nRef", "nBid", "nBc", "nAsk", "nAc", "nTQty", "nUp", "nDown",
                "nSimulate", "nTradingDay", "nTradingLotFlag", "nDealTime")}
    bid, ask = price(stock.nBid), price(stock.nAsk)
    result = dict(symbol=str(stock.bstrStockNo), name=str(stock.bstrStockName), exchange="TWSE" if market_number == 5 else "TPEX",
                  source="CAPITAL_SKCOM", quote_basis="capital_intraday_odd_lot", retrieval_mode="BROKER", volume_unit="shares",
                  quote_timestamp=exchange, received_at=received_at, last_price=price(stock.nClose), open=price(stock.nOpen),
                  high=price(stock.nHigh), low=price(stock.nLow), bid=bid, ask=ask, volume=int(stock.nTQty),
                  limit_up=price(stock.nUp), limit_down=price(stock.nDown), is_trial=bool(stock.nSimulate),
                  raw_stock=raw, raw_depth=depth, depth_received_at=depth_received, bid_size_shares=None, ask_size_shares=None,
                  depth_status="NO_VERIFIED_CALLBACK", quote_status="LAST_KNOWN")
    if depth is not None and depth.get("market_number") == market_number and not depth["is_trial"]:
        if price(depth["bid"]) == bid and price(depth["ask"]) == ask:
            result.update(bid_size_shares=depth["bid_qty"], ask_size_shares=depth["ask_qty"], depth_status="ODDLOT_CALLBACK_SHARES")
    result["evidence_id"] = "capital-" + digest(dict(stock=raw, depth=depth, received_at=received_at))[:24]
    return result


class CapitalFeed:
    def __init__(self, output, symbols, etfs=()):
        self.output, self.symbols, self.etfs = Path(output), tuple(symbols), set(etfs)
        self.output.mkdir(parents=True, exist_ok=True)
        self.depth, self.depth_times, self.index_symbols = {}, {}, {}
        self.objects, self.connections = {}, []
        self.ready = False
        self.started = False
        self.depth_symbols = set()
        self.api_codes = []

    def connect(self):
        from config import load_env
        from capital_check import find_dll, unpack
        import comtypes.client as client
        load_env()
        dll, _ = find_dll()
        self.dll_handle = os.add_dll_directory(str(dll.parent))
        self.client, self.sk = client, client.GetModule(str(dll))
        for name in ("SKCenterLib", "SKReplyLib", "SKQuoteLib"):
            self.objects[name] = client.CreateObject(getattr(self.sk, name), interface=getattr(self.sk, "I" + name))
        owner = self

        class ReplyEvents:
            def OnReplyMessage(self, user, message):
                return -1

        class QuoteEvents:
            def OnConnection(self, kind, code):
                if kind == 3003 and code == 0:
                    owner.ready = True
                if kind == 3002 or code:
                    owner.ready = False

            def OnNotifyQuoteLONG(self, market, index):
                pass

            def OnNotifyBest5LONG(self, market, index, *values):
                if market not in (5, 6) or len(values) != 25:
                    return
                owner.depth[(market, index)] = dict(market_number=market, bid=int(values[0]), bid_qty=int(values[1]),
                    ask=int(values[12]), ask_qty=int(values[13]), is_trial=bool(values[24]))
                owner.depth_times[(market, index)] = stamp()

            def OnNotifyHistoryTicksLONG(self, *args):
                pass

            def OnNotifyTicksLONG(self, *args):
                pass

        self.sinks = [ReplyEvents(), QuoteEvents()]
        self.connections = [client.GetEvents(self.objects["SKReplyLib"], self.sinks[0]), client.GetEvents(self.objects["SKQuoteLib"], self.sinks[1])]
        rc = int(self.objects["SKCenterLib"].SKCenterLib_Login(os.environ["CAPITAL_USER_ID"], os.environ["CAPITAL_PASSWORD"]))
        self.api_codes.append(dict(api="SKCenterLib_Login", code=rc))
        if rc:
            raise RuntimeError("CAPITAL_LOGIN_CODE_" + str(rc))
        rc = int(self.objects["SKQuoteLib"].SKQuoteLib_EnterMonitorLONG())
        self.api_codes.append(dict(api="SKQuoteLib_EnterMonitorLONG", code=rc))
        self.started = rc == 0
        if rc:
            raise RuntimeError("CAPITAL_MONITOR_CODE_" + str(rc))
        deadline = time.monotonic() + 25
        while not self.ready and time.monotonic() < deadline:
            self.pump(.1)
        if not self.ready:
            raise RuntimeError("CAPITAL_MONITOR_NOT_READY")
        rc = unpack(self.objects["SKQuoteLib"].SKQuoteLib_RequestStocksWithMarketNo(1, 5, ",".join(self.symbols)))
        self.api_codes.append(dict(api="SKQuoteLib_RequestStocksWithMarketNo", market=5, code=rc))
        if rc:
            raise RuntimeError("CAPITAL_SUBSCRIBE_CODE_" + str(rc))

    def pump(self, seconds=.1):
        import ctypes
        from ctypes import wintypes
        user32=ctypes.windll.user32
        user32.PeekMessageW.argtypes=[ctypes.POINTER(wintypes.MSG),wintypes.HWND,wintypes.UINT,wintypes.UINT,wintypes.UINT]
        user32.TranslateMessage.argtypes=[ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.argtypes=[ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype=ctypes.c_ssize_t
        msg=wintypes.MSG()
        for _ in range(1000):
            if not user32.PeekMessageW(ctypes.byref(msg),None,0,0,1):break
            user32.TranslateMessage(ctypes.byref(msg));user32.DispatchMessageW(ctypes.byref(msg))
        self.client.PumpEvents(seconds)

    def stopped(self):
        return False

    def subscribe_depth(self, symbols):
        from capital_check import unpack
        wanted = set(symbols)
        if len(wanted) > 10 or not wanted.issubset(self.symbols):
            raise ValueError("DEPTH_SUBSCRIPTION_LIMIT")
        if self.depth_symbols - wanted:
            # SDK lacks a market-specific cancellation; restart only this quote
            # monitor to remove old odd-lot subscriptions safely on its STA.
            quote = self.objects["SKQuoteLib"]
            rc = unpack(quote.SKQuoteLib_LeaveMonitor())
            if rc:
                raise RuntimeError("CAPITAL_LEAVE_FAILED")
            self.ready, self.depth_symbols = False, set()
            self.depth.clear(); self.depth_times.clear()
            rc = int(quote.SKQuoteLib_EnterMonitorLONG())
            if rc:
                raise RuntimeError("CAPITAL_REENTER_FAILED")
            deadline = time.monotonic() + 20
            while not self.ready and time.monotonic() < deadline:
                self.pump(.1)
            if not self.ready:
                raise RuntimeError("CAPITAL_REENTER_NOT_READY")
            rc = unpack(quote.SKQuoteLib_RequestStocksWithMarketNo(1, 5, ",".join(self.symbols)))
            if rc:
                raise RuntimeError("CAPITAL_RESUBSCRIBE_FAILED")
        for symbol in sorted(wanted - self.depth_symbols):
            result = self.objects["SKQuoteLib"].SKQuoteLib_RequestTicksWithMarketNo(-1, 5, symbol)
            rc = unpack(result)
            self.api_codes.append(dict(api="SKQuoteLib_RequestTicksWithMarketNo", symbol=symbol, market=5, code=rc))
            if rc:
                raise RuntimeError("CAPITAL_DEPTH_CODE_" + str(rc))
            self.depth_symbols.add(symbol)
            self.pump(.1)

    def snapshot(self):
        from capital_check import unpack
        quotes, rejected = [], []
        for symbol in self.symbols:
            value = self.objects["SKQuoteLib"].SKQuoteLib_GetStockByMarketAndNo(5, symbol, self.sk.SKSTOCKLONG())
            if unpack(value):
                rejected.append(dict(symbol=symbol, status="NO_STOCK_SNAPSHOT"))
                continue
            stock = next((v for v in value if hasattr(v, "nClose")), None)
            if stock is None or str(stock.bstrStockNo) != symbol:
                rejected.append(dict(symbol=symbol, status="SYMBOL_MISMATCH"))
                continue
            key = (5, int(stock.nStockIdx))
            try:
                q = normalize(stock, 5, stamp(), self.depth.get(key), self.depth_times.get(key))
                q["asset_type"] = "ETF" if symbol in self.etfs else "STOCK"
                quotes.append(q)
            except (ValueError, TypeError):
                rejected.append(dict(symbol=symbol, status="INVALID_STOCK_DATA"))
        return dict(at=stamp(), mode="PAPER_ONLY", quotes=quotes, rejected=rejected, api_codes=list(self.api_codes),
                    collector="CENTER_REPLY_QUOTE_ONLY_NO_ORDER_OBJECT")

    def close(self):
        if self.started:
            self.objects["SKQuoteLib"].SKQuoteLib_LeaveMonitor()
        for c in self.connections:
            c.disconnect()
        self.connections.clear(); self.objects.clear()
        if hasattr(self, "dll_handle"):
            self.dll_handle.close()


def probe(output, universe):
    out = Path(output)
    out.mkdir(parents=True, exist_ok=False)
    u = read(universe)
    feed = CapitalFeed(out, u["symbols"], u.get("etf_symbols", []))
    try:
        feed.connect()
        feed.subscribe_depth(u["symbols"][:10])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            feed.pump(.1)
        result = feed.snapshot()
        save(out / "snapshot.json", result)
        summary = dict(at=result["at"], quotes=len(result["quotes"]), depth_callbacks=len(feed.depth),
                       actual_limits=sum(q["limit_up"] is not None and q["limit_down"] is not None for q in result["quotes"]),
                       side_depth=sum(q["bid_size_shares"] is not None for q in result["quotes"]), api_codes=feed.api_codes,
                       mode="READ_ONLY_NO_ORDER_OBJECT", status="COLLECTED")
    except Exception as exc:
        summary = dict(at=stamp(), status="ERROR", error_type=type(exc).__name__, error=str(exc)[:100], api_codes=feed.api_codes)
    finally:
        feed.close()
    save(out / "summary.json", summary)
    return summary
