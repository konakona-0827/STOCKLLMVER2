"""Dated source evidence, never a replacement for executable odd-lot quotes."""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime,timedelta
from pathlib import Path
import threading

from .common import TAIPEI,number,read,save,stamp


def decode_tick(event,stock,symbol,market,at):
    source,event_market,index,ptr,day,hms,micros,bid,ask,close,qty,trial=event
    if event_market!=market or str(stock.bstrStockNo)!=symbol or market not in (5,6):
        raise ValueError('TICK_IDENTITY_MISMATCH')
    if trial!=0:raise ValueError('TICK_IS_TRIAL')
    when=datetime.strptime(f'{day:08d}{hms:06d}','%Y%m%d%H%M%S').replace(tzinfo=TAIPEI)
    at=stamp(at)
    if when.date()!=at.date() or when>at:raise ValueError('TICK_DATE_OR_TIME_INVALID')
    if not (0<=int(stock.sDecimal)<=6 and ptr>=0 and qty>0 and 0<=micros<1000000):
        raise ValueError('TICK_FIELDS_INVALID')
    scale=10**int(stock.sDecimal)
    return dict(exchange_time=when.isoformat(),microseconds=micros,ptr=ptr,
        price=number(close/scale,True),quantity=qty,source=source)


def quote_trace(quote,raw=None,error=None):
    """An SDK empty field is upstream evidence, not proof the whole market is empty."""
    raw=raw or {};fields=raw.get('raw_fields') or {};result={}
    for field,sdk in (('bid','nBid'),('ask','nAsk'),('last_price','nClose'),('low','nLow')):
        value=quote.get(field);original=fields.get(sdk)
        try:present=number(value,True)>0
        except (ValueError,TypeError):present=False
        if present:origin='PRESENT'
        elif isinstance(original,(int,float)) and original>0:
            normalized=raw.get('price' if field=='last_price' else field)
            origin=('SOURCE_VALIDATION_REJECTED' if normalized is not None and quote.get('quote_status')=='UNAVAILABLE'
                    else 'SYSTEM_TRANSFORM_LOSS')
        elif isinstance(original,(int,float)) and original<=0:origin='PROVIDER_EMPTY_FIELD'
        elif error:origin='SOURCE_REQUEST_OR_VALIDATION_ERROR'
        elif not raw:origin='SOURCE_NO_RESPONSE'
        else:origin='SOURCE_TRACE_UNAVAILABLE' if sdk not in fields else 'PROVIDER_FIELD_UNAVAILABLE'
        result[field]=dict(origin=origin,sdk_field=sdk,
            sdk_value=original if type(original) in (int,float) else None,
            market_absence_confirmed=False)
    return dict(source='CAPITAL_SKCOM',requested=True,received=bool(raw),
        sdk_price_decimals=fields.get('sDecimal') if type(fields.get('sDecimal')) is int else None,
        sdk_values_are_unscaled_integers=True,
        request_error_type=str(error).split(':',1)[0][:100] if error else None,fields=result,
        interpretation='EMPTY_PROVIDER_VALUE_IS_NOT_PROOF_OF_MARKET_ABSENCE')


def valid_points(rows,at,ticks=False):
    result={};at=stamp(at)
    for row in rows if isinstance(rows,list) else []:
        try:
            when=stamp(row['exchange_time']);price=number(row['price'],True)
            if when.date()!=at.date() or when>at:continue
            clean=dict(exchange_time=when.isoformat(),price=price)
            if ticks:
                if (row.get('source') not in ('HISTORY_TICK','LIVE_TICK') or type(row.get('ptr')) is not int
                        or row['ptr']<0 or type(row.get('quantity')) is not int or row['quantity']<=0):continue
                clean.update(ptr=row['ptr'],microseconds=int(row.get('microseconds',0)),
                    quantity=row['quantity'],source=row['source'])
                key=(clean['ptr'],clean['exchange_time'])
            else:
                if row.get('quote_status')!='LIVE' or row.get('is_trial') is not False:continue
                clean.update(quote_status='LIVE',is_trial=False,retrieval_mode='BROKER')
                key=clean['exchange_time']
            result[key]=clean
        except (KeyError,ValueError,TypeError):continue
    return sorted(result.values(),key=lambda r:(r['exchange_time'],r.get('microseconds',0),r.get('ptr',0)))


