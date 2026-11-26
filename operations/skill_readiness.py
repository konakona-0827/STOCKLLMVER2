"""Nonmutating launch checks. Never logs in, calls a model, or sends an order."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from opportunity_flow.common import ROOT,policy,read
from opportunity_flow.live import doctor,resolve_account_root
from opportunity_flow.skill_profile import load_profile


def inspect(state_root=None,dataset=None):
    profile=load_profile();profile.verify()
    if not profile.enabled:raise ValueError('PRODUCTION_TRADING_SKILL_NOT_ENABLED')
    account=resolve_account_root(state_root);p=policy();config=read(account/'config/execution_live.json')
    with closing(sqlite3.connect((account/'data/analysis/market_history.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        capital=db.execute('SELECT capital_limit_cents FROM paper_account WHERE account_id=1').fetchone()
    manifest=Path(dataset or ROOT/'local_data/official_market_20261111')/'manifest.json'
    market=read(manifest) if manifest.is_file() else {}
    d=doctor(state_root)
    return dict(trading_skill=profile.metadata,base_branch='codex/new-system-tw-opportunity-flow',base_commit='1f3a56265a3440f7aad5ff00b7d67020772058ce',
        code_root=str(ROOT),account_state_root=str(account),original_executor_reused=True,
        structural_ready=d['ready'] and bool(capital),session_trade_ready=False,
        reason='LIVE_ENTRY_MUST_REFRESH_BROKER_QUOTES_ACCOUNT_AND_COMPLETED_DAILY_DATA',
        broker_login_tested=False,models_called=0,orders_sent=0,
        capital_limit_twd=capital[0]/100 if capital else None,
        minimum_cash_reserve_twd=config.get('min_available_to_buy_twd'),daily_buy_cap_twd=config.get('max_daily_buy_twd'),
        minimum_commission_twd=config.get('minimum_commission_twd'),min_buy_fee_reserve_twd=config.get('min_buy_fee_reserve_twd'),
        buy_cash_buffer_rate=config.get('buy_cash_buffer_rate'),planned_limits_enabled=config.get('planned_limits_enabled') is True,
        executor_enabled=config.get('enabled') is True,model=p['model'],dataset=str(manifest.parent),
        universe_symbols=len(market.get('symbols',[])),calendar_verified_through=market.get('calendar_verified_through'),
        live_authorization_changed=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--state-root');parser.add_argument('--dataset');args=parser.parse_args()
    result=inspect(args.state_root,args.dataset)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['structural_ready'] else 1)
