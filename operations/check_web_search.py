"""Paid, advice-only search check using a saved candidate; never contacts broker."""
import argparse
import json
import sys
from config import ROOT, load_env, now, redact
from position_analyzer import build_request, analyze
from strategy_catalog import live_selection


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbol', default='2330')
    args = parser.parse_args()
    load_env()
    runs = sorted((ROOT/'data'/'analysis'/'runs').glob('*/openai_request.json'), reverse=True)
    candidate = None
    for path in runs:
        saved = json.loads(path.read_text(encoding='utf-8'))
        payload = json.loads(saved.get('input', '{}'))
        candidate = next((c for c in payload.get('analysis_set', [])
                          if c.get('symbol') == args.symbol), None)
        if candidate is not None:
            break
    if candidate is None:
        raise ValueError('NO_SAVED_CANDIDATE_FOR_SYMBOL')
    # Archived quotes are never promoted to current/live market data.
    candidate.update(quote_status='LAST_KNOWN', max_buy_qty=0,
                     position={'ai_managed_qty': 0},
                     paper_position={'qty': 0, 'cost_cents': 0},
                     warnings=['不是即時行情；唯讀搜尋測試使用舊快照，禁止交易。'])
    account = {'source': 'WEB_SEARCH_CHECK_NO_ORDERS', 'available_cash_twd': 0}
    request = build_request([candidate], account, strategy_prompt=live_selection()['prompt'])
    request['instructions'] += '\n這是唯讀搜尋驗證。請實際使用搜尋查證該公司近期公告，預算為零且行情過期，必須 WAIT。'
    directory = ROOT/'data'/'analysis'/'web_search_checks'/now().strftime('%Y%m%dT%H%M%S%f')
    directory.mkdir(parents=True)
    (directory/'openai_request.json').write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding='utf-8')
    decision, response = analyze([candidate], request)
    for name, record in [('openai_response', response), ('selection_decision', decision)]:
        (directory/f'{name}.json').write_text(redact(json.dumps(record, ensure_ascii=False, indent=2)), encoding='utf-8')
    audit = response.get('api', {}).get('web_search') or {}
    success = (response['validation'] == 'VALID' and audit.get('tool_calls', 0) > 0
               and bool(audit.get('sources'))
               and decision['decisions'][0]['decision'] == 'WAIT')
    print(json.dumps({'success': success, 'validation': response['validation'],
                      'web_search': audit, 'directory': str(directory),
                      'error': response.get('api', {}).get('error'),
                      'decision': decision}, ensure_ascii=False, indent=2))
    if not success:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
