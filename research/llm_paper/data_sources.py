"""Official exchange master/calendar and independently cached completed daily bars."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timedelta
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import ssl
import subprocess
import shutil
from urllib.request import Request, urlopen

import certifi

from .common import stamp, digest, save, read, Market

MASTER_URL="https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
ETF_URL="https://openapi.twse.com.tw/v1/opendata/t187ap47_L"
CALENDAR_URL="https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?response=json&date=20260101"


def download(url):
    # Absolute wall timeout also bounds Windows proxy auto-discovery/DNS stalls.
    executable=shutil.which("curl.exe") or shutil.which("curl")
    if not executable:raise RuntimeError("CURL_REQUIRED_FOR_BOUNDED_OFFICIAL_FETCH")
    result=subprocess.run([executable,"--fail","--silent","--show-error","--location","--user-agent","Mozilla/5.0","--connect-timeout","8","--max-time","25",url],
                          capture_output=True,timeout=30)
    if result.returncode:raise RuntimeError("OFFICIAL_SOURCE_FETCH_FAILED_"+str(result.returncode))
    raw=result.stdout
    return raw,stamp()


class TableParser(HTMLParser):
    def __init__(self):super().__init__();self.rows=[];self.row=[];self.cell=None
    def handle_starttag(self,tag,attrs):
        if tag=="tr":self.row=[]
        if tag in ("td","th"):self.cell=[]
    def handle_data(self,data):
        if self.cell is not None:self.cell.append(data)
    def handle_endtag(self,tag):
        if tag in ("td","th") and self.cell is not None:self.row.append("".join(self.cell).strip());self.cell=None
        if tag=="tr" and self.row:self.rows.append(self.row)


def instrument_master(raw,symbols,etfs,received_at):
    parser=TableParser()
    text=raw.decode("big5",errors="replace") if b"charset=big5" in raw.lower() or b"charset=big-5" in raw.lower() else raw.decode("utf-8",errors="replace")
    if "有價證券" not in text:
        text=raw.decode("big5",errors="replace")
    parser.feed(text)
    result={}
    for row in parser.rows:
        if len(row)<6:continue
        match=re.match(r"^(\d{4,6}[A-Z]?)\s+(.+)$",row[0])
        if not match or match[1] not in symbols:continue
        symbol=match[1];cfi=row[5].strip()
        if row[3]!="上市" or (symbol not in etfs and not cfi.startswith("ES")) or (symbol in etfs and not cfi.startswith("C")):
            raise ValueError("INSTRUMENT_CLASS_OR_EXCHANGE_MISMATCH_"+symbol)
        result[symbol]=dict(symbol=symbol,name=match[2],isin=row[1],listed_at=row[2],exchange="TWSE",cfi=cfi,
                            asset_type="ETF" if symbol in etfs else "STOCK",source_url=MASTER_URL,received_at=received_at,source_sha256=digest(raw))
    if set(result)!=set(symbols):raise ValueError("OFFICIAL_MASTER_COVERAGE_INCOMPLETE")
    return result


def official_master(company_raw,etf_raw,symbols,etfs,received_at):
    companies=json.loads(company_raw);funds=json.loads(etf_raw)
    company_map={str(r.get("公司代號")):r for r in companies}
    fund_map={str(r.get("基金代號",r.get("證券代號",""))):r for r in funds}
    result={}
    for symbol in symbols:
        if symbol in etfs:
            row=fund_map.get(symbol)
            if row is None:raise ValueError("OFFICIAL_ETF_NOT_FOUND_"+symbol)
            result[symbol]=dict(symbol=symbol,name=row.get("基金簡稱",row.get("基金名稱")),exchange="TWSE",asset_type="ETF",
                                source_url=ETF_URL,received_at=received_at,source_sha256=digest(etf_raw))
        else:
            row=company_map.get(symbol)
            if row is None:raise ValueError("OFFICIAL_COMPANY_NOT_FOUND_"+symbol)
            result[symbol]=dict(symbol=symbol,name=row["公司簡稱"],exchange="TWSE",asset_type="STOCK",listed_at=row["上市日期"],
                                source_url=MASTER_URL,received_at=received_at,source_sha256=digest(company_raw))
    return result


def refresh(output,universe,as_of=None):
    out=Path(output);out.mkdir(parents=True,exist_ok=False)
    u=read(universe)
    as_of=as_of or datetime.now(Market().tz).date()
    clock=datetime.now(Market().tz)
    if as_of>clock.date()+timedelta(days=1) or as_of>clock.date() and clock.strftime("%H:%M")<"13:30":
        raise ValueError("UNCOMPLETED_FORWARD_DAILY_SESSION")
    if as_of.year!=2026:raise ValueError("VERIFIED_2026_CALENDAR_SCOPE_ONLY")
    with ThreadPoolExecutor(max_workers=3) as pool:
        a=pool.submit(download,MASTER_URL);b=pool.submit(download,CALENDAR_URL);c=pool.submit(download,ETF_URL)
        master_raw,master_at=a.result();calendar_raw,calendar_at=b.result();etf_raw,etf_at=c.result()
    (out/"master.raw.json").write_bytes(master_raw);(out/"calendar.raw.json").write_bytes(calendar_raw);(out/"etf.raw.json").write_bytes(etf_raw)
    master=official_master(master_raw,etf_raw,u["symbols"],set(u.get("etf_symbols",[])),max(master_at,etf_at))
    cal=json.loads(calendar_raw)
    if not cal.get("data") or int(cal.get("queryYear",0))!=2026:
        raise ValueError("OFFICIAL_CALENDAR_YEAR_OR_RESPONSE_INVALID")
    from daily_history import _parse_month, daily_trend
    from market_calendar import twse_closed_reason
    annual_closed={row[0] for row in cal["data"] if not any(word in row[1] for word in ("開始交易","最後交易"))}
    ordinary_closed=set();d=date(2026,1,1)
    while d.year==2026:
        if d.weekday()<5 and twse_closed_reason(d):ordinary_closed.add(d.isoformat())
        d+=timedelta(days=1)
    if ordinary_closed-annual_closed-{"2026-10-23"} or any(date.fromisoformat(d).weekday()<5 for d in annual_closed-ordinary_closed):
        raise ValueError("OFFICIAL_AND_LOCAL_CALENDAR_DISAGREE")
    # Existing parser validates OHLC, source, symbol and calendar dates; its cache
    # is directed to this isolated study, never the production daily-history tree.
    cache=out/"daily_cache";cache.mkdir()
    months=[];month=as_of.replace(day=1)
    for _ in range(3):months.append(month);month=(month-timedelta(days=1)).replace(day=1)
    def daily(symbol):
        bars=[];urls=[];errors=[];receipts=[]
        for month in months:
            url=f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={month:%Y%m%d}&stockNo={symbol}"
            try:
                raw,received=download(url)
                (cache/f"{symbol}_{month:%Y%m}.raw.json").write_bytes(raw)
                rows=_parse_month(json.loads(raw),symbol,month,received,url)
                bars.extend(rows);urls.append(url);receipts.append(received)
            except Exception as exc:errors.append(dict(month=month.isoformat(),error_type=type(exc).__name__))
        rows=sorted((b for b in bars if b["date"]<as_of.isoformat()),key=lambda b:b["date"])[-22:]
        from market_calendar import previous_trading_day
        latest=rows[-1]["date"] if rows else None
        return symbol,dict(bars=rows,trend=daily_trend(rows),status=dict(source="TWSE_DAILY",as_of=as_of.isoformat(),latest_date=latest,
            expected_latest_date=previous_trading_day(as_of).isoformat(),prior_complete_sessions=len(rows),source_urls=urls,
            latest_retrieved_at=max(receipts) if receipts else None,failed_months=errors,
            coverage="SUFFICIENT_FOR_20D_COMPARISON" if len(rows)>=21 and latest==previous_trading_day(as_of).isoformat() else "INCOMPLETE"))
    with ThreadPoolExecutor(max_workers=4) as pool:history=dict(pool.map(daily,u["symbols"]))
    sessions=[];d=date(2026,1,1)
    while d<=date(2026,12,31):
        if twse_closed_reason(d) is None:sessions.append(d.isoformat())
        d+=timedelta(days=1)
    result=dict(at=stamp(),as_of=as_of.isoformat(),instruments=master,daily=history,sessions=sessions,calendar_valid_year=2026,
                calendar_source_url=CALENDAR_URL,calendar_received_at=calendar_at,calendar_sha256=digest(calendar_raw),
                annual_schedule_backed_by_existing_emergency_closure_calendar=True,
                warnings=["DAILY_RAW_UNADJUSTED", "DAILY_BARS_ARE_NOT_ODDLOT_FILLS", "NEWS_AND_CORPORATE_ACTION_COMPLETENESS_UNVERIFIED"],
                daily_complete=sum(bool(x.get("bars")) for x in history.values()),point_in_time_forward_receipt_preserved=True)
    save(out/"sources.json",result)
    return result
