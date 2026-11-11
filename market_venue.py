"""Exchange routing from verified discovery observations, never a symbol guess."""
import json
from pathlib import Path
from threading import RLock
from config import ROOT
from main import save_json

_lock=RLock()


def registry(root=ROOT):
    try:
        result=json.loads((Path(root)/'data/analysis/market_discovery/symbol_venues.json').read_text('utf-8'))
        return result if isinstance(result,dict) else {}
    except (OSError,ValueError):
        return {}


def record_tpex(root, rows, session):
    with _lock:
        result=registry(root)
        for row in rows:
            if row.get('venue')=='TPEx':
                result[row['symbol']]=dict(venue='TPEx',source_session=session,source='TPEX_COMPLETED_DAILY_AND_COMPANY_REGISTRY')
        save_json(Path(root)/'data/analysis/market_discovery/symbol_venues.json',result)


def oddlot_market(symbol):
    return 6 if registry().get(symbol,{}).get('venue')=='TPEx' else 5


def recovery_watchlist(result, session):
    """Research additions still require real market-6 quotes and every BUY guard."""
    rows=(result.get('weekly_recovery') or {}).get('candidates',[])[:6]
    for row in rows:
        if row['symbol'] not in result['symbols']:
            result['symbols'].append(row['symbol'])
            result['watchlist'].append(dict(symbol=row['symbol'],venue='TPEx',
                reason='完成日線跌後收復前高；核對零股回測與近端退出空間',
                trigger_kind='PULLBACK_TO_CLOSE',condition_price=row['reclaimed_high'],
                expires_session=session,required_next_checks=row['required_next_checks']))
    return result
