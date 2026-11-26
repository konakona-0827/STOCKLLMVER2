"""Freeze selected funding rules from an existing checkout; no account mutation."""
from pathlib import Path
from contextlib import closing
import sqlite3
import subprocess

from .common import read,save,digest,protocol,stamp,output_path,decimal


def profile(code_root,output):
    code=Path(code_root).resolve();out=output_path(output)
    binding=read(code/'.trading-state.json')
    state=(code/binding['state_root']).resolve()
    config_file=state/'config/execution_live.json'
    config=read(config_file);policy=read(code/'opportunity_flow/policy.json')
    account=state/'data/analysis/market_history.sqlite3'
    with closing(sqlite3.connect(account.as_uri()+'?mode=ro',uri=True)) as db:
        cap=db.execute('SELECT capital_limit_cents FROM paper_account WHERE account_id=1').fetchone()
    if not cap or cap[0]<=0:raise ValueError('VERIFIED_AI_CAPITAL_REQUIRED')
    def git(*args):return subprocess.run(['git','-C',str(code),*args],check=True,capture_output=True,text=True).stdout.strip()
    keys=('max_order_twd','max_buy_qty','buy_cash_buffer_rate','min_buy_fee_reserve_twd',
          'minimum_commission_twd','min_available_to_buy_twd','max_daily_buy_twd')
    selected={k:config[k] for k in keys}
    if any(decimal(v)<0 for v in selected.values()):raise ValueError('INVALID_FUNDING_RULE')
    receipt=dict(at=stamp(),code_root=str(code),state_root=str(state),branch=git('branch','--show-current'),commit=git('rev-parse','HEAD'),
        execution_config_sha256=digest(config_file.read_bytes()),selected_rules=selected,strategy_cap_twd=str(decimal(cap[0])/100),
        capital_source='READ_ONLY_SQLITE_paper_account.capital_limit_cents',
        code_sha256={name:digest((code/name).read_bytes()) for name in ('execution/health_monitor.py','execution/auto_advice_executor.py',
            'execution/execution_store.py','opportunity_flow/policy.json')},
        commission_source_url=config.get('commission_assumption_source'),live_authorization_copied=False,
        broker_balance_not_assumed_verified=True)
    p=protocol();p.update(version='formal-llm-paper-v2-latest-funding',funding_mode='LATEST_BRANCH_CUMULATIVE',
        strategy_cap_twd=receipt['strategy_cap_twd'],min_available_to_buy_twd=str(config['min_available_to_buy_twd']),
        max_daily_buy_twd=str(config['max_daily_buy_twd']),max_order_twd=str(config['max_order_twd']),
        max_order_quantity=config['max_buy_qty'],minimum_commission_twd=str(config['minimum_commission_twd']),
        commission_rate=str(policy['commission_rate']),buy_cash_buffer_rate=str(config['buy_cash_buffer_rate']),
        min_buy_fee_reserve_twd=str(config['min_buy_fee_reserve_twd']),funding_receipt_sha256=digest(receipt),
        funding_source_branch=receipt['branch'],funding_source_commit=receipt['commit'])
    p['notes']+=['Wallet is simulated NT$100,000 per arm, not a claim about the real broker balance.',
        'AI cumulative cap, broker cash floor, daily cap and conservative funding reserve are independent; commission is separate.',
        'Legacy historical v1 protocol/results remain unchanged; this is a new paired forward experiment.']
    out.mkdir(parents=True,exist_ok=False)
    save(out/'funding_provenance.json',receipt);save(out/'protocol.json',p)
    return receipt
