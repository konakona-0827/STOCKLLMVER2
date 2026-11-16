"""Free TWSE MIS board-lot references. Never relabel these as odd-lot quotes."""
from datetime import datetime
import hashlib
import json
import time
from urllib.parse import urlencode
from urllib.request import Request,urlopen

from .common import TAIPEI,number


def last_trade(row):
    """Match MIS's tradeFallback: an empty event price is not no trading all day.

    The official frontend uses trade.z AND trade.t when top-level z is missing.
    Never use pz, yesterday's close, a book midpoint or the feed/query timestamp.
    """
    try:
        price=number(row.get('z'),True)
    except (ValueError,TypeError):
        trade=row.get('trade')
        if not isinstance(trade,dict):
            raise ValueError('PUBLIC_LAST_TRADE_UNAVAILABLE')
        try:
            price=number(trade.get('z'),True)
        except (ValueError,TypeError):
            raise ValueError('PUBLIC_LAST_TRADE_UNAVAILABLE')
        when=trade.get('t')
        source='trade.z'
    else:
        when=row.get('t')
        source='z'
    try:
        timestamp=datetime.strptime(row['d']+' '+when,'%Y%m%d %H:%M:%S').replace(tzinfo=TAIPEI)
    except (ValueError,TypeError,KeyError):
        raise ValueError('PUBLIC_LAST_TRADE_TIME_INVALID')
    return price,timestamp,source


def parse(payload,requested):
    if payload.get("rtcode")!="0000" or not isinstance(payload.get("msgArray"),list):
        raise ValueError("PUBLIC_QUOTE_SOURCE_UNAVAILABLE")
    quotes,errors=[],[]
    expected={(m,s) for m,s in requested}
    returned=set()
    for row in payload["msgArray"]:
        exchange={"tse":"TWSE","otc":"TPEX"}.get(row.get("ex"))
        symbol=str(row.get("c",""))
        if (exchange,symbol) not in expected:
            raise ValueError("PUBLIC_QUOTE_SYMBOL_MISMATCH")
        if (exchange,symbol) in returned:
            raise ValueError("DUPLICATE_PUBLIC_QUOTE")
        returned.add((exchange,symbol))
        try:
            price,when,field=last_trade(row)
            quotes.append(dict(symbol=symbol,exchange=exchange,last_price=price,
                quote_timestamp=when.isoformat(),quote_status="PUBLIC_REFERENCE",
                quote_basis="TWSE_MIS_BOARD_LOT_NOT_ODDLOT",source="TWSE_MIS",is_trial=False,
                price_source_field=field,last_trade_fallback=field=='trade.z'))
        except (ValueError,TypeError,KeyError) as exc:
            errors.append(dict(symbol=symbol,exchange=exchange,reason=str(exc)))
    errors.extend(dict(exchange=m,symbol=s,reason="PUBLIC_QUOTE_MISSING_OR_NO_LAST_TRADE") for m,s in expected-returned)
    return quotes,errors


def fetch(symbols,previous_call=0):
    """One request for at most forty symbols, respecting source's five-second delay."""
    if not 1<=len(symbols)<=40 or len(set(symbols))!=len(symbols):
        raise ValueError("PUBLIC_QUOTE_BATCH_REQUIRE_1_TO_40_UNIQUE_SYMBOLS")
    ex=[]
    for market,symbol in symbols:
        if market not in ("TWSE","TPEX") or len(symbol)!=4 or not symbol.isdigit():
            raise ValueError("PUBLIC_QUOTE_MARKET_OR_SYMBOL_INVALID")
        ex.append(("tse" if market=="TWSE" else "otc")+"_"+symbol+".tw")
    time.sleep(max(0,5-(time.monotonic()-previous_call)))
    url="https://mis.twse.com.tw/stock/api/getStockInfo.jsp?"+urlencode(dict(ex_ch="|".join(ex),json=1,delay=0))
    request=Request(url,headers={"User-Agent":"Mozilla/5.0","Referer":"https://mis.twse.com.tw/stock/"})
    started=time.monotonic()
    with urlopen(request,timeout=20) as response:
        raw=response.read()
    payload=json.loads(raw)
    quotes,errors=parse(payload,symbols)
    return dict(quotes=quotes,errors=errors,http_requests=1,bytes=len(raw),
        elapsed_seconds=round(time.monotonic()-started,3),retrieved_at=datetime.now(TAIPEI).isoformat(),
        last_trade_fallback_count=sum(q['last_trade_fallback'] for q in quotes),
        source_sha256=hashlib.sha256(raw).hexdigest(),
        decision_scope="PUBLIC_PRICE_REFERENCE_NOT_EXECUTION_EVIDENCE",additional_data_fee_twd=0)
