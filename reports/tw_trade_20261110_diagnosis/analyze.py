"""Reproduce the archived screening and arithmetic offline; never run an agent."""
from collections import Counter
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import types
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'reports/tw_trade_20261110_until_112203_Taipei'
OUTPUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def no_network(*args, **kwargs):
    raise RuntimeError('OFFLINE_DIAGNOSIS_NETWORK_DISABLED')


socket.socket.connect = no_network
socket.socket.connect_ex = no_network
socket.create_connection = no_network

# This report explains the archived runtime, even after the working branch is
# repaired. Reproduce its engine rather than silently using today's revision.
archived_engine = types.ModuleType('archived_diagnosis_engine')
exec(compile(subprocess.check_output(['git', 'show', '030d842:opportunity_engine.py'], cwd=ROOT).decode('utf-8'),
             '030d842:opportunity_engine.py', 'exec'), archived_engine.__dict__)
refresh, select = archived_engine.refresh, archived_engine.select
from quant_scanner import scan_quote
from trade_economics import evaluate_buy


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    manifest = read(SOURCE / 'manifest.json')
    integrity = {name: sha(SOURCE / name) == value
                 for name, value in manifest['files'].items()}
    assert all(integrity.values()), integrity
    policies = read(SOURCE / 'policies.json')
    live = policies['effective_live_config']
    search_policy = policies['code_config']['opportunity_search']
    rules = SimpleNamespace(
        max_buy_qty=live['max_buy_qty'],
        max_order_twd=Decimal(str(live['max_order_twd'])),
        min_buy_fee_reserve_twd=Decimal(str(live['min_buy_fee_reserve_twd'])),
        buy_cash_buffer_rate=Decimal(str(live['buy_cash_buffer_rate'])),
        economic_minimum_commission_twd=Decimal(str(live['minimum_commission_twd'])),
    )
    results = []
    arithmetic_checks = 0
    candidate_checks = 0
    sensitivity = []
    targets = []
    unique_researched = set()
    for folder in sorted((SOURCE / 'runs').iterdir()):
        run = read(folder / 'run.json')
        candidates = read(folder / 'candidates.json')
        market = read(folder / 'market.json')
        decisions = read(folder / 'decisions.json')
        account = run['account_snapshot']
        assert account['budget_status'] == 'READY'
        assert decisions['llm_validation'] == 'VALID'
        assert decisions['execution_budget']['pre_execution_buy_block_reasons'] == []
        assert all(r['decision'] == 'WAIT' for r in decisions['recommendations'])
        expected_pool = {c['symbol']: c for c in candidates['after_selection']}
        for stage in ['before_selection', 'final_research_set']:
            for candidate in candidates[stage]:
                reproduced = refresh(deepcopy(candidate), account, rules, search_policy)
                expected = expected_pool[candidate['symbol']] if stage == 'before_selection' else candidate
                assert reproduced['trade_setups'] == expected['trade_setups'], (folder.name, stage, candidate['symbol'])
                assert reproduced['opportunity_status']['blockers'] == expected['opportunity_status']['blockers']
                candidate_checks += 1
        for stage in ['after_selection', 'final_research_set']:
            for candidate in candidates[stage]:
                for setup in candidate['trade_setups']:
                    economics = evaluate_buy(candidate, qty=setup['max_qty'],
                        limit_price=setup['entry'], target_price=setup['target'],
                        invalidation_price=setup['invalidation'], minimum_commission_twd=1)
                    for key, value in economics.items():
                        assert setup[key] == value, (folder.name, candidate['symbol'], key)
                    arithmetic_checks += 1
        # Sensitivity scenarios are in-memory diagnostics, not proposed live settings.
        for name in ['archived_policy', 'profit_hurdle_1_twd', 'five_research_slots']:
            policy = deepcopy(search_policy)
            if name == 'profit_hurdle_1_twd':
                policy.update(minimum_target_net_profit_twd=1,
                              analysis_cost_budget_twd=1,
                              minimum_target_profit_to_analysis_cost=1)
            if name == 'five_research_slots':
                policy['max_research_candidates'] = 5
            _, audit = select(deepcopy(candidates['before_selection']), account, rules, policy)
            sensitivity.append(dict(run=folder.name, scenario=name,
                qualified=audit['qualified_symbols'], researched=audit['research_symbols'],
                watches=audit['watch_research_symbols']))
        for candidate in candidates['final_research_set']:
            unique_researched.add(candidate['symbol'])
            if not candidate['trade_setups']:
                continue
            setup = candidate['trade_setups'][0]
            target = candidate['daily_bars'][-1]['high'] if candidate['symbol'] in ('2377', '2376') else 61.2
            sources = [dict(date=b['date'], field=field) for b in candidate['daily_bars']
                       for field in ['high', 'low'] if b[field] == target]
            assert sources and setup['entry'] < target < setup['target']
            economics = evaluate_buy(candidate, qty=setup['max_qty'],
                limit_price=setup['entry'], target_price=target,
                invalidation_price=setup['invalidation'], minimum_commission_twd=1)
            targets.append(dict(run=folder.name, symbol=candidate['symbol'],
                entry=setup['entry'], qty=setup['max_qty'], stop=setup['invalidation'],
                original_target=setup['target'], original_rr=setup['reward_risk_ratio'],
                alternative_observed_level=target, level_sources=sources,
                alternative_rr=economics['reward_risk_ratio'],
                alternative_upside_twd=economics['estimated_net_upside_twd'],
                interpretation='NEARER_BARRIER_SENSITIVITY_NOT_AN_ASSERTION_EVERY_HIGH_IS_A_HARD_EXIT'))
        initial = candidates['after_selection']
        analyzed_symbols = {c['symbol'] for c in initial}
        quote_exclusions = []
        for quote in market['quotes']['rows']:
            if quote['symbol'] not in analyzed_symbols:
                scan = scan_quote(quote, policies['code_config']['scan_rules'],
                    datetime.fromisoformat(market['quotes']['request_completed_at']))
                quote_exclusions.append(dict(symbol=quote['symbol'], name=quote['name'],
                    price=quote['last_price'], bid=quote['bid'], ask=quote['ask'],
                    scan_exclusions=scan['exclusion_reasons'],
                    note='2449 is a configured manual holding outside the buy universe'
                         if quote['symbol'] == '2449' else None))
        trend_passes = []
        for c in initial:
            bars = c['daily_bars']
            assert c['daily_history_status']['missing_20d_trading_dates'] == []
            closes = [b['close'] for b in bars]
            ma5, ma20 = sum(closes[-5:]) / 5, sum(closes[-20:]) / 20
            if ma5 > ma20 and closes[-1] >= ma5 and closes[-1] >= closes[-6]:
                trend_passes.append(c['symbol'])
        scout = market['scout']
        results.append(dict(run=folder.name, counts=run['diagnostics']['counts'],
            scout={k: scout[k] for k in ['status', 'model_called', 'symbols', 'market_count', 'eligible_count', 'screen_count']},
            watches=[{k: w[k] for k in ['symbol', 'trigger_kind', 'condition_price', 'observed_price', 'status']}
                     for w in scout['watch_status']],
            qualified=market['entry_selection']['search']['qualified_symbols'],
            researched=market['entry_selection']['search']['research_symbols'],
            historical_trend_passes=trend_passes,
            blockers_in_initial_pool=dict(Counter(b for c in initial for b in c['opportunity_status']['blockers'])),
            quote_exclusions=quote_exclusions,
            support_verdicts=dict(Counter(c['review']['verdict'] for c in decisions['support_research']['calls'])),
            final_reasons=[dict(symbol=r['symbol'], reason=r['reason']) for r in decisions['recommendations']]))
    historical = ROOT / 'reports/october_expanded_20261109/competitive'
    attribution = read(historical / 'strategy_attribution.json')
    comparison = read(historical / 'comparison.json')
    history = dict(
        reference_outcomes=dict(Counter(r['outcome'] for r in attribution['reference_cases'])),
        reference_failures=attribution['failure_reasons'],
        families={f: dict(Counter(r['outcome'] for r in attribution['reference_cases'] if r['family'] == f))
                  for f in sorted({r['family'] for r in attribution['reference_cases']})},
        all_snapshot_buys={v: {k: val for k, val in comparison[v]['all_snapshot_buys'].items() if k != 'decisions'}
                           for v in ['baseline', 'revised']},
        scope=comparison['protocol'],
    )
    assert candidate_checks == 75
    assert arithmetic_checks == 17
    ledger = read(SOURCE / 'execution_ledger.json')
    assert ledger['execution_attempts'] == ledger['execution_events'] == []
    result = dict(scope='OFFLINE_ARCHIVED_EVIDENCE_ONLY_NO_MODEL_NO_BROKER',
        source_snapshot=str(SOURCE.relative_to(ROOT)), captured_until=manifest['captured_until'],
        runtime_commit=manifest['runtime_commit'], verified_snapshot_hashes=integrity,
        reproduced_candidate_checks=candidate_checks, reproduced_setup_arithmetic_checks=arithmetic_checks,
        unique_researched_symbols=sorted(unique_researched), runs=results,
        nearer_target_sensitivity=targets, screening_sensitivity=sensitivity, historical_evidence=history,
        source_code_sha256={name: hashlib.sha256(subprocess.check_output(['git', 'show', '030d842:'+name], cwd=ROOT)).hexdigest()
                            for name in ['opportunity_engine.py', 'trade_economics.py',
            'market_discovery.py', 'scheduler.py', 'config/market_discovery.json']})
    (OUTPUT / 'diagnostics.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(snapshot_hashes_verified=len(integrity), candidate_checks=candidate_checks,
        setup_arithmetic_checks=arithmetic_checks, researched_symbols=sorted(unique_researched),
        output=str(OUTPUT / 'diagnostics.json')), ensure_ascii=False))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
