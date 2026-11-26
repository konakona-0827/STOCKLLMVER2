"""Matched native CLI research smoke, no execution/account mutation."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path

from opportunity_flow.common import policy,read,save,digest
from opportunity_flow.review import CodexRunner,verify_packet
from opportunity_flow.skill_profile import load_profile


def compare(packet_file,output,model=None):
    packet=read(packet_file);verify_packet(packet)
    out=Path(output);out.mkdir(parents=True,exist_ok=False)
    selected=model or policy()['model'];calls=[];original=os.environ.get('FLOW_TRADING_SKILL')
    try:
        for arm,value in [('formal_baseline','off'),('formal_with_skill','on')]:
            os.environ['FLOW_TRADING_SKILL']=value;profile=load_profile();runner=CodexRunner(selected)
            for role in ('researcher','challenger'):
                try:
                    result=runner([packet],role);record=dict(arm=arm,role=role,status='VALID',**result)
                except Exception as exc:
                    lines=str(exc).splitlines();record=dict(arm=arm,role=role,status='ERROR',error_type=type(exc).__name__,
                        error=(lines[0] if lines else type(exc).__name__)[:220],usage=None,trading_skill=profile.metadata)
                calls.append(record);save(out/(arm+'-'+role+'.json'),record)
                if record['status']=='ERROR':break
            if calls[-1]['status']=='ERROR':break
    finally:
        if original is None:os.environ.pop('FLOW_TRADING_SKILL',None)
        else:os.environ['FLOW_TRADING_SKILL']=original
    summary=dict(at=datetime.now(timezone.utc).isoformat(),scope='FROZEN_REAL_INPUT_NATIVE_MODEL_COMPARISON_NOT_FORWARD_OR_PNL',
        packet_id=packet['packet_id'],packet_source=str(Path(packet_file).resolve()),packet_sha256=digest(packet),model=selected,
        same_packet_market_capital_cost_and_schema=True,packet_financial_policy=packet['policy'],calls=calls,
        known_input_tokens=sum((c.get('usage') or {}).get('input_tokens',0) for c in calls),
        known_output_tokens=sum((c.get('usage') or {}).get('output_tokens',0) for c in calls),
        unknown_usage_calls=sum(c.get('usage') is None for c in calls),additional_api_billed_usd=0,plan_credit_cost_unknown=True,
        native_calls_permitted_tools=False,orders_sent=0,real_broker_used=False,profitability_proven=False)
    save(out/'summary.json',summary)
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--packet',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--model');parser.add_argument('--llm',action='store_true',required=True,help='Consume current ChatGPT plan for four native calls')
    args=parser.parse_args();result=compare(args.packet,args.output,args.model)
    print(json.dumps(dict(scope=result['scope'],model=result['model'],known_input_tokens=result['known_input_tokens'],known_output_tokens=result['known_output_tokens'],
        calls=[dict(arm=c['arm'],role=c['role'],status=c['status'],elapsed_seconds=c.get('elapsed_seconds'),
            opinions=[dict(symbol=r['symbol'],verdict=r['verdict'],route=r['selected_route_id']) for r in c.get('answer',{}).get('opinions',[])],
            error=c.get('error')) for c in result['calls']]),ensure_ascii=False,indent=2))
