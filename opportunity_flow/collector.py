"""Official public daily bulk data, cached by market/session. No paid fallback."""
from __future__ import annotations

import csv
from datetime import date,datetime,timedelta
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request,urlopen

from .common import TAIPEI,number,read,save,stamp

FIELDS=("symbol","exchange","date","open","high","low","close","volume","turnover_twd",
        "retrieved_at","source","source_sha256","price_basis")
MASTERS={"TWSE":"https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
         "TPEX":"https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"}


class PublicHTTP:
    def __init__(self,folder,delay=1.5,stopped=lambda:False):
        self.folder=Path(folder)
        self.folder.mkdir(parents=True,exist_ok=True)
        self.delay,self.last=delay,0
        self.records=[]
        self.stopped=stopped

    def get(self,name,url):
        if self.stopped():raise RuntimeError('COLLECTION_STOPPED')
        path=self.folder/(name+".json")
        meta=path.with_suffix(".meta.json")
        cached=path.exists() and meta.exists()
        if cached:
            raw=path.read_bytes()
            record=read(meta)
            if record["sha256"]!=hashlib.sha256(raw).hexdigest() or record["url"]!=url:
                raise ValueError("PUBLIC_CACHE_HASH_OR_URL_MISMATCH")
        else:
            time.sleep(max(0,self.delay-(time.monotonic()-self.last)))
            if self.stopped():raise RuntimeError('COLLECTION_STOPPED')
            started=time.monotonic()
            request=Request(url,headers={"User-Agent":"TWOpportunityFlow/1.0 (personal research)","Accept":"application/json"})
            with urlopen(request,timeout=45) as response:
                raw=response.read()
            self.last=time.monotonic()
            obj=json.loads(raw)
            if not isinstance(obj,(dict,list)):
                raise ValueError("PUBLIC_SOURCE_NOT_JSON_TABLE")
            record=dict(url=url,sha256=hashlib.sha256(raw).hexdigest(),retrieved_at=datetime.now(TAIPEI).isoformat(),
                        bytes=len(raw),elapsed_seconds=round(time.monotonic()-started,3))
            path.write_bytes(raw)
            save(meta,record)
        self.records.append({**record,"cache_hit":cached,"source_file":str(path)})
        return json.loads(raw),record


def master_table(exchange,rows):
    if not isinstance(rows,list):
        raise ValueError("MASTER_SCHEMA_CHANGED")
    result={}
    for row in rows:
        code=str(row.get("公司代號") if exchange=="TWSE" else row.get("SecuritiesCompanyCode",""))
        if not (len(code)==4 and code.isdigit() and not code.startswith("00")):
            continue
        if code in result:
            raise ValueError("DUPLICATE_MASTER_SYMBOL")
        result[code]=dict(symbol=code,exchange=exchange,
            name=row.get("公司簡稱") if exchange=="TWSE" else row.get("CompanyAbbreviation"),
            industry=row.get("產業別") if exchange=="TWSE" else row.get("SecuritiesIndustryCode"))
    if not result:
        raise ValueError("EMPTY_ORDINARY_SHARE_MASTER")
    return result


def parse_daily(exchange,payload,session,master,record):
    expected=session.replace("-","")
    if str(payload.get("date"))!=expected or str(payload.get("stat","")).upper()!="OK":
        raise ValueError("DAILY_SOURCE_DATE_OR_STATUS_MISMATCH:"+exchange+":"+session)
    name="證券代號" if exchange=="TWSE" else "代號"
    tables=[t for t in payload.get("tables",[]) if name in t.get("fields",[]) and "成交股數" in t.get("fields",[])]
    if exchange=="TPEX":
        tables=[t for t in tables if t.get("title")=="上櫃股票行情"]
    if len(tables)!=1:
        raise ValueError("DAILY_SOURCE_SCHEMA_CHANGED:"+exchange)
    table=tables[0]
    mapping={"symbol":name,"open":"開盤價" if exchange=="TWSE" else "開盤",
             "high":"最高價" if exchange=="TWSE" else "最高","low":"最低價" if exchange=="TWSE" else "最低",
             "close":"收盤價" if exchange=="TWSE" else "收盤","volume":"成交股數",
             "turnover_twd":"成交金額" if exchange=="TWSE" else "成交金額(元)"}
    index={k:table["fields"].index(v) for k,v in mapping.items()}
    bars,errors,seen=[],[],set()
    for row in table["data"]:
        code=str(row[index["symbol"]]).strip()
        if code not in master:
            continue
        try:
            if code in seen:
                raise ValueError("DUPLICATE_SOURCE_SYMBOL")
            seen.add(code)
            values={k:number(row[i],k in ("open","high","low","close")) for k,i in index.items() if k!="symbol"}
            if not values["volume"].is_integer():
                raise ValueError("SOURCE_VOLUME_NOT_INTEGER_SHARES")
            if not values["low"]<=min(values["open"],values["close"])<=max(values["open"],values["close"])<=values["high"]:
                raise ValueError("INVALID_SOURCE_OHLC")
            values["volume"]=int(values["volume"])
            bars.append(dict(symbol=code,exchange=exchange,date=session,**values,
                source=exchange+"_OFFICIAL_DAILY_BULK",source_sha256=record["sha256"],
                retrieved_at=record["retrieved_at"],price_basis="RAW_UNADJUSTED_UNVERIFIED_ACTIONS"))
        except (ValueError,TypeError,IndexError) as exc:
            errors.append(dict(symbol=code,exchange=exchange,date=session,reason=str(exc)))
    return bars,errors


