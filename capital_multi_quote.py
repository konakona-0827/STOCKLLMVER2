"""One login/monitor/subscription; per-symbol failures do not abort the batch."""
import time
import math
from collections import deque
from datetime import datetime
from capital import CapitalMarket, normalize_stock
from capital_check import unpack, error_info
from config import ROOT, now
from main import snapshot
from execution.capital_session_audit import record_capital_event

MAX_ODDLOT_QUOTE_SYMBOLS = 20
MAX_ODDLOT_HISTORY_SYMBOLS = 10


def buy_quote_complete(q,at):
    """Do not finish a BUY lookup on an early price-only callback."""
    if snapshot(q['symbol'],q,at=at)['data_quality']!='LIVE' or q.get('is_trial') is not False:
        return False
    if any(type(q.get(k)) not in (int,float) or not math.isfinite(q[k]) or q[k]<=0
           for k in ('price','bid','ask','low')):
        return False
    return q['bid']<=q['ask'] and (q.get('high') is None or q['low']<=q['high'])


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
        from market_venue import oddlot_market
        override=getattr(self,'flow_market_override',None)
        if override is not None and override not in (5,6):
            raise ValueError('INVALID_FLOW_ODDLOT_MARKET')
        resolve_market=lambda symbol: override if override is not None else oddlot_market(symbol)
        groups=[]
        for market_no in (5,6):
            names=tuple(s for s in ordered_symbols if resolve_market(s)==market_no)
            limit=MAX_ODDLOT_HISTORY_SYMBOLS if getattr(self,'flow_require_complete_buy',False) else MAX_ODDLOT_QUOTE_SYMBOLS
            groups.extend(names[i:i+limit] for i in range(0,len(names),limit))
        quotes, errors = {}, {}
        for group_index, group in enumerate(groups, start=1):
            self.oddlot_market_no=resolve_market(group[0])
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
        # Leave after every scan, including a single-symbol-group scan. Keeping
        # the monitor subscribed overnight precedes a first-morning symbol-set
        # switch. That switch crashed in the COM message pump immediately after
        # re-entering the monitor (2026-10-28 09:05).
        # Retain the logged-in COM objects and event sink for the next scan.
        self.collect_intraday_ticks=False
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
                1, getattr(self,'oddlot_market_no',5), ','.join(ordered_symbols)
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
        strict=getattr(self,'flow_require_complete_buy',False)
        self.intraday_tick_events=deque(maxlen=20000)
        self.intraday_tick_overflow=self.intraday_tick_invalid=False
        self.collect_intraday_ticks=strict
        histories={s:[] for s in ordered_symbols};history_status={};stock_cache={}
        requested=set()
        if strict:
            for page,symbol in enumerate(ordered_symbols[:MAX_ODDLOT_HISTORY_SYMBOLS]):
                try:
                    rc=unpack(quote.SKQuoteLib_RequestTicksWithMarketNo(page,getattr(self,'oddlot_market_no',5),symbol))
                    history_status[symbol]='REQUEST_ACCEPTED_NO_EVENTS' if rc==0 else 'PROVIDER_REQUEST_REJECTED:'+str(rc)
                    if rc==0:requested.add(symbol)
                except AttributeError:
                    history_status[symbol]='SYSTEM_SDK_HISTORY_METHOD_UNAVAILABLE'
                except (TypeError,ValueError):
                    history_status[symbol]='SYSTEM_HISTORY_REQUEST_RESULT_INVALID'
                except Exception as exc:
                    history_status[symbol]='SOURCE_HISTORY_REQUEST_ERROR:'+type(exc).__name__
                self.event('ODDLOT_HISTORY_REQUEST',dict(symbol=symbol,status=history_status[symbol]))
        ready=set()
        deadline = time.monotonic() + timeout
        # History has no completion event. Keep a bounded response window and
        # explicitly report partial coverage; never certify an empty response.
        last_tick_received=None
        while time.monotonic() < deadline and not self.stopped():
            history_waiting=bool(requested and (not all(histories[s] for s in requested)
                or last_tick_received is None or time.monotonic()-last_tick_received<.5))
            if ready==wanted and not history_waiting:break
            self.pump(.1)
            for market, index in list(self.pending_indices):
                self.pending_indices.discard((market, index))
                try:
                    stock = self.sk.SKSTOCKLONG()
                    stock = read_stock(quote.SKQuoteLib_GetStockByIndexLONG(market, index, stock), stock)
                    stock_cache[(market,index)]=stock
                    q = normalize_stock(stock, now())
                    q['callback_received_at'] = self.callback_times.get((market, index))
                    q['retrieval_method'] = 'BATCH_CALLBACK'
                    if q['symbol'] in wanted:
                        q['venue']='TPEx' if int(market)==6 else 'TWSE'
                        quotes[q['symbol']] = q
                        complete=(buy_quote_complete(q,now()) if getattr(self,'flow_require_complete_buy',False)
                                  else snapshot(q['symbol'],q,at=now())['data_quality']!='UNAVAILABLE')
                        if complete:
                            ready.add(q['symbol'])
                        else:
                            ready.discard(q['symbol'])
                except Exception as exc:
                    self.event('BATCH_CALLBACK_ERROR', error_info(exc))
            from opportunity_flow.intraday import decode_tick
            while self.intraday_tick_events:
                event=self.intraday_tick_events.popleft();_,market,index,*_=event
                try:
                    if market!=getattr(self,'oddlot_market_no',5):continue
                    stock=stock_cache.get((market,index))
                    if stock is None:
                        original=self.sk.SKSTOCKLONG()
                        stock=read_stock(quote.SKQuoteLib_GetStockByIndexLONG(market,index,original),original)
                        stock_cache[(market,index)]=stock
                    symbol=str(stock.bstrStockNo)
                    if symbol not in requested:continue
                    if event[-1]!=0:continue  # Trial ticks are expected, not trades.
                    tick=decode_tick(event,stock,symbol,market,now())
                    histories[symbol].append(tick)
                    last_tick_received=time.monotonic()
                    if len(histories[symbol])>3000:histories[symbol]=histories[symbol][-3000:]
                    history_status[symbol]='RECEIVED_TICKS_PARTIAL_COVERAGE'
                except Exception:self.intraday_tick_invalid=True
        for symbol in ordered_symbols:
            if symbol in ready or self.stopped():
                continue
            try:
                stock = self.sk.SKSTOCKLONG()
                market_no=getattr(self,'oddlot_market_no',5)
                stock = read_stock(quote.SKQuoteLib_GetStockByMarketAndNo(market_no, symbol, stock), stock)
                q = normalize_stock(stock, now())
                if q['symbol'] != symbol:
                    raise ValueError('RETURNED_SYMBOL_MISMATCH')
                q['callback_received_at'] = None
                q['retrieval_method'] = 'BATCH_SUBSCRIBED_SYMBOL_LOOKUP'
                q['venue']='TPEx' if market_no==6 else 'TWSE'
                quotes[symbol] = q
            except Exception as exc:
                errors[symbol] = type(exc).__name__ + ':' + str(error_info(exc))
                self.event('BATCH_SYMBOL_ERROR', {'symbol': symbol, **error_info(exc)})
        if strict:
            for symbol in ordered_symbols:
                if symbol in quotes:
                    quotes[symbol].update(intraday_ticks=histories[symbol],
                        tick_history_status='RESPONSE_DROPPED_OR_INVALID' if self.intraday_tick_overflow or self.intraday_tick_invalid else
                            history_status.get(symbol,'SYSTEM_NOT_REQUESTED'))
            self.collect_intraday_ticks=False
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
