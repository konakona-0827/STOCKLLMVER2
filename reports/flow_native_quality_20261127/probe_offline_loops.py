"""Offline numeric/UI memory observations; no forced GC within either loop."""
import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from operations.flow_resources import sample_memory
from opportunity_flow.common import policy,read,stamp


def run(mode,iterations):
    samples=[];start=time.monotonic();count=None
    if mode=='numeric':
        from opportunity_flow.data import DatasetReader
        from opportunity_flow.pipeline import run_cycle
        dataset=DatasetReader(ROOT/'local_data/official_market_20261111').get()
        at=stamp('2026-11-13T10:00:00+08:00');p=policy()
        state=ROOT/'local_data/incident_20261127/numeric_state'
        for i in range(iterations):
            result=run_cycle(dataset,at+timedelta(seconds=i*60),p,state)
            if i%10==0 or i==iterations-1:
                samples.append(dict(iteration=i+1,**sample_memory(os.getpid())))
                print(i+1,samples[-1]['private_commit_bytes'],flush=True)
        count=result['summary']['symbols_observed'];manifest=dataset['manifest_sha256']
    else:
        from test_flow_dashboard import DashboardReuseTests
        from opportunity_flow.dashboard_service import project_research
        fixture=DashboardReuseTests('test_full_legacy_information_tabs_and_financial_controls_are_retained');fixture.setUp()
        try:
            for token in fixture.root.tk.call('after','info'):fixture.root.after_cancel(token)
            fixture.service.flow=read(ROOT/'local_data/incident_20261127/numeric_state/latest.json')
            for i in range(iterations):
                fixture.service.flow['summary']['as_of']=(stamp('2026-11-13T11:00:00+08:00')+timedelta(seconds=i)).isoformat()
                fixture.app.result=project_research(fixture.service.flow);fixture.app.render();fixture.root.update_idletasks()
                if i%50==0 or i==iterations-1:
                    samples.append(dict(iteration=i+1,**sample_memory(os.getpid())))
                    print(i+1,samples[-1]['private_commit_bytes'],flush=True)
            count=len(fixture.service.flow['candidates']);manifest=None
        finally:fixture.cleanup()
    return dict(mode=mode,iterations=iterations,symbols=count,dataset_manifest_sha256=manifest,
        python=sys.version.split()[0],elapsed_seconds=round(time.monotonic()-start,3),samples=samples,
        scope='OFFLINE_DEVELOPER_PROCESS_NO_BROKER_NO_MODEL_NO_ORDERS',
        limitation='Finite numeric/UI loop observation, not live SDK or a full trading-day certification.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--mode',choices=('numeric','ui'),required=True)
    parser.add_argument('--iterations',type=int,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();result=run(args.mode,args.iterations)
    args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
