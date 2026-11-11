"""New stock holdout after the first two failed cohorts became development data."""
import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
import shutil

from operations import validate_target_reachability as study
from operations import validate_reachability_extension as extension
from operations import collect_reachability_history as collector

PREVIOUS=study.REPORT
OUT=PREVIOUS/'revision2'
extension.EXT=OUT
extension.PRIMARY=PREVIOUS

def verified():
    study.REPORT=OUT
    if (OUT/'freeze.json').exists():
        frozen=study.verified_freeze()
        if frozen['revision_program_sha256']!=study.sha(Path(__file__)):
            raise ValueError('REVISION_PROGRAM_CHANGED')
        if frozen['extension_program_sha256']!=study.sha(Path(extension.__file__)):
            raise ValueError('SHARED_EVALUATOR_CHANGED')

extension.parent_verified=verified

def collect():
    verified();OUT.mkdir(parents=True,exist_ok=True)
    if (OUT/'freeze.json').exists():raise ValueError('ALREADY_FROZEN')
    protocol=study.read(PREVIOUS/'protocol.json')
    protocol['validation']['entry_dates']=[extension.START,extension.END]
    protocol['validation']['outcomes_until']='2026-10-30'
    protocol['validation']['exclude_symbols'] += [c['symbol'] for c in study.read(PREVIOUS/'validation_universe.json')['selections']]
    protocol['development']='Original seven failures plus the two failed studies on original20 holdout names; those studies are no longer unseen for revision2'
    protocol['candidate_method']['sampling']='Event-anchored past windows. Match completed breakout/volume first if >=8 disjoint observations; else explicit completed-trend fallback; no outcome-conditioned membership'
    protocol['hypotheses'] += ['Avoid fixed-grid phase bias; match the completed signal before choosing disjoint historical windows. Keep quantile=.5, min_windows=8, all economic thresholds and entry rules unchanged']
    protocol['validation']['universe_rule']='20 equally spaced codes among the remaining original liquid ordinary-stock universe, excluding all prior seed/scout and prior20 validation names'
    study.save(OUT/'protocol.json',protocol)
    plan=study.read(PREVIOUS/'extension/evaluation_plan.json')
    plan.update(decision=extension.START+' through '+extension.END+', new fixed20 stocks excluded from all prior adjustment',
        prior_result='Both prior cohorts failed opportunity-quality acceptance and are retained as development, not rebranded holdout',
        revision='Event-anchored completed-signal matching; unchanged price quantile, minimum sample count, financial gates and entry confirmation')
    study.save(OUT/'evaluation_plan.json',plan)
    shutil.copy2(PREVIOUS/'universe_source.json',OUT/'universe_source.json')
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
    shutil.copy2(PREVIOUS/'extension/verified_calendar.json',OUT/'verified_calendar.json')
    shutil.copytree(PREVIOUS/'extension/market_sessions',OUT/'market_sessions',dirs_exist_ok=True)

def freeze():
    verified();study.freeze()
    frozen=study.read(OUT/'freeze.json')
    frozen.update(revision_program_sha256=study.sha(Path(__file__)),
        extension_program_sha256=study.sha(Path(extension.__file__)),
        supersedes_failed_attempt_commit='129b09e',prior_studies_now_development=True)
    study.save(OUT/'freeze.json',frozen)

def prepare():extension.prepare()
def score():extension.score()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('collect','freeze','prepare','score'))
    args=parser.parse_args();globals()[args.action]()
