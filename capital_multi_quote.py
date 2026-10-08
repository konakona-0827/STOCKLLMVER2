"""One login/monitor/subscription; per-symbol failures do not abort the batch."""
import time
from datetime import datetime
from capital import CapitalMarket, normalize_stock
from capital_check import unpack, error_info
from config import ROOT, now
from main import snapshot
from execution.capital_session_audit import record_capital_event

MAX_ODDLOT_QUOTE_SYMBOLS = 20


def read_stock(result, original):
    if unpack(result):
        raise ValueError('QUOTE_RETURN_CODE_' + str(unpack(result)))
    if isinstance(result, (tuple, list)):
        return next((v for v in result if hasattr(v, 'nClose')), original)
    return original


class MultiCapitalMarket(CapitalMarket):
    def fetch_batch(self, symbols, timeout=12):
        ordered_symbols = tuple(dict.fromkeys(symbols))
        if not ordered_symbols:
            return {}, {}
        groups = [ordered_symbols[i:i + MAX_ODDLOT_QUOTE_SYMBOLS]
                  for i in range(0, len(ordered_symbols), MAX_ODDLOT_QUOTE_SYMBOLS)]
        quotes, errors = {}, {}
        for group_index, group in enumerate(groups, start=1):
            if group_index > 1:
                # Empirical account probe: LeaveMonitor=0 then re-entering the
                # same SKQuoteLib object allowed the next odd-lot batch.
                rc = self.leave_monitor()
                self.event('BATCH_MONITOR_LEFT', {
                    'return_code': rc,
                    'batch_index': group_index - 1,
                })
                self._enter_monitor()
            else:
                self.connect()
            batch_quotes, batch_errors = self._fetch_one_batch(
                group, timeout=timeout, batch_index=group_index,
                batch_count=len(groups),
            )
            quotes.update(batch_quotes)
            errors.update(batch_errors)
        if len(groups) > 1:
            # A clean final leave avoids retaining a live quote monitor between
            # scan cycles; COM objects and the authenticated reply session stay
            # on the broker thread for the next scan.
            self.leave_monitor()
        return quotes, errors

    def _fetch_one_batch(self, ordered_symbols, *, timeout, batch_index, batch_count):
        quote = self.objects['SKQuoteLib']
        if self.subscribed and self.subscribed != ordered_symbols:
            raise RuntimeError('QUOTE_SYMBOL_SET_CHANGED_REQUIRES_NEW_MONITOR')
        if not self.subscribed:
            status = self.quote_connection_status(phase=f'PRE_SUBSCRIBE_BATCH_{batch_index}')
            if (status.get('return_code') == 0 and status.get('over_limit') is True):
                raise RuntimeError('BROKER_QUOTE_CONNECTION_LIMIT')
            rc = unpack(quote.SKQuoteLib_RequestStocksWithMarketNo(
                1, 5, ','.join(ordered_symbols)
            ))
            self.event('BATCH_SUBSCRIBE', {
                'return_code': rc,
                'symbols': ordered_symbols,
                'symbol_count': len(ordered_symbols),
                'batch_index': batch_index,
                'batch_count': batch_count,
            })
            record_capital_event(
                ROOT,
                session_id=self.session_id,
                call_site='MultiCapitalMarket._fetch_one_batch',
                event='API_CALL',
                object_type='SKQuoteLib',
                object_id=hex(id(quote)),
                api_name='SKQuoteLib_RequestStocksWithMarketNo',
                return_code=rc,
            )
            if rc:
                if rc == 3030:
                    self.quote_connection_status(
                        phase=f'POST_SUBSCRIBE_REJECT_BATCH_{batch_index}'
                    )
                raise RuntimeError('BATCH_SUBSCRIBE_REJECTED_' + str(rc))
            self.subscribed = ordered_symbols
        else:
            self.event('BATCH_SUBSCRIBE_REUSED', {
                'symbols': ordered_symbols,
                'symbol_count': len(ordered_symbols),
                'batch_index': batch_index,
                'batch_count': batch_count,
            })
        wanted, quotes, errors = set(ordered_symbols), {}, {}
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
        for symbol in ordered_symbols:
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
