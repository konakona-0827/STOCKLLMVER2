"""Read-only decision funnel and transport size audit; no API/credential reads."""
from pathlib import Path
from collections import Counter
from contextlib import closing
import argparse
import json
import sqlite3
from token_budget import compact


def audit(root, day):
    runs = []
    for folder in sorted((root/'data/analysis/runs').glob(day.replace('-', '')+'*')):
        def read(name):
            p = folder/name
            return json.loads(p.read_text(encoding='utf-8-sig')) if p.exists() else {}
        manifest, scan, selection, execution = [read(n) for n in (
            'run.json', 'quant_scan.json', 'selection_decision.json', 'execution_report.json')]
        scanned = scan.get('scanned', scan.get('results', []))
        if not scanned:
            scanned = next((v for k, v in scan.items() if isinstance(v, list) and
                            v and isinstance(v[0], dict) and 'eligible' in v[0]), [])
        request = read('openai_request.json')
        payload = request.get('input', {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        original = len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))
        reduced = len(json.dumps(compact(payload), ensure_ascii=False, separators=(',', ':')))
        runs.append(dict(run_id=folder.name, universe=manifest.get('universe_count'),
                         quotes=manifest.get('symbols_received'), scanned=len(scanned),
                         eligible=sum(r.get('eligible', False) for r in scanned),
                         excluded=dict(Counter(reason for r in scanned for reason in r.get('exclusion_reasons', []))),
                         sent=manifest.get('candidates_sent'), decisions=dict(Counter(
                             d.get('decision') for d in selection.get('decisions', []))),
                         warnings=dict(Counter(w for d in selection.get('decisions', []) for w in d.get('warnings', []))),
                         intents=execution.get('trade_intent_count'), execution_status=execution.get('status'),
                         execution_results=execution.get('results'),
                         transport_chars_before=original, transport_chars_after=reduced))
    databases = {}
    for file in [root/'data/analysis/market_history.sqlite3', root/'data/execution/live_execution.sqlite3',
                 root/'data/simulation/experiments.sqlite3']:
        if not file.exists():
            continue
        with closing(sqlite3.connect(file.resolve().as_uri()+'?mode=ro', uri=True)) as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            databases[str(file.relative_to(root))] = {t: db.execute('SELECT count(*) FROM "'+t.replace('"', '""')+'"').fetchone()[0]
                                                     for t in tables}
    return dict(day=day, runs=runs, databases=databases)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--day', default='2026-10-30')
    parser.add_argument('--root', default='.')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = audit(Path(args.root), args.day)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True))
