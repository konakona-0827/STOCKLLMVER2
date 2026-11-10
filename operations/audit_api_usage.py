"""Deduplicate saved provider usage; no API access or credentials required."""
import argparse
from collections import Counter
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from pathlib import Path
import json

KEYS=('input_tokens','output_tokens','cached_input_tokens','cache_write_tokens','reasoning_tokens')
PRICING_URL='https://developers.openai.com/api/docs/models/gpt-6-sol'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def estimate_standard_sol(rows):
    total=Decimal(0)
    for row in rows:
        fresh=row['input_tokens']-row['cached_input_tokens']-row['cache_write_tokens']
        if fresh<0:raise ValueError('INPUT_CACHE_PARTITION_INVALID')
        multiplier=2 if row['input_tokens']>272000 else 1
        output_multiplier=Decimal('1.5') if multiplier==2 else Decimal(1)
        total+=(fresh*Decimal(2)+row['cached_input_tokens']*Decimal('.2')+
            row['cache_write_tokens']*Decimal('2.5'))*multiplier
        total+=row['output_tokens']*10*output_multiplier
    return str(total/1000000)


def audit(root,day='2026-11-09'):
    root=Path(root);date_key=day.replace('-','');unique={};copies=0;roles=Counter();models=Counter()
    for path in sorted(list(root.rglob('api_usage_requests.json'))+list(root.rglob('pretrade_usage_requests.json'))):
        legacy_day=date_key in str(path.relative_to(root))
        response=read(path.parent/'agent_response.json') if (path.parent/'agent_response.json').exists() else {}
        model=response.get('api',{}).get('model','UNKNOWN')
        for record in read(path):
            if record.get('observed_at_utc'):
                if datetime.fromisoformat(record['observed_at_utc']).astimezone(timezone(timedelta(hours=9))).date().isoformat()!=day:continue
            elif not legacy_day:continue
            record_model=record.get('model') or model
            identity=record.get('response_id')
            if not identity or not identity.startswith('resp_'):continue
            values={k:record.get(k,0) or 0 for k in KEYS}
            if any(type(v) is not int or v<0 for v in values.values()):raise ValueError('INVALID_USAGE_TOKEN_COUNT')
            source=str(path.relative_to(root)).replace('\\','/')
            if identity in unique:
                if any(unique[identity][k]!=values[k] for k in KEYS):raise ValueError('CONFLICTING_RESPONSE_USAGE:'+identity)
                unique[identity]['sources'].append(source);copies+=1
            else:
                unique[identity]=dict(response_id=identity,**values,role=record.get('role'),model=record_model,
                    actor=record.get('actor'),symbol=record.get('symbol'),
                    response_status=record.get('response_status'),sources=[source])
                roles[record.get('role')]+=1;models[record_model]+=1
    totals={key:sum(r[key] for r in unique.values()) for key in KEYS}
    totals.update(successful_responses=len(unique),total_tokens=totals['input_tokens']+totals['output_tokens'],
        uncached_nonwrite_input_tokens=totals['input_tokens']-totals['cached_input_tokens']-totals['cache_write_tokens'])
    unknown=[];inflight=[]
    for path in sorted(root.rglob('*.accounting.json')):
        record=read(path)
        if record.get('observed_at_utc'):
            if datetime.fromisoformat(record['observed_at_utc']).astimezone(timezone(timedelta(hours=9))).date().isoformat()!=day:continue
        elif date_key not in str(path.relative_to(root)):continue
        if record.get('status')=='REQUEST_FAILED_OR_CANCELLED':unknown.append(str(path.relative_to(root)).replace('\\','/'))
        elif record.get('status') in ('REQUEST_STARTED','OUTPUT_TRUNCATED_RETRYING'):inflight.append(str(path.relative_to(root)).replace('\\','/'))
    return dict(date=day,cutoff_utc=datetime.now(timezone.utc).isoformat(),
        scope='Task reports named with execution date; historical as_of clocks are not billing dates. Excludes other projects, live trading machine and Codex subscription usage.',
        totals=totals,unique_response_ids=len(unique),duplicate_copies_excluded=copies,
        by_role=dict(roles),by_model=dict(models),by_actor=dict(Counter(r.get('actor') or 'LEGACY_UNCLASSIFIED' for r in unique.values())),
        failed_or_cancelled_files=len(unknown),in_flight_files=len(inflight),
        failed_or_cancelled_evidence=unknown,
        estimated_usd=estimate_standard_sol(list(unique.values())) if set(models)=={'gpt-6-sol'} else None,
        estimated_usd_assuming_configured_gpt6_sol=estimate_standard_sol(list(unique.values())),
        model_unknown_responses=models.get('UNKNOWN',0),
        actual_billed_usd=None,pricing=dict(source=PRICING_URL,per_million=dict(input=2,cached_input=.2,cache_writes=2.5,output=10),
            assumption='Standard processing, no regional surcharge; per-request long context adjustment applied.',
            exclusions='Unrecorded failed-request usage, other projects and actual billing adjustments are not estimated.'),
        notes=['Input already includes cache reads and cache writes; do not add them again.',
            'Reasoning tokens are included in output; do not add them again.',
            'Failed calls without provider usage are unknown, not asserted to cost zero.'],responses=list(unique.values()))


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--reports',type=Path,default=Path('reports'))
    parser.add_argument('--date',default='2026-11-09');parser.add_argument('--output',type=Path,required=True);a=parser.parse_args()
    result=audit(a.reports,a.date);a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('totals','duplicate_copies_excluded','by_model','failed_or_cancelled_files','estimated_usd','actual_billed_usd')},ensure_ascii=False))


if __name__=='__main__':main()
