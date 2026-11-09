"""Offline saved-run audit and context replay. No API or broker calls."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
from execution.auto_advice_executor import LiveExecutionConfig
from token_budget import decision_input

JST = timezone(timedelta(hours=9))


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def legacy_compact(value):
    """Reproduce v3 transport, which compacted only daily bars."""
    if isinstance(value, list):
        return [legacy_compact(v) for v in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if (key == 'daily_bars' and isinstance(item, list) and item
                and all(isinstance(b, dict) and set(b) == set(item[0]) for b in item)):
            columns = list(item[0])
            result[key] = dict(columns=columns, rows=[[b[c] for c in columns] for b in item])
        else:
            result[key] = legacy_compact(item)
    return result


def audit(root, day, encoder=None):
    measure = lambda value: len((encoder.encode if encoder else str)(
        json.dumps(value, ensure_ascii=False, separators=(',', ':'))))
    rules = LiveExecutionConfig.load(root/'config/execution_live.json')
    result = dict(date_jst=day, calculated_at_jst=datetime.now(JST).isoformat(), runs=[],
        scope='Saved trading app runs only; no account-wide billing access.',
        replay_measurement='o200k_base estimated tokens, not server billing tokens' if encoder else 'characters',
        input_tokens=0, output_tokens=0, missing_usage_runs=[], decision_counts={},
        price_assumption='gpt-6-sol Standard: uncached input $2/M, output $10/M; observed searches $0.01 each')
    decisions, search_calls = Counter(), 0
    api_input_tokens = api_output_tokens = api_search_calls = 0
    result['codex_plan_tokens'] = 0
    for directory in sorted((root/'data/analysis/runs').iterdir()):
        if not (directory/'run.json').is_file():
            continue
        run = read(directory/'run.json')
        if datetime.fromisoformat(run['started_at']).astimezone(JST).date().isoformat() != day:
            continue
        has_response=(directory/'agent_response.json').is_file()
        has_scout=(directory/'market_discovery.json').is_file()
        if not has_response and not has_scout:
            continue
        response = read(directory/'agent_response.json') if has_response else {}
        scout = read(directory/'market_discovery.json') if has_scout else {}
        if not has_response and not scout.get('model_called'):
            continue
        api = response.get('api') or {}
        usage = api.get('usage')
        missing = usage is None or api.get('usage_accounting_complete') is False
        if usage is None:
            # Only use child usage when no aggregate exists; never double count.
            child = [c.get('usage') or {} for c in (response.get('support_research') or {}).get('calls', [])]
            usage = {key: sum(u.get(key, 0) for u in child) for key in ('input_tokens', 'output_tokens')}
        # Scout dispatch precedes the decision run. Preserve its costs even if
        # later quote/history/model processing failed before aggregate writing.
        if scout.get('model_called') and 'market_scout_usage' not in api:
            scout_usage_path=directory/'market_scout_usage_requests.json'
            scout_rows=read(scout_usage_path) if scout_usage_path.is_file() else scout.get('usage_requests',[])
            usage={**usage,**{key:usage.get(key,0)+sum(u.get(key,0) for u in scout_rows)
                             for key in ('input_tokens','output_tokens')}}
            missing=not scout.get('usage_accounting_complete',False) or (has_response and missing)
        review_api=(response.get('pretrade_review') or {}).get('api') or {}
        review_usage=review_api.get('usage')
        review_path=directory/'pretrade_usage_requests.json'
        if review_usage is None and review_path.is_file():
            review_rows=read(review_path)
            review_usage={key:sum(r.get(key,0) or 0 for r in review_rows) for key in ('input_tokens','output_tokens')}
        review_accounting=directory/'pretrade_usage_requests.accounting.json'
        if review_accounting.is_file() and not read(review_accounting).get('complete'):
            missing=True
        for key in ('input_tokens', 'output_tokens'):
            result[key] += usage.get(key, 0)
            result[key] += (review_usage or {}).get(key,0)
        if missing:
            result['missing_usage_runs'].append(directory.name)
        calls = (api.get('web_search') or {}).get('tool_calls') or 0
        search_calls += calls
        plan = (api.get('billing_source') or scout.get('billing_source')) == 'chatgpt-plan'
        tokens = {key: usage.get(key,0)+(review_usage or {}).get(key,0)
                  for key in ('input_tokens','output_tokens')}
        if plan:
            result['codex_plan_tokens'] += sum(tokens.values())
        else:
            api_input_tokens += tokens['input_tokens']
            api_output_tokens += tokens['output_tokens']
            api_search_calls += calls
        decision = read(directory/'selection_decision.json') if (directory/'selection_decision.json').is_file() else {}
        counts = Counter(d['decision'] for d in decision.get('decisions', []))
        decisions.update(counts)
        row = dict(run_id=directory.name, input_tokens=usage.get('input_tokens'),
            output_tokens=usage.get('output_tokens'), usage_incomplete=missing,
            decisions=dict(counts), observed_search_calls=calls)
        if not (directory/'openai_request.json').is_file():
            result['runs'].append(row)
            continue
        request = read(directory/'openai_request.json')
        data = json.loads(request['input'])
        history = next((c['result'] for c in response.get('tool_calls',[]) if c['tool'] == 'get_trade_history'), [])
        account = next((c['result'] for c in response.get('tool_calls',[]) if c['tool'] == 'get_portfolio'), data['paper_account'])
        context = SimpleNamespace(initial_input=data, candidates={c['symbol']: c for c in data['analysis_set']},
                                  account=account, history=history, rules=rules)
        payload = decision_input(context)
        old_size = measure(dict(input=legacy_compact(data), read_only=bool(run.get('read_only'))))
        new_size = measure(dict(input=payload, read_only=bool(run.get('read_only'))))
        row['replay'] = dict(old_input_payload=old_size, new_input_payload=new_size,
            reduction_percent=round((1-new_size/old_size)*100, 2),
            screen_status_counts=dict(Counter(c['entry_screen']['status'] for c in payload['analysis_set'])),
            numerical_scenarios={c['symbol']: c['entry_screen']['scenarios'] for c in payload['analysis_set']
                                 if c['entry_screen']['scenarios']})
        result['runs'].append(row)
    result.update(total_tokens=result['input_tokens']+result['output_tokens'],
        token_cost_usd_uncached=(api_input_tokens*2+api_output_tokens*10)/1e6,
        observed_search_calls=search_calls, search_cost_usd_estimate=api_search_calls*.01,
        decision_counts=dict(decisions))
    result['total_cost_usd_estimate'] = result['token_cost_usd_uncached']+result['search_cost_usd_estimate']
    result['codex_billing_note'] = 'Codex ChatGPT plan tokens are excluded from API cost estimates; quota remaining is unavailable.'
    # UTC ledger times must be converted before defining the JST day.
    ledger = root/'data/execution/live_execution.sqlite3'
    if ledger.is_file():
        with sqlite3.connect(ledger.resolve().as_uri()+'?mode=ro', uri=True) as db:
            rows = db.execute('SELECT started_at,status FROM execution_attempts').fetchall()
        result['execution_attempt_statuses'] = dict(Counter(status for stamp, status in rows
            if datetime.fromisoformat(stamp).astimezone(JST).date().isoformat() == day))
    result['limitations'] = [
        'Input payload comparison excludes instructions, schemas, model reasoning, hosted search context and later tool results.',
        'Replay does not call a model and cannot establish future token bills, trading frequency or profitability.',
        'Cache read/write details are unavailable in legacy runs; estimates are not invoices.',
        'The finite entry screen failing is not proof that every possible trading plan is invalid.',
        'Pretrade independent review usage is added separately; specialist usage is included in successful SDK aggregates.']
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=CODE)
    parser.add_argument('--date', default=datetime.now(JST).date().isoformat())
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--tokenizer-path', type=Path)
    args = parser.parse_args()
    if args.tokenizer_path:
        sys.path.insert(0, str(args.tokenizer_path))
    try:
        import tiktoken
        encoder = tiktoken.get_encoding('o200k_base')
    except ImportError:
        encoder = None
    result = audit(args.root, args.date, encoder)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k not in ('runs', 'limitations')}, ensure_ascii=False))
