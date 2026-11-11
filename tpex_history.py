"""TPEx official monthly ordinary-market bars; units are explicitly converted."""
from datetime import date
from decimal import Decimal
from urllib.parse import urlencode


def monthly_url(symbol, month):
    return 'https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?'+urlencode(
        dict(code=symbol,date=month.strftime('%Y/%m/%d'),response='json'))


def parse_month(payload,symbol,month,fetched_at,url):
    if payload.get('stat','').lower()!='ok' or payload.get('code')!=symbol:
        raise ValueError('TPEX_DAILY_WRONG_SYMBOL_OR_UNAVAILABLE')
    table=next(t for t in payload['tables'] if t['title']=='個股日成交資訊')
    fields=table['fields']
    index={k:fields.index(v) for k,v in dict(date='日 期',open='開盤',high='最高',low='最低',close='收盤',
        volume='成交張數',turnover='成交仟元').items()}
    bars=[]
    for row in table['data']:
        year,m,day=map(int,row[index['date']].split('/'))
        session=date(year+1911,m,day)
        if (session.year,session.month)!=(month.year,month.month):
            raise ValueError('TPEX_DAILY_WRONG_MONTH')
        values={key:Decimal(str(row[index[key]]).replace(',','')) for key in
                ('open','high','low','close','volume','turnover')}
        if any(not v.is_finite() or v<=0 for v in values.values()):
            continue
        if not values['low']<=min(values['open'],values['close'])<=max(values['open'],values['close'])<=values['high']:
            raise ValueError('TPEX_DAILY_INVALID_OHLC')
        bars.append(dict(date=session.isoformat(),**{k:float(values[k]) for k in ('open','high','low','close')},
            volume=int(values['volume']*1000),turnover_twd=float(values['turnover']*1000),complete=True,
            source='TPEX_DAILY',market='TPEX_ORDINARY_DAILY_NOT_ODD_LOT',source_url=url,retrieved_at=fetched_at))
    if len({b['date'] for b in bars})!=len(bars):
        raise ValueError('TPEX_DAILY_DUPLICATE_DATE')
    return sorted(bars,key=lambda b:b['date'])
