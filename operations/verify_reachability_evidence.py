"""Verify raw source snapshots and evidence without touching runtime files."""
import hashlib
from pathlib import Path
from operations.validate_target_reachability import ROOT,REPORT,read,save,sha


def run():
    results=[];errors=[]
    for directory in (REPORT,REPORT/'extension',REPORT/'revision2',REPORT/'final',REPORT/'corroborated'):
        frozen=read(directory/'freeze.json');code_count=0
        for name,expected in frozen['sources'].items():
            if sha(directory/name)!=expected:errors.append(str(directory/name))
        for name,expected in frozen['code'].items():
            choices=(directory/'frozen_code'/name,REPORT/'frozen_code'/name)
            if not any(p.is_file() and sha(p)==expected for p in choices):
                errors.append(str(directory/'frozen_code'/name))
            else:code_count+=1
        validation=read(directory/'validation.json')
        if validation['freeze_sha256']!=sha(directory/'freeze.json'):errors.append(str(directory/'validation.json:freeze'))
        if validation['probes_sha256']!=sha(directory/'probes.json'):errors.append(str(directory/'validation.json:probes'))
        models=list(directory.glob('development_agent*/result.json'))
        for path in models:
            record=read(path)
            if record['freeze_sha256']!=sha(directory/'freeze.json'):errors.append(str(path)+':freeze')
            if record['input_sha256']!=sha(path.parent/'input.json'):errors.append(str(path)+':input')
            if record['orders_sent'] is not False or record['broker_access'] is not False:errors.append(str(path)+':isolation')
        results.append(dict(report=str(directory.relative_to(ROOT)).replace('\\','/'),
            source_files=len(frozen['sources']),exact_code_snapshots=code_count,model_controls=len(models),
            expected_stock_days=validation['expected_stock_days'],rows=len(validation['rows'])))
    # Actual economic and order guards are unchanged from the integrated remote.
    quotes=read(REPORT/'quote_source_manifest.json')
    for name,expected in quotes['files'].items():
        if sha(REPORT/'quote_sources'/name)!=expected:errors.append('QUOTE_SOURCE_CHANGED:'+name)
    for path in REPORT.rglob('development_agent*/input.json'):
        for candidate in read(path)['analysis_set']:
            for row in candidate.get('market_history',[]):
                if row.get('evidence_file') and quotes['files'].get(row['evidence_file'])!=row['evidence_sha256']:
                    errors.append('QUOTE_REFERENCE_NOT_ARCHIVED:'+str(path))
    import subprocess
    unchanged={}
    for name in ('pretrade_policy.py','trade_economics.py','execution/auto_advice_executor.py',
                 'execution/risk_guard.py','config/execution_live.json','config/daily_participation.json'):
        baseline=subprocess.check_output(['git','show','cc22a1c:'+name],cwd=ROOT)
        current=(ROOT/name).read_bytes()
        unchanged[name]=current.replace(b'\r\n',b'\n')==baseline.replace(b'\r\n',b'\n')
        if not unchanged[name]:errors.append('ECONOMIC_GUARD_CHANGED:'+name)
    value=dict(status='PASS' if not errors else 'FAIL',reports=results,
               exact_historical_quote_snapshots=len(quotes['files']),
               unchanged_economic_and_order_guards=unchanged,errors=errors)
    save(REPORT/'integrity.json',value)
    print(value['status'],'reports',len(results),'errors',len(errors))
    if errors:raise ValueError(errors)


if __name__=='__main__':run()
