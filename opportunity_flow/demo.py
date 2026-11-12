"""Synthetic controls establish behavior, never stand in for market performance."""
from __future__ import annotations

import csv
from datetime import date,timedelta
import hashlib
from pathlib import Path

from .common import save


def create(folder):
    folder=Path(folder)
    folder.mkdir(parents=True,exist_ok=False)
    days=[]
    day=date(2026,7,1)
    while len(days)<65:
        if day.weekday()<5:
            days.append(day.isoformat())
        day+=timedelta(days=1)
    rows=[]
    for symbol,exchange in (("1111","TWSE"),("2222","TPEX"),("3333","TWSE")):
        for i,day in enumerate(days):
            close=100+i*.04 if i<50 else 100+(i-50)*.3
            high=close+.6
            low=close-1.2
            if i==50:
                high=112
            if symbol=="3333":
                high,low,close=104,96,100+i*.01
            if symbol=="2222" and i==len(days)-4:
                continue
            rows.append(dict(symbol=symbol,exchange=exchange,date=day,open=close-.1,high=high,low=low,
                close=close,volume=10000000,turnover_twd=close*10000000,
                source="SYNTHETIC_CONTROL",source_sha256=hashlib.sha256(f"{symbol}/{day}".encode()).hexdigest(),
                retrieved_at="2026-11-12T00:00:00+00:00",price_basis="SYNTHETIC"))
    with (folder/"bars.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    with (folder/"sessions.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=["date"]);w.writeheader();w.writerows(dict(date=d) for d in days)
    save(folder/"manifest.json",dict(schema_version=1,symbols=["1111","2222","3333"],
        end=days[-1],
        universe="SYNTHETIC_CONTROLS_NOT_MARKET",point_in_time_certified=False,corporate_actions_verified=False,
        files={n:hashlib.sha256((folder/n).read_bytes()).hexdigest() for n in ("bars.csv","sessions.csv")}))
    next_day=date.fromisoformat(days[-1])+timedelta(days=1)
    while next_day.weekday()>=5:
        next_day+=timedelta(days=1)
    return next_day.isoformat()+"T08:30:00+08:00"