class IntradayEvidence:
    def __init__(self,path):
        self.path=Path(path);self.lock=threading.RLock();self.rows={};self.storage_error=None
        try:
            if self.path.exists():
                value=read(self.path)
                if value.get('schema_version')!=1 or not isinstance(value.get('symbols'),dict):
                    raise ValueError('INTRADAY_CACHE_SCHEMA_INVALID')
                self.rows=value['symbols']
        except (OSError,ValueError,AttributeError):self.storage_error='STORAGE_READ_FAILED'

    def record(self,quotes,at):
        at=stamp(at)
        with self.lock:
            # Do not mix an earlier trading session or trust cache fields blindly.
            cleaned={}
            for symbol,row in self.rows.items():
                if not isinstance(row,dict):continue
                samples=valid_points(row.get('observations'),at)[-60:]
                ticks=valid_points(row.get('ticks'),at,True)[-3000:]
                if samples or ticks:cleaned[symbol]=dict(observations=samples,ticks=ticks,
                    history_status=row.get('history_status','SYSTEM_NOT_REQUESTED'))
            self.rows=cleaned
            for q in quotes:
                symbol=q['symbol'];row=self.rows.setdefault(symbol,dict(observations=[],ticks=[]))
                sample=dict(exchange_time=q.get('quote_timestamp'),price=q.get('last_price'),
                    quote_status=q.get('quote_status'),is_trial=q.get('is_trial'))
                samples=valid_points([sample],at)
                if samples and (not row['observations'] or
                        (stamp(samples[0]['exchange_time'])-stamp(row['observations'][-1]['exchange_time'])).total_seconds()>=30):
                    row['observations']=(row['observations']+samples)[-60:]
                row['ticks']=valid_points(row['ticks']+q.get('intraday_ticks',[]),at,True)[-3000:]
                row['history_status']=q.get('tick_history_status','SYSTEM_NOT_REQUESTED')
            try:
                save(self.path,dict(schema_version=1,symbols=self.rows))
                self.storage_error=None
            except OSError:self.storage_error='STORAGE_WRITE_FAILED'

    def evidence(self,symbol,at):
        at=stamp(at)
        with self.lock:
            row=deepcopy(self.rows.get(symbol) or {})
            storage=self.storage_error
        samples=valid_points(row.get('observations'),at)[-60:]
        ticks=valid_points(row.get('ticks'),at,True)[-3000:]
        grouped=defaultdict(list)
        for tick in ticks:
            minute=stamp(tick['exchange_time']).replace(second=0,microsecond=0)
            if minute<at.replace(second=0,microsecond=0):grouped[minute.isoformat()].append(tick)
        bars=[]
        for when,points in sorted(grouped.items())[-60:]:
            prices=[t['price'] for t in points]
            bars.append([when,prices[0],max(prices),min(prices),prices[-1],sum(t['quantity'] for t in points),len(points)])
        gaps=[(stamp(b['exchange_time'])-stamp(a['exchange_time'])).total_seconds() for a,b in zip(samples,samples[1:])]
        return dict(source='CAPITAL_SKCOM_INTRADAY_ODD_LOT',session_date=at.date().isoformat(),
            observations=samples,observation_count=len(samples),max_observation_gap_seconds=max(gaps,default=None),
            tick_count=len(ticks),history_status=row.get('history_status','SYSTEM_NOT_REQUESTED'),
            minute_bars=dict(columns=['minute','open','high','low','close','volume_shares','tick_count'],rows=bars),
            coverage='RECEIVED_TICKS_ONLY_NOT_CERTIFIED_COMPLETE' if ticks else 'NO_TICK_EVIDENCE',
            forming_minute_excluded=True,continuous_coverage_verified=False,storage_error=storage,
            limitations=['SAMPLED_QUOTES_ARE_NOT_MINUTE_BARS','MISSING_MINUTES_ARE_NOT_FILLED',
                'REQUEST_ACCEPTED_DOES_NOT_PROVE_HISTORY_COMPLETE'])


