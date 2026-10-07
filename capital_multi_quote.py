"""One login/monitor/subscription; per-symbol failures do not abort the batch."""
import time
from datetime import datetime
from capital import CapitalMarket, normalize_stock
from capital_check import unpack, error_info
from config import now
from main import snapshot


def read_stock(result, original):
    if unpack(result):
        raise ValueError('QUOTE_RETURN_CODE_' + str(unpack(result)))
    if isinstance(result, (tuple, list)):
        return next((v for v in result if hasattr(v, 'nClose')), original)
    return original


class MultiCapitalMarket(CapitalMarket):
    def fetch_batch(self, symbols, timeout=12):
        self.connect()
        quote = self.objects['SKQuoteLib']
        rc = unpack(quote.SKQuoteLib_RequestStocksWithMarketNo(1, 5, ','.join(symbols)))
        self.event('BATCH_SUBSCRIBE', {'return_code': rc, 'symbols': symbols})
        if rc:
            raise RuntimeError('BATCH_SUBSCRIBE_REJECTED_' + str(rc))
        wanted, quotes, errors = set(symbols), {}, {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and set(quotes) != wanted and not self.stopped():
            self.pump(.1)
            for market, index in list(self.pending_indices):
                self.pending_indices.discard((market, index))
                try:
                    stock = self.sk.SKSTOCKLONG()
                    stock = read_stock(quote.SKQuoteLib_GetStockByIndexLONG(market, index, stock), stock)
                    q = normalize_stock(stock, now())
                    q['callback_received_at'] = self.callback_times.get((market, index))
                    q['retrieval_method'] = 'BATCH_CALLBACK'
                    if q['symbol'] in wanted:
                        quotes[q['symbol']] = q
                except Exception as exc:
                    self.event('BATCH_CALLBACK_ERROR', error_info(exc))
        for symbol in symbols:
            if symbol in quotes or self.stopped():
                continue
            try:
                stock = self.sk.SKSTOCKLONG()
                stock = read_stock(quote.SKQuoteLib_GetStockByMarketAndNo(5, symbol, stock), stock)
                q = normalize_stock(stock, now())
                if q['symbol'] != symbol:
                    raise ValueError('RETURNED_SYMBOL_MISMATCH')
                q['callback_received_at'] = None
                q['retrieval_method'] = 'BATCH_SUBSCRIBED_SYMBOL_LOOKUP'
                quotes[symbol] = q
            except Exception as exc:
                errors[symbol] = type(exc).__name__ + ':' + str(error_info(exc))
                self.event('BATCH_SYMBOL_ERROR', {'symbol': symbol, **error_info(exc)})
        return quotes, errors


def multi_snapshot(symbols, raw, errors, started, completed, at=None):
    at = at or now()
    quotes = []
    for symbol in symbols:
        q = raw.get(symbol)
        s = snapshot(symbol, q, [errors[symbol]] if symbol in errors else [], at)
        s['data_age_seconds'] = round((at - datetime.fromisoformat(s['exchange_time'])).total_seconds(), 3) if s.get('exchange_time') else None
        s.update(quote_status=s['data_quality'], last_price=s['price'],
                 quote_timestamp=s['exchange_time'], open=q.get('open') if q else None,
                 high=q.get('high') if q else None, low=q.get('low') if q else None,
                 volume=s['volume_shares'], volume_unit='shares',
                 request_started_at=started, request_completed_at=completed,
                 retrieval_mode='BROKER')
        quotes.append(s)
    unavailable = sum(q['quote_status'] == 'UNAVAILABLE' for q in quotes)
    return dict(request_timestamp=started, request_completed_at=completed,
                symbols_requested=len(symbols), symbols_received=len(symbols)-unavailable,
                symbols_unavailable=unavailable, quotes=quotes)