def roc_date(value):
    y,m,d=str(value).split("/")
    return date(int(y)+1911,int(m),int(d)).isoformat()


def collect(folder,end,count=65,http=None,stopped=lambda:False):
    from .dataset_lock import dataset_lease
    with dataset_lease(folder):return _collect_locked(folder,end,count,http,stopped)


def _collect_locked(folder,end,count,http,stopped):
    started=time.monotonic()
    end=date.fromisoformat(end)
    now=datetime.now(TAIPEI)
    if end>now.date() or (end==now.date() and now.hour<18) or not 1<=count<=250:
        raise ValueError("REQUIRE_COMPLETED_END_AFTER_18_TAIPEI_AND_1_TO_250_SESSIONS")
    folder=Path(folder)
    folder.mkdir(parents=True,exist_ok=True)
    lock=folder/".collecting"
    save(lock,dict(lock_version=1,started_at=datetime.now(TAIPEI).isoformat()))
    try:
        http=http or PublicHTTP(folder/"sources",stopped=stopped)
        today=datetime.now(TAIPEI).date().isoformat()
        master={}
        for exchange,url in MASTERS.items():
            rows,_=http.get("master_"+exchange+"_"+today,url)
            for symbol,row in master_table(exchange,rows).items():
                if symbol in master:
                    raise ValueError("DUPLICATE_SYMBOL_ACROSS_MARKETS")
                master[symbol]=row
        days=[]
        cursor=end.replace(day=1)
        for _ in range(18):
            url="https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?"+urlencode(dict(date=cursor.strftime("%Y%m01"),response="json"))
            calendar_key="calendar_"+cursor.strftime("%Y%m")+"_"+str(end)
            if cursor.strftime("%Y%m")!=end.strftime("%Y%m"):
                # A cache captured mid-month is incomplete even after that month closes.
                closed_before=(cursor.replace(day=28)+timedelta(days=4)).replace(day=1)
                cached=[]
                for x in http.folder.glob('calendar_'+cursor.strftime('%Y%m')+'_*.json'):
                    if x.name.endswith('.meta.json'):continue
                    try:
                        if stamp(read(x.with_suffix('.meta.json'))['retrieved_at']).date()>=closed_before:cached.append(x)
                    except (ValueError,KeyError,OSError,TypeError):continue
                if cached:
                    calendar_key=sorted(cached)[-1].stem
            obj,_=http.get(calendar_key,url)
            if obj.get("stat")!="OK" or not isinstance(obj.get("data"),list):
                raise ValueError("OFFICIAL_CALENDAR_UNAVAILABLE")
            days += [roc_date(r[0]) for r in obj["data"] if roc_date(r[0])<=str(end)]
            if len(set(days))>=count:
                break
            cursor=(cursor-timedelta(days=1)).replace(day=1)
        days=sorted(set(days))[-count:]
        if len(days)!=count or (end-date.fromisoformat(days[-1])).days>7:
            raise ValueError("OFFICIAL_CALENDAR_OR_HISTORY_INCOMPLETE")
        bars,errors=[],[]
        for session in days:
            if stopped():raise RuntimeError('COLLECTION_STOPPED')
            for exchange in ("TWSE","TPEX"):
                if exchange=="TWSE":
                    url="https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?"+urlencode(dict(date=session.replace("-",""),type="ALLBUT0999",response="json"))
                else:
                    url="https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?"+urlencode(dict(date=session.replace("-","/"),response="json"))
                obj,record=http.get(exchange+"_"+session,url)
                subset={s:m for s,m in master.items() if m["exchange"]==exchange}
                found,rejected=parse_daily(exchange,obj,session,subset,record)
                bars.extend(found);errors.extend(rejected)
        if stopped():raise RuntimeError('COLLECTION_STOPPED')
        for name,fields,rows in (("bars.csv",FIELDS,bars),("sessions.csv",["date"],[dict(date=d) for d in days])):
            path=folder/name
            with path.with_suffix(".tmp").open("w",encoding="utf-8",newline="") as f:
                w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
            path.with_suffix(".tmp").replace(path)
        save(folder/"master.json",master)
        save(folder/"rejected.json",errors)
        manifest=dict(schema_version=1,symbols=sorted(master),start=days[0],end=days[-1],calendar_verified_through=str(end),rows=len(bars),
            rejected_rows=len(errors),universe="CURRENT_OFFICIAL_TWSE_TPEX_ORDINARY_SHARE_MASTERS_NOT_HISTORICAL_MEMBERSHIP",
            point_in_time_certified=False,corporate_actions_verified=False,publication_time_verified=False,
            calendar_source="TWSE_FMTQIK_COMPLETED_MARKET_SESSIONS",
            requests=http.records,http_requests=sum(not r["cache_hit"] for r in http.records),
            cache_hits=sum(r["cache_hit"] for r in http.records),additional_data_fee_twd=0,
            files={n:hashlib.sha256((folder/n).read_bytes()).hexdigest() for n in ("bars.csv","sessions.csv","master.json","rejected.json")},
            elapsed_seconds=round(time.monotonic()-started,3))
        save(folder/"manifest.json",manifest)
        return manifest
    finally:
        lock.unlink()