def availability(raw_quote,bars,event,history):
    quote=(raw_quote or {}).get('information_trace') or dict(requested=False,received=bool(raw_quote),
        origin='SYSTEM_NOT_REQUESTED_OR_NOT_DELIVERED' if not raw_quote else 'LEGACY_SOURCE_TRACE_UNAVAILABLE')
    status=history.get('history_status','SYSTEM_NOT_REQUESTED')
    if not isinstance(status,str):status='SYSTEM_HISTORY_METADATA_INVALID'
    history_origin=(status if status.startswith('SYSTEM_') and status!='SYSTEM_NOT_REQUESTED' else
        'SYSTEM_RECEIVE_OR_DECODE_ERROR' if status=='RESPONSE_DROPPED_OR_INVALID' else
        'SOURCE_RESPONSE_INCOMPLETE' if status.startswith(('PROVIDER_','SOURCE_')) else
        'PROVIDED_RECEIVED_TICKS' if history.get('tick_count') else
        'SYSTEM_NOT_REQUESTED' if status=='SYSTEM_NOT_REQUESTED' else 'SOURCE_RESPONSE_INCOMPLETE')
    return dict(oddlot_quote=quote,
        daily_history=dict(origin='PROVIDED',available_bars=len(bars),delivered_bars=len(bars),truncated=False),
        intraday_history=dict(origin=history_origin,history_status=status,
            minute_bars_delivered=len((history.get('minute_bars') or {}).get('rows',[])),
            ticks_delivered=history.get('tick_count',0),
            observation_count=history.get('observation_count',0),continuous_coverage_verified=False),
        current_disclosures=dict(origin='PROVIDED_CURRENT_OFFICIAL_DISCLOSURES' if event and event.get('source_url')
            and event.get('status') in ('CHECKED_CLEAR','SOURCE_REVIEW_REQUIRED') else 'SOURCE_UNAVAILABLE',
            complete_news_history=False),historical_corporate_action_adjustment=dict(origin='SYSTEM_NOT_INTEGRATED',verified=False),
        market_absence_confirmed=False)


def material_history(history,levels,at):
    """Re-review new usable time evidence without hashing every tick or timestamp."""
    at=stamp(at);rows=(history.get('minute_bars') or {}).get('rows',[])
    start=at.replace(second=0,microsecond=0)-timedelta(minutes=5)
    recent=[r for r in rows if start<=stamp(r[0])<at.replace(second=0,microsecond=0)]
    latest=max((stamp(r[0]) for r in rows if stamp(r[0])<at.replace(second=0,microsecond=0)),default=None)
    return dict(has_ticks=bool(history.get('tick_count')),completed_minutes=min(len(rows),5),
        latest_completed_5m_window=int(latest.timestamp()//300) if latest else None,
        recent_minutes=min(len(recent),5),levels={name:'UNOBSERVED' if not recent else
            'ALL_RECEIVED_LOWS_ABOVE' if min(r[3] for r in recent)>price else 'RECEIVED_LOW_AT_OR_BELOW'
            for name,price in levels.items()})


def requested_gaps(candidate,review):
    """Attribute a model's time-series request to actual delivered capabilities."""
    text=' '.join(str(s) for role in ('researcher','challenger') for key in ('wait_conditions','execution_missing')
                  for s in (review.get(role) or {}).get(key,[]))
    import re
    if not re.search(r'分鐘|分[ KＫk]|minute|intraday.{0,12}(series|sequence)',text,re.I):return []
    info=candidate.get('information_availability',{}).get('intraday_history',{})
    return [dict(information='INTRADAY_TIME_SERIES',origin=info.get('origin','SYSTEM_NOT_REQUESTED'),
        history_status=info.get('history_status','SYSTEM_NOT_REQUESTED'),
        minute_bars_delivered=info.get('minute_bars_delivered',0),
        continuous_coverage_verified=False,market_absence_confirmed=False)]
