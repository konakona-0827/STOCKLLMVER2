"""Summarize local execution timing without contacting a broker or model."""
import argparse
from pathlib import Path

from execution.timing import metrics,valid_record
from opportunity_flow.common import read,save


def summarize(folder,date=None):
    traces=[];errors=[]
    for path in sorted(Path(folder).glob('*.jsonl')):
        events=[];incomplete=False
        try:
            import json
            for line in path.read_text(encoding='utf-8-sig').splitlines():
                try:
                    event=json.loads(line)
                    if not valid_record(event):
                        incomplete=True;continue
                    events.append(event)
                except (ValueError,TypeError):incomplete=True
        except (OSError,UnicodeError) as exc:
            errors.append(dict(file=path.name,error=type(exc).__name__));continue
        if not events or date and not any(str(e.get('at','')).startswith(date) for e in events):continue
        decisions=sorted({e['decision_id'] for e in events if e.get('decision_id')})
        rows=[]
        for decision_id in decisions:
            chosen=[e for e in events if not e.get('decision_id') or e['decision_id']==decision_id]
            actual=[e for e in chosen if e.get('decision_id')==decision_id]
            fills=[e for e in actual if e['stage']=='fill_price_observation']
            outcomes=[e for e in actual if e['stage']=='execution_result']
            rows.append(dict(decision_id=decision_id,symbol=actual[0].get('symbol'),side=actual[0].get('side'),
                metrics=metrics(chosen),sdk_call_started=any(e['stage']=='sdk_send' and e['phase']=='start' for e in actual),
                sdk_return_observed=any(e['stage']=='sdk_return' for e in actual),
                result_status=outcomes[-1].get('status') if outcomes else None,
                fill_observation=fills[-1] if fills else None))
        traces.append(dict(trace_id=events[0].get('trace_id'),file=path.name,started_at=events[0]['at'],last_event_at=events[-1]['at'],
            partial_record=incomplete,metrics=metrics(events),decisions=rows))
    return dict(source='LOCAL_EXECUTION_TIMING_ONLY',traces=traces,read_errors=errors,
        note='巢狀階段時間會重疊，不可直接加總。沒有 SDK 回傳或已核實成交價不代表沒有送單；仍以原券商對帳為準。')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root');parser.add_argument('--folder');parser.add_argument('--date');parser.add_argument('--output')
    args=parser.parse_args()
    from opportunity_flow.live import resolve_account_root
    folder=Path(args.folder) if args.folder else resolve_account_root(args.state_root)/'data/execution/timing'
    result=summarize(folder,args.date)
    if args.output:save(args.output,result)
    import json,sys
    sys.stdout.reconfigure(encoding='utf-8');print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
