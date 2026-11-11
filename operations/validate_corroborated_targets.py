"""Untouched stocks for the replicated-evidence and within-symbol fallback fix."""
import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
import shutil

from operations import validate_target_reachability as study
from operations import validate_reachability_extension as extension
from operations import collect_reachability_history as collector

ROOT_REPORT=study.REPORT
OUT=ROOT_REPORT/'corroborated'
extension.EXT=OUT;extension.PRIMARY=ROOT_REPORT

def verified():
    study.REPORT=OUT
    if (OUT/'freeze.json').exists():
        frozen=study.verified_freeze()
        if frozen['program_sha256']!=study.sha(Path(__file__)):raise ValueError('VALIDATION_PROGRAM_CHANGED')
        if frozen['shared_program_sha256']!=study.sha(Path(extension.__file__)):raise ValueError('SHARED_PROGRAM_CHANGED')

extension.parent_verified=verified

def collect():
    verified();OUT.mkdir(parents=True,exist_ok=True)
    if (OUT/'freeze.json').exists():raise ValueError('ALREADY_FROZEN')
    protocol=study.read(ROOT_REPORT/'protocol.json')
    excluded=[r['symbol'] for p in (ROOT_REPORT,ROOT_REPORT/'revision2')
              for r in study.read(p/'validation_universe.json')['selections']]
    protocol['validation']['exclude_symbols']+=excluded
    protocol['validation']['entry_dates']=[extension.START,extension.END]
    protocol['validation']['outcomes_until']='2026-10-30'
    protocol['validation']['universe_rule']='20 equally spaced codes among remaining liquid ordinary shares, excluding all seed/scout and previous40 validation stocks'
    protocol['development']='Original seven failed buys and all previous40 validation stocks; previous studies retained and no longer treated as unseen'
    protocol['hypotheses']=[
        'Payoff requirements are not forecasts. Estimated targets need support in at least two non-overlapping completed historical ranges; a lone extreme is insufficient corroboration.',
        'The historical median is reference evidence, never an automatic ceiling.',
        'Review lower-ranked feasible routes after higher-ranked vetoes; cap approved and pending payloads after review.',
        'Keep first-day distinct LIVE entry confirmation and every original economic threshold unchanged.'
    ]
    protocol['candidate_method']['minimum_replicated_target_windows']=2
    protocol['candidate_method']['empirical_target_enabled']=False
    study.save(OUT/'protocol.json',protocol)
    plan=study.read(ROOT_REPORT/'revision2/evaluation_plan.json')
    plan.update(decision=extension.START+' through '+extension.END+', new fixed20 stocks excluded from every prior adjustment',
        revision='Minimum replicated target evidence plus within-symbol route fallback; original payoff/economic rules retained',
        prior_result='Earlier caps and evidence-only Agent controls failed; preserved as development, never relabeled unseen')
    study.save(OUT/'evaluation_plan.json',plan)
    shutil.copy2(ROOT_REPORT/'universe_source.json',OUT/'universe_source.json')
    collector.REPORT=OUT
    universe=collector.select_universe()['selections']
    jobs=[(item['symbol'],f'{year}{month:02d}') for item in universe
          for year,months in ((2025,range(1,13)),(2026,range(1,11))) for month in months]
    completed=[]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(collector.fetch,*job) for job in jobs]
        for future in as_completed(futures):
            completed.append(future.result())
            if len(completed)%50==0 or len(completed)==len(jobs):
                print('collected',len(completed),'of',len(jobs),flush=True)
    study.save(OUT/'collection_manifest.json',dict(jobs=len(jobs),results=completed))
    shutil.copy2(ROOT_REPORT/'extension/verified_calendar.json',OUT/'verified_calendar.json')
    shutil.copytree(ROOT_REPORT/'extension/market_sessions',OUT/'market_sessions',dirs_exist_ok=True)

def freeze():
    verified();study.freeze()
    frozen=study.read(OUT/'freeze.json')
    frozen.update(program_sha256=study.sha(Path(__file__)),shared_program_sha256=study.sha(Path(extension.__file__)),
        development_checkpoint='d3d7ec4',prior_studies_now_development=True)
    study.save(OUT/'freeze.json',frozen)
    for name in frozen['code']:
        destination=OUT/'frozen_code'/name
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(study.ROOT/name,destination)

def prepare():extension.prepare()
def score():extension.score()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('collect','freeze','prepare','score'))
    args=parser.parse_args();globals()[args.action]()
