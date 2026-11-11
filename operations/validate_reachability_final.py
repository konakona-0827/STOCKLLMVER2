"""Post-withdrawal regression on already-seen data, explicitly NOT a new holdout."""
import argparse
import contextlib
import io
from pathlib import Path
import shutil

from operations import validate_target_reachability as study
from operations import validate_reachability_extension as extension

PREVIOUS=study.REPORT/'revision2'
OUT=study.REPORT/'final'
extension.EXT=OUT;extension.PRIMARY=PREVIOUS

def verified():
    prior=study.read(PREVIOUS/'freeze.json')
    for name,expected in prior['sources'].items():
        if study.sha(PREVIOUS/name)!=expected:raise ValueError('REUSED_SOURCE_CHANGED:'+name)
    study.REPORT=OUT
    if (OUT/'freeze.json').exists():
        frozen=study.verified_freeze()
        if frozen['program_sha256']!=study.sha(Path(__file__)):raise ValueError('FINAL_PROGRAM_CHANGED')

extension.parent_verified=verified

def freeze():
    verified();OUT.mkdir(parents=True,exist_ok=True)
    for name in ('validation_universe.json','universe_source.json','verified_calendar.json'):
        shutil.copy2(PREVIOUS/name,OUT/name)
    for name in ('protocol.json','evaluation_plan.json'):
        value=study.read(PREVIOUS/name)
        value['scope']='POST_WITHDRAWAL_DEVELOPMENT_REGRESSION_NOT_UNSEEN_ACCEPTANCE'
        value['decision']='Withdraw failed median target cap. Preserve original payoff scenarios as explicitly unproven research requirements, with past-range support evidence and independent entry confirmation'
        value['independent_opportunity_improvement_proven']=False
        study.save(OUT/name,value)
    study.freeze()
    frozen=study.read(OUT/'freeze.json')
    frozen.update(program_sha256=study.sha(Path(__file__)),
        reused_source_freeze_sha256=study.sha(PREVIOUS/'freeze.json'),
        scope='POST_WITHDRAWAL_DEVELOPMENT_REGRESSION_NOT_UNSEEN_ACCEPTANCE')
    study.save(OUT/'freeze.json',frozen)
    for name in frozen['code']:
        destination=OUT/'frozen_code'/name
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(study.ROOT/name,destination)

def prepare():
    extension.prepare()
    result=study.read(OUT/'probes.json')
    result['protocol']='POST_WITHDRAWAL_DEVELOPMENT_REGRESSION_KNOWN_STOCKS_AND_DATES'
    study.save(OUT/'probes.json',result)

def score():
    with contextlib.redirect_stdout(io.StringIO()):extension.score()
    result=study.read(OUT/'validation.json')
    result['scope']='POST_WITHDRAWAL_DEVELOPMENT_REGRESSION_NOT_UNSEEN_ACCEPTANCE'
    result['independent_opportunity_improvement_proven']=False
    result['limitations'].insert(0,'These stocks and outcomes were used to withdraw a failed cap; this reuse is development regression only.')
    study.save(OUT/'validation.json',result)
    print(result['scope'],result['comparison'],flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('freeze','prepare','score'))
    args=parser.parse_args();globals()[args.action]()
