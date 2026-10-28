"""Synthetic Japan-shaped scenarios, never evidence about real Japanese stocks."""
from .demo import demo_bundle as taiwan_demo


def demo_bundle():
    bundle = taiwan_demo()
    bundle.update(market="JP", universe_version="synthetic-japan-scenarios-v1")
    bundle["as_of"] = bundle["as_of"].replace("+08:00", "+09:00")
    bundle["calendar"].update(source="SYNTHETIC_NOT_JPX_CALENDAR", timezone="Asia/Tokyo", session_close="15:30")
    bundle["calendar"]["available_at"] = bundle["calendar"]["available_at"].replace("+08:00", "+09:00")
    bundle["market_context"]["available_at"] = bundle["as_of"]
    for item in bundle["securities"]:
        item.update(symbol=item["symbol"] + ".T", exchange="TSE", currency="JPY", turnover_basis="synthetic_close_times_volume")
        item["adjustment_as_of"] = item["adjustment_as_of"].replace("+08:00", "+09:00")
        dossier = item.get("dossier")
        if dossier:
            dossier["available_at"] = bundle["as_of"]
            dossier["events_reviewed_through"] = dossier["events_reviewed_through"].replace("+08:00", "+09:00")
        for bar in item["bars"]:
            bar["closed_at"] = bar["session"] + "T15:30:00+09:00"
            bar["available_at"] = bar["session"] + "T16:00:00+09:00"
            bar["volume"] *= 10
            bar["turnover_jpy"] = bar.pop("turnover_twd") * 10
    return bundle
