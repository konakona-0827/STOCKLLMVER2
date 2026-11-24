"""Current public disclosures, with explicit limits; never certify adjusted history."""
from datetime import datetime,timedelta
import json
from urllib.request import Request,urlopen

from .common import TAIPEI,digest,read,save,stamp

SOURCES={
    'TWSE_NEWS':'https://openapi.twse.com.tw/v1/opendata/t187ap04_L',
    'TPEX_NEWS':'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O',
    'TWSE_ACTIONS':'https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL',
    'TPEX_ACTIONS':'https://www.tpex.org.tw/openapi/v1/tpex_exright_prepost',
}


def validate_source_schema(key,rows):
    """A changed source schema must not look like zero company announcements."""
    if not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows):
        raise ValueError('DISCLOSURE_SOURCE_SCHEMA_CHANGED')
    code_fields=('公司代號','SecuritiesCompanyCode') if key.endswith('_NEWS') else ('Code','SecuritiesCompanyCode','StockCode')
    date_fields=('發言日期',) if key.endswith('_NEWS') else ('Date','ExRrightsExDividendDate','ExrightDate','ExDividendDate')
    for row in rows:
        if (not any(row.get(k) is not None and str(row[k]).strip() for k in code_fields)
                or not any(row.get(k) for k in date_fields)
                or (key.endswith('_NEWS') and not any(isinstance(row.get(k),str) and row[k].strip() for k in ('主旨','主旨 ')))):
            raise ValueError('DISCLOSURE_SOURCE_SCHEMA_CHANGED')


def event_facts(event):
    """Compare disclosure content separately from the time it was fetched."""
    return {key:value for key,value in (event or {}).items() if key!='checked_at'}


def action_date(value):
    s=str(value).strip().replace('/','').replace('-','')
    if len(s)==7 and s.isdigit():
        s=str(int(s[:3])+1911)+s[3:]
    return datetime.strptime(s,'%Y%m%d').date()


def current_events(folder,at,fetch=None):
    from pathlib import Path
    folder=Path(folder);path=folder/'current.json';at=stamp(at)
    if path.exists():
        try:
            old=read(path)
            for key,rows in old.get('rows',{}).items():validate_source_schema(key,rows)
            if 0<=(at-stamp(old['checked_at'])).total_seconds()<1800 and stamp(old['checked_at']).date()==at.date():
                return old
        except (OSError,ValueError,TypeError,KeyError,AttributeError):pass
    results={};errors={}
    for key,url in SOURCES.items():
        try:
            if fetch is None:
                req=Request(url,headers={'User-Agent':'TWOpportunityFlow/1.1'})
                with urlopen(req,timeout=20) as response:
                    rows=json.loads(response.read())
            else:
                rows=fetch(url)
            validate_source_schema(key,rows)
            results[key]=rows
            save(folder/(key+'_'+at.strftime('%Y%m%d_%H%M')+'.json'),dict(url=url,checked_at=at.isoformat(),rows=rows))
        except Exception as exc:
            errors[key]=type(exc).__name__+':'+str(exc)[:120]
    result=dict(checked_at=at.isoformat(),sources=SOURCES,rows=results,errors=errors)
    save(path,result)
    return result


def for_symbol(snapshot,symbol,exchange,at,holding_days):
    at=stamp(at);keys=[exchange+'_NEWS',exchange+'_ACTIONS']
    missing=[k for k in keys if k not in snapshot.get('rows',{}) or k in snapshot.get('errors',{})]
    if stamp(snapshot['checked_at']).date()!=at.date() or not 0<=(at-stamp(snapshot['checked_at'])).total_seconds()<=3600:
        missing.append('DISCLOSURES_STALE')
    news=[];actions=[];hazards=[]
    for row in snapshot.get('rows',{}).get(keys[0],[]):
        code=str(row.get('公司代號',row.get('SecuritiesCompanyCode',''))).strip()
        if code!=symbol:continue
        title=str(row.get('主旨',row.get('主旨 ',''))).strip()
        published=action_date(row['發言日期'])
        if published>at.date():
            missing.append('FUTURE_DISCLOSURE');continue
        text=str(row.get('說明',''))
        news.append(dict(title=title,published_date=published.isoformat(),description=text[:1800]))
        if any(term in title for term in ('停止交易','暫停交易','下市','下櫃','減資','變更面額','分割','重整','破產')):
            hazards.append('MATERIAL_TRADING_OR_CAPITAL_EVENT')
    for row in snapshot.get('rows',{}).get(keys[1],[]):
        code=str(row.get('Code',row.get('SecuritiesCompanyCode',row.get('StockCode','')))).strip()
        if code!=symbol:continue
        try:
            day=action_date(row.get('Date',row.get('ExRrightsExDividendDate',row.get('ExrightDate',row.get('ExDividendDate','')))))
        except ValueError:
            missing.append('ACTION_DATE_UNPARSEABLE');continue
        if at.date()<=day<=at.date()+timedelta(days=holding_days*2+3):
            actions.append(dict(date=day.isoformat(),data=row));hazards.append('UPCOMING_CORPORATE_ACTION_REQUIRES_NEW_PLAN')
    facts=dict(status='SOURCE_REVIEW_REQUIRED' if not missing else 'SOURCE_UNAVAILABLE',
        checked_at=snapshot['checked_at'],source_url=snapshot['sources'][keys[0]],
        source_urls=[snapshot['sources'][k] for k in keys],announcements=news,upcoming_actions=actions,
        blockers=sorted(set(missing+hazards)),historical_corporate_actions_verified=False,
        limitations=['CURRENT_DISCLOSURE_FEED_NOT_COMPLETE_NEWS_HISTORY','RAW_UNADJUSTED_HISTORY_REMAINS_UNCERTIFIED'])
    facts['evidence_id']='EVENTS:'+digest(event_facts(facts))[:20]
    return facts


def for_symbols(snapshot,markets,at,holding_days):
    """Index once so disclosure coverage is independent of the quote rotation."""
    grouped={}
    for key,rows in snapshot.get('rows',{}).items():
        index={}
        for row in rows:
            if key.endswith('_NEWS'):
                code=str(row.get('公司代號',row.get('SecuritiesCompanyCode',''))).strip()
            else:
                code=str(row.get('Code',row.get('SecuritiesCompanyCode',row.get('StockCode','')))).strip()
            index.setdefault(code,[]).append(row)
        grouped[key]=index
    result={}
    for symbol,exchange in markets.items():
        view={**snapshot,'rows':{key:index.get(symbol,[]) for key,index in grouped.items()}}
        try:result[symbol]=for_symbol(view,symbol,exchange,at,holding_days)
        except (KeyError,ValueError,TypeError):
            result[symbol]=dict(status='SOURCE_UNAVAILABLE',blockers=['COMPANY_EVENT_PARSE_ERROR'])
    return result
