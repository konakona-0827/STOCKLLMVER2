"""Frozen historical selection comparison and isolated Codex-CLI model replay.

prepare freezes only information available at each decision time. run never
opens outcome files or a broker/ledger. score independently opens later prices.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT/'reports/opportunity_quality_20261110'
BENCHMARK = ROOT/'reports/october_expanded_20261109/competitive'
SOURCE_FILES = ['entry_structure.py', 'opportunity_engine.py', 'opportunity_search_cycle.py',
    'agent_decision.py', 'analysis_service.py', 'market_discovery.py', 'trading_agent.py',
    'support_scout/trading.py', 'support_scout/agents.py', 'strategies/strategy_opportunity_v2.md',
    'config/opportunity_search.json', 'config/market_discovery.json', 'pretrade_policy.py',
    'trade_economics.py', 'codex_cli.py', 'operations/validate_search_revision.py']


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    path = Path(path);path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def offline_python():
    # Windows asyncio creates a loopback socket pair. Block external Python
    # connections, while the existing authenticated CLI owns model transport.
    for name in ['connect', 'connect_ex']:
        original = getattr(socket.socket, name)
        def guard(sock, address, original=original):
            if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1', 'localhost'):
                return original(sock, address)
            raise RuntimeError('HISTORICAL_REPLAY_EXTERNAL_PYTHON_NETWORK_DISABLED')
        setattr(socket.socket, name, guard)


def execution_rules():
    from execution.auto_advice_executor import LiveExecutionConfig
    return LiveExecutionConfig(minimum_commission_twd=Decimal(1), max_order_twd=Decimal(100000),
        max_daily_buy_twd=Decimal(30000), planned_limits_enabled=True)


def prepare(output):
    from opportunity_engine import select, policy
    old_source = subprocess.check_output(['git', 'show', '030d842:opportunity_engine.py'], cwd=ROOT).decode('utf-8')
    old = types.ModuleType('archived_opportunity_engine')
    exec(compile(old_source, '030d842:opportunity_engine.py', 'exec'), old.__dict__)
    baseline_policy = read(ROOT/'reports/tw_trade_20261110_until_112203_Taipei/policies.json')['code_config']['opportunity_search']
    benchmark = read(BENCHMARK/'benchmark.json')
    groups, comparison = [], []
    for group in benchmark['groups']:
        pool = deepcopy(group['selection_pool'])
        assert all(all(b['date'] < group['as_of'][:10] for b in c['daily_bars']) for c in pool)
        assert all(c['quote_timestamp'] <= group['as_of'] for c in pool)
        before, old_audit = old.select(deepcopy(pool), benchmark['account'], execution_rules(), baseline_policy)
        after, audit = select(deepcopy(pool), benchmark['account'], execution_rules(), policy())
        comparison.append(dict(group_id=group['group_id'], before=old_audit, after=audit))
        groups.append(dict(group_id=group['group_id'], as_of=group['as_of'], selection_pool=pool))
    source_hashes = {name: sha(ROOT/name) for name in SOURCE_FILES}
    save(output/'frozen.json', dict(protocol=benchmark['protocol'], account=benchmark['account'], groups=groups,
        source_hashes=source_hashes, baseline_commit='030d842', benchmark_sha256=sha(BENCHMARK/'benchmark.json'),
        policy=policy(), scope='KNOWN_OCTOBER_SEED_POOL_NOT_UNSEEN_OR_WHOLE_MARKET',
        no_future_bars=True, broker_access=False, executor_available=False))
    save(output/'selection_comparison.json', comparison)
    print(json.dumps(dict(groups=len(groups), candidate_snapshots=sum(len(g['selection_pool']) for g in groups),
        nonempty_before=sum(bool(c['before']['qualified_symbols']) for c in comparison),
        nonempty_after=sum(bool(c['after']['qualified_symbols']) for c in comparison))))


def run_models(output):
    from opportunity_engine import select
    from opportunity_search_cycle import run
    from support_scout.trading import SupportCoordinator
    from trading_agent import TradingTools, analyze, agent_model
    import daily_participation
    frozen = read(output/'frozen.json')
    for name, expected in frozen['source_hashes'].items():
        if sha(ROOT/name) != expected:
            raise ValueError('FROZEN_SOURCE_CHANGED:'+name)
    print(json.dumps(dict(runtime='codex-cli', model=agent_model(), billing_source='chatgpt-plan', api_fallback=False)), flush=True)
    progress_state = dict(status='READY', filled=dict(BUY=0, SELL=0), pending=dict(BUY=0, SELL=0), needed=['BUY', 'SELL'])
    for group in frozen['groups']:
        for name, expected in frozen['source_hashes'].items():
            if sha(ROOT/name) != expected:
                raise ValueError('FROZEN_SOURCE_CHANGED:'+name)
        result_path = output/'model_runs'/group['group_id']/'result.json'
        if result_path.exists():
            prior = read(result_path)
            if prior.get('frozen_sha256') != sha(output/'frozen.json'):
                raise ValueError('FROZEN_INPUT_CHANGED')
            if prior['response']['validation'] in ('VALID', 'NOT_CALLED'):
                continue
            raise ValueError('FAILED_REPLAY_REQUIRES_REVIEW_BEFORE_RETRY:'+group['group_id'])
        if (output/'stop.json').exists():
            break
        pool = deepcopy(group['selection_pool'])
        account = deepcopy(frozen['account'])
        rules = execution_rules()
        selected, selection = select(pool, account, rules, frozen['policy'])
        at = datetime.fromisoformat(group['as_of'])
        original = {c['symbol']: deepcopy(c) for c in pool}
        views = []
        def analyze_batch(batch, index):
            directory = result_path.parent/'rounds'/f'{index+1:03d}'
            directory.mkdir(parents=True, exist_ok=True)
            for candidate in batch:
                candidate.setdefault('position', dict(ai_managed_qty=0))
            initial = dict(analysis_set=deepcopy(batch), paper_account=deepcopy(account),
                daily_participation=dict(policy=dict(enabled=False)),
                previous_candidate_rejections=deepcopy(views),
                evaluation=dict(as_of=group['as_of'], external_search=False, executor_available=False))
            quote_keys = ('symbol', 'last_price', 'bid', 'ask', 'open', 'high', 'low', 'volume',
                          'quote_timestamp', 'quote_status', 'is_trial')
            def quote(symbol, previous):
                return {k: deepcopy(original[symbol][k]) for k in quote_keys if k in original[symbol]}
            context = TradingTools(batch, rules, group['group_id']+f':{index+1}', directory/'agent_tools.json',
                quote, lambda: deepcopy(account), lambda: [], initial_input=initial,
                search_request=dict(tools=[]), quote_batch_reader=lambda symbols, previous:
                    {s: quote(s, previous[s]) for s in symbols}, entry_skill_enabled=False)
            def observation(symbol, candidate, count, root):
                return dict(status='RAW_PRICE_OBSERVATION', technical=None,
                    daily_bars=deepcopy(candidate['daily_bars']),
                    history_status=candidate.get('daily_history_status', {}),
                    limitations=['CORPORATE_ACTIONS_UNVERIFIED', 'ONLY_FROZEN_PAST_DAILY_HISTORY'])
            context.support = SupportCoordinator(context, ROOT, read(ROOT/'config/support_agents.json'), observation)
            instructions = ('歷史凍結回放，唯一決策時點 '+group['as_of']+
                '；所有工具只回傳該時點資訊，外部搜尋停用，不按今天日期判過期，沒有下單器。'
                '正常獨立判斷，不為回放強制買進。\n'+(ROOT/'strategies/strategy_opportunity_v2.md').read_text('utf-8'))
            with patch('trading_agent.now', return_value=at), patch('support_scout.trading.now', return_value=at), \
                 patch('position_analyzer.now', return_value=at), \
                 patch.object(daily_participation, 'policy', return_value=dict(enabled=False)), \
                 patch.object(daily_participation, 'progress', return_value=dict(progress_state, date=at.date().isoformat(), timezone='Asia/Taipei')):
                decision, response = analyze(context, instructions, model=agent_model())
            views.extend(dict(symbol=d['symbol'], reason=d['reason']) for d in decision['decisions'] if d['decision'] == 'WAIT')
            save(directory/'decision.json', decision)
            return decision, response
        decision, response, handoff = run(pool, selected, account, rules, frozen['policy'], analyze_batch,
            stopped=lambda: (output/'stop.json').exists())
        save(result_path, dict(group_id=group['group_id'], as_of=group['as_of'], selection=selection,
            all_candidates=pool, selected_symbols=[c['symbol'] for c in handoff], decision=decision, response=response,
            frozen_sha256=sha(output/'frozen.json'), broker_access=False, executor_available=False, orders_sent=False))
        print(json.dumps(dict(group=group['group_id'], validation=response['validation'],
            decisions=[(d['symbol'], d['decision']) for d in decision['decisions']]), ensure_ascii=False), flush=True)
        if response['validation'] == 'ERROR':
            save(output/'stop.json', dict(reason='MODEL_ERROR_REQUIRES_REVIEW', group=group['group_id']))
            break


def score(output, archive):
    from operations.expanded_opportunity_replay import route_outcome
    from operations.replay_archived_plans import load_daily
    from opportunity_engine import match_setup
    benchmark = read(BENCHMARK/'benchmark.json')
    labels = read(BENCHMARK/'labels.json')
    comparison = read(output/'selection_comparison.json')
    frozen = read(output/'frozen.json')
    quotes = defaultdict(list)
    for path in sorted((archive/'data/analysis/runs').glob('*/multi_market_snapshot.json')):
        for quote in read(path).get('quotes', []):
            quotes[quote['symbol']].append(quote)
    files = {p.parent.name: read(p) for p in (output/'model_runs').glob('*/result.json')}
    trades = []
    for group_id, result in files.items():
        for decision in result['decision']['decisions']:
            if decision['decision'] != 'BUY':
                continue
            candidate = next(c for c in result['all_candidates'] if c['symbol'] == decision['symbol'])
            setup = match_setup(candidate, decision)
            outcome = route_outcome(candidate, setup, quotes[candidate['symbol']],
                load_daily(BENCHMARK/'sources', candidate['symbol'])) if setup else dict(status='INVALID_PLAN')
            trades.append(dict(group_id=group_id, as_of=result['as_of'], symbol=candidate['symbol'],
                decision=decision, setup=setup, outcome=outcome))
    events = []
    for event in benchmark['events']:
        identity = event['event_id'];truth = labels['labels'][identity]['reference']
        positive = truth == 'SUCCESS' or any(r['status'] == 'SUCCESS' for r in labels['labels'][identity]['routes'].values())
        result = files.get(event['source_run'])
        comparison_row = next(c for c in comparison if c['group_id'] == event['source_run'])
        captures = [t for t in trades if positive and t['symbol'] == event['symbol'] and
            t['outcome']['status'] == 'SUCCESS' and t['as_of'][:10] == event['as_of'][:10] and
            t['as_of'] >= event['as_of'] and match_setup(event['candidate'], t['decision'])]
        events.append(dict(event_id=identity, original_reference=truth, positive=positive,
            survives_new_screen=event['symbol'] in comparison_row['after']['qualified_symbols'],
            first_response_captured=any(t['group_id'] == event['source_run'] for t in captures),
            same_day_original_plan_captured=bool(captures), miss=positive and not captures,
            replay_available=result is not None))
    summary = dict(scope=frozen['scope'], frozen_sha256=sha(output/'frozen.json'),
        groups_expected=len(frozen['groups']), groups_completed=len(files),
        model_valid_groups=sum(r['response']['validation'] == 'VALID' for r in files.values()),
        no_model_needed_groups=sum(r['response']['validation'] == 'NOT_CALLED' for r in files.values()),
        original_reference_outcomes=dict(Counter(e['original_reference'] for e in events)),
        original_failures_removed=sum(e['original_reference'] == 'FAILURE' and not e['survives_new_screen'] for e in events),
        original_positives=sum(e['positive'] for e in events),
        first_response_captured=sum(e['first_response_captured'] for e in events),
        same_day_original_plan_captured=sum(e['same_day_original_plan_captured'] for e in events),
        misses=sum(e['miss'] for e in events), buy_outcomes=dict(Counter(t['outcome']['status'] for t in trades)),
        events=events, trades=trades,
        limitations=['Known small seed universe with one confirmed positive, not unseen data or whole-market recall.',
            'Entry uses preserved odd-lot asks; exits use regular-market daily proxies, not verified fills.',
            'No full-market scout snapshots are available for this October cohort.',
            'Fewer surviving plans alone does not establish an improvement in opportunity finding.'])
    save(output/'validation.json', summary)
    print(json.dumps({k:v for k,v in summary.items() if k not in ['events','trades','limitations']}, ensure_ascii=False))


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['prepare', 'run', 'score'])
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--archive-root', type=Path, default=ROOT.parent/'STOCKLLM')
    args = parser.parse_args()
    offline_python()
    if args.mode == 'prepare': prepare(args.output)
    elif args.mode == 'run': run_models(args.output)
    else: score(args.output, args.archive_root)
