"""Read-only live acceptance and allowlisted exports; never calls a broker or sends an order."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import sqlite3
from pathlib import Path
import subprocess
import sys
import threading
import time

from opportunity_flow.common import atomic_write_text,read,save,TAIPEI
from opportunity_flow.recovery import STAGES,code_revision,folder,money,runtime_fingerprint,safe_details,same_runtime_code
from operations.flow_resources import PRIVATE_LIMIT,pressure_reason

ROOT=Path(__file__).resolve().parents[1]


def epoch(value):
    try:
        if isinstance(value,str):
            value=datetime.fromisoformat(value)
            return value.timestamp() if value.tzinfo is not None else None
        return float(value) if type(value) in (int,float) and math.isfinite(value) else None
    except (OSError,ValueError,TypeError,OverflowError):return None


def native_inspection(root,dataset,state,*,headless=False,model=None,code_root=ROOT):
    command=['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',
        str(code_root/'operations/InstallOpportunityFlowRecovery.ps1'),'-StateRoot',str(root),
        '-Dataset',str(dataset),'-State',str(state),'-PythonPath',sys.executable,'-InspectOnly']
    if headless:command.append('-Headless')
    if model:command.extend(['-Model',model])
    try:
        result=subprocess.run(command,cwd=code_root,capture_output=True,timeout=25,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:return dict(inspection_error='WINDOWS_INSPECTION_FAILED')
        value=json.loads(result.stdout.decode('utf-8-sig'))
        if not isinstance(value,dict):raise ValueError('invalid native report')
        return value
    except (OSError,ValueError,subprocess.SubprocessError):
        return dict(inspection_error='WINDOWS_INSPECTION_UNAVAILABLE')


def public_native(value):
    value=value if isinstance(value,dict) else {}
    result={}
    for key in ('startup_verified','task_present','task_definition_verified','task_enabled',
                'shortcut_verified','conflicting_task','requires_logged_on_owner',
                'current_session_is_interactive','credentials_saved','automatic_force_kill'):
        if type(value.get(key)) is bool:result[key]=value[key]
    for key in ('task_trigger_count','task_last_result'):
        if type(value.get(key)) is int:result[key]=value[key]
    for key in ('inspected_at','boot_at','task_last_run_at'):
        if isinstance(value.get(key),str) and epoch(value[key]) is not None:
            result[key]=datetime.fromtimestamp(epoch(value[key]),timezone.utc).isoformat()
    triggers=value.get('effective_triggers')
    result['effective_triggers']=[s for s in (triggers if isinstance(triggers,list) else [])
                                 if s in ('BOOT','OWNER_LOGON','EVERY_MINUTE')]
    if value.get('inspection_error') in ('WINDOWS_INSPECTION_FAILED','WINDOWS_INSPECTION_UNAVAILABLE'):
        result['inspection_error']=value['inspection_error']
    return result


def assess(native,row,*,pid,run_id,launched_at,now,process_alive,revision,process_state,fingerprint=None,heartbeat_read_degraded=False):
    native=public_native(native)
    current=isinstance(row,dict) and row.get('pid')==pid and row.get('run_id')==run_id
    row=row if current else {}
    alive=bool(process_alive and current and epoch(row.get('at')) is not None and 0<=now-epoch(row['at'])<=180)
    stages={}
    raw_stages=row.get('stages') if isinstance(row.get('stages'),dict) else {}
    for stage,value in raw_stages.items():
        if stage not in STAGES or not isinstance(value,dict):continue
        first,last=epoch(value.get('first_at')),epoch(value.get('last_at'))
        if first is None or last is None or not launched_at<=first<=last<=now:continue
        count=value.get('count')
        if type(count) is not int or count<1:continue
        stages[stage]=dict(first_at=first,last_at=last,count=count,detail=safe_details(value.get('detail') or {}))
    events=[]
    raw_events=row.get('events') if isinstance(row.get('events'),list) else []
    for value in raw_events[-100:]:
        if not isinstance(value,dict) or value.get('stage') not in stages:continue
        at=epoch(value.get('at'))
        if at is not None and launched_at<=at<=now:
            events.append(dict(at=at,stage=value['stage'],**safe_details(value)))
    checked=epoch(row.get('account_checked_at'))
    account=bool(alive and row.get('account_verified') is True and not row.get('broker_read_blocked')
        and checked is not None and math.floor(launched_at)<=checked<=now and now-checked<=120
        and 'ACCOUNT_RECONCILED' in stages)
    loop=epoch(row.get('loop_at'))
    watch=bool(alive and row.get('controller_status')=='RUNNING' and loop is not None and 0<=now-loop<=120)
    same_code=same_runtime_code(revision,fingerprint,row.get('code_revision'),row.get('runtime_fingerprint'))
    inspected=epoch(native.get('inspected_at'))
    startup=bool(native.get('startup_verified') is True and inspected is not None and 0<=now-inspected<=180
                 and native.get('current_session_is_interactive') is True)
    automation=bool(account and watch and same_code and row.get('executor_enabled') is True and row.get('read_only') is False
        and row.get('desired_running') is True and process_state not in ('MANUALLY_PAUSED','STOPPED','INSTALLATION_FAILED'))
    cycle=stages.get('RESEARCH_CYCLE_COMPLETED',{})
    reconciled=stages.get('ACCOUNT_RECONCILED',{}).get('first_at',now+1)
    research=bool(cycle and cycle['last_at']>=reconciled)
    waiting='WAITING_RESEARCH_WINDOW' in stages and (not cycle or stages['WAITING_RESEARCH_WINDOW']['last_at']>cycle['last_at'])
    state='AWAITING_STARTUP_INSPECTION'
    if process_state in ('LAUNCH_FAILED','INSTALLATION_FAILED','MANUALLY_PAUSED','STOPPED','EXISTING_ACCOUNT_OWNER','RESTART_BACKOFF','RECOVERING','STALLED_OWNER_WAITING'):
        state=process_state
    elif native.get('inspection_error'):state='STARTUP_INSPECTION_UNAVAILABLE'
    elif inspected is not None and not startup:state='STARTUP_CONFIGURATION_BLOCKED'
    elif not alive:state='AWAITING_RUNTIME'
    elif not same_code:state='RUNNING_VERSION_UNVERIFIED'
    elif not account:state='AWAITING_FRESH_ACCOUNT'
    elif not automation:state='AWAITING_AUTOMATIC_CONTROLLER'
    elif startup:
        last_cycle=cycle.get('last_at',0)
        daily=stages.get('WAITING_DAILY_DATA',{}).get('last_at',0)
        error=stages.get('RESEARCH_ERROR',{}).get('last_at',0)
        window=stages.get('WAITING_RESEARCH_WINDOW',{}).get('last_at',0)
        state=('WAITING_RESEARCH_WINDOW' if waiting and window>=max(daily,error) else
               'WAITING_DAILY_DATA' if daily>max(last_cycle,error) else
               'RESEARCH_BLOCKED' if error>last_cycle else
               'AUTOMATION_ACTIVE' if research else 'AWAITING_RESEARCH_CYCLE')
    budget=row.get('budget') if isinstance(row.get('budget'),dict) else {}
    io=row.get('diagnostics') if isinstance(row.get('diagnostics'),dict) else {}
    diagnostics={}
    diagnostics['heartbeat_read_degraded']=bool(heartbeat_read_degraded)
    failures=io.get('heartbeat_write_failures')
    diagnostics['heartbeat_write_failures']=failures if type(failures) is int and failures>=0 else None
    for key in ('last_write_failed_at','last_write_succeeded_at'):
        value=epoch(io.get(key))
        diagnostics[key]=(datetime.fromtimestamp(value,timezone.utc).isoformat()
                          if value is not None and launched_at<=value<=now else None)
    return dict(state=state,checks=dict(startup_verified=startup,current_process_heartbeat=alive,
        running_version_verified=same_code,fresh_account_reconciled=account,watch_loop_running=watch,
        automatic_execution_enabled=automation,research_cycle_observed=research),
        windows=native,stages=stages,events=events,diagnostics=diagnostics,
        runtime_source_revision=row.get('code_revision') if current else None,
        account=dict(checked_at=datetime.fromtimestamp(checked,timezone.utc).isoformat() if checked is not None else None,
            budget_ready=row.get('budget_ready') is True,
            **{key:money(budget.get(key)) for key in ('cash_reserve_twd','ai_cap_twd','current_buy_budget_twd','daily_buy_cap_twd')}),
        execution_block_reasons=sorted({v['detail']['reason'] for k,v in stages.items()
                                       if k=='EXECUTION_BLOCKED' and 'reason' in v['detail']}),
        conditional_trading='ORIGINAL_RESEARCH_PRETRADE_AND_RISK_GATES_APPLY',
        order_evidence=dict(sent_events=stages.get('ORDER_SENT',{}).get('count',0),
                            fill_events=stages.get('FILL_CONFIRMED',{}).get('count',0),
                            unknown_events=stages.get('DISPATCH_UNKNOWN',{}).get('count',0)),
        position_management_observed='POSITION_MANAGEMENT' in stages)


def text_report(report):
    checks=report['checks']
    japan=datetime.fromisoformat(report['exported_at']).astimezone(timezone(timedelta(hours=9)))
    lines=['Opportunity Flow 實機執行檢查',f"匯出時間（日本）：{japan.isoformat()}",
        f"程式版本：{report['source_revision'] or '未能確認'}",f"狀態：{report['state']}"]
    labels={'startup_verified':'Windows 自動啟動設定','current_process_heartbeat':'目前程序心跳',
        'running_version_verified':'執行版本一致','fresh_account_reconciled':'本次啟動後新對帳',
        'watch_loop_running':'研究監看迴圈','automatic_execution_enabled':'原自動執行流程已啟動',
        'research_cycle_observed':'已觀測研究掃描'}
    lines.extend(f"{label}：{'已確認' if checks[key] else '尚未確認'}" for key,label in labels.items())
    lines.extend([f"有效啟動觸發：{', '.join(report['windows'].get('effective_triggers',[])) or '尚未確認'}",
        f"本次送單事件：{report['order_evidence']['sent_events']}；成交事件：{report['order_evidence']['fill_events']}",
        f"持股管理路徑：{'已觀測' if report['position_management_observed'] else '尚無執行事件'}",
        '交易仍須符合原研究、複核、資金、行情、持股及其他風控；啟動成功不代表已送單或成交。',
        '盤外自動等待研究／交易時段；日線更新或帳務／資料阻擋時持續留下狀態，不需要另執行研究腳本。',
        '此報告只匯出白名單欄位，不含券商帳號、憑證、私人路徑或模型原文。'])
    for key,label in (('cash_reserve_twd','最低保留額'),('ai_cap_twd','獨立 AI 資金上限'),
                      ('current_buy_budget_twd','最近對帳的新買可用額'),('daily_buy_cap_twd','單日買進上限')):
        lines.append(f"{label}：{report['account'].get(key) or '尚未確認'}")
    if report['execution_block_reasons']:lines.append('執行阻擋：'+', '.join(report['execution_block_reasons']))
    count=report.get('diagnostics',{}).get('heartbeat_write_failures')
    resources=report.get('resources') or {}
    if resources:
        lines.append(f"程序私人承諾記憶體：{resources.get('private_commit_bytes',0)/1024**2:.1f} MiB；正常交接上限 {PRIVATE_LIMIT/1024**2:.0f} MiB。")
        if resources.get('pressure_reason'):lines.append('資源異常：'+resources['pressure_reason']+'；僅請求正常退出，不強殺金融程序。')
    if count is not None:lines.append(f'本次重試後仍失敗的心跳寫入累計：{count}；歷史寫入錯誤不代表目前交易已失敗。')
    if report.get('diagnostics',{}).get('heartbeat_read_degraded'):
        lines.append('本次心跳讀取暫時失敗；保留上次心跳與對帳時間，待下次讀取，未刷新資料時效。')
    if report['order_evidence']['unknown_events']:
        lines.append(f"本次曾有 {report['order_evidence']['unknown_events']} 個委託回覆不明事件；不可解讀為未送單，須依原帳本重新對帳，不自動重送。")
    return '\n'.join(lines)+'\n'


class RuntimeAudit:
    def __init__(self,root,dataset,state,*,headless=False,model=None,code_root=ROOT,clock=time.time):
        self.root,self.dataset,self.state=Path(root),Path(dataset),Path(state)
        self.code_root,self.clock=Path(code_root),clock
        self.headless,self.model=headless,model
        self.revision=code_revision()
        self.fingerprint=runtime_fingerprint()
        self.native={};self.native_thread=None;self.native_at=-float('inf');self.last_saved=-float('inf')
        self.last_signature=None

    def _refresh_native(self):
        self.native=native_inspection(self.root,self.dataset,self.state,headless=self.headless,
                                      model=self.model,code_root=self.code_root)

    def publish(self,process_state,*,row=None,pid=None,run_id=None,launched_at=None,process_alive=False,force=False,heartbeat_read_degraded=False):
        now=self.clock()
        if now-self.native_at>=60 and (self.native_thread is None or not self.native_thread.is_alive()):
            self.native_at=now
            self.native_thread=threading.Thread(target=self._refresh_native,name='flow-windows-inspection',daemon=True)
            self.native_thread.start()
        report=assess(self.native,row or {},pid=pid,run_id=run_id,launched_at=launched_at or now,now=now,
                      process_alive=process_alive,revision=self.revision,process_state=process_state,
                      fingerprint=self.fingerprint,heartbeat_read_degraded=heartbeat_read_degraded)
        report.update(schema_version=1,source='LIVE_RUNTIME_OBSERVATION',source_revision=self.revision,
            exported_at=datetime.fromtimestamp(now,timezone.utc).isoformat(),
            supervisor_pid=__import__('os').getpid(),runtime_pid=pid,run_id=run_id,source_fingerprint=self.fingerprint)
        report['resources']=None
        report['decision_quality']=None
        try:
            quality=read(self.state/'decision_quality.json');quality_at=epoch(quality.get('as_of'))
            if quality_at is not None and 0<=now-quality_at<=3600:
                safe={k:v for k,v in quality.items() if k in ('symbols_observed','paired_research_candidates',
                    'research_pending','research_errors_or_backoff','deferred_candidates','data_blocked',
                    'model_call_errors','eligible_routes_above_descriptive_historical_p75') and type(v) is int and v>=0}
                report['decision_quality']=dict(as_of=quality['as_of'],**safe,market_opportunity_absence_verified=False)
        except (OSError,ValueError,TypeError,AttributeError):pass
        report['memory_diagnostics']=None
        try:
            detail=read(folder(self.root)/'memory_diagnostics.json')
            sampled=epoch(detail.get('at'))
            if (detail.get('pid')==pid and sampled is not None and 0<=now-sampled<=180
                    and detail.get('stage') in ('watch_loop','numeric_cycle_after','watch_closed',
                        'broker_health_before','broker_health_after','broker_quotes_before','broker_quotes_after',
                        'broker_execute_before','broker_execute_after','broker_shutdown_before','broker_shutdown_after')):
                safe={k:v for k,v in detail.items() if k in ('private_commit_bytes','working_set_bytes',
                    'python_allocated_blocks','private_delta_since_same_stage') and type(v) is int}
                native=detail.get('com') or {}
                safe['com']={k:v for k,v in native.items() if k in ('stock_records','bstr_freed','bstr_bytes_freed','sta_waits')
                    and type(v) is int and v>=0}
                report['memory_diagnostics']=dict(stage=detail['stage'],**safe)
        except (OSError,ValueError,TypeError,AttributeError):pass
        try:
            resource=read(folder(self.root)/'resources.json');sampled=epoch(resource.get('at'))
            if (resource.get('pid')==pid and resource.get('run_id')==run_id
                    and sampled is not None and 0<=now-sampled<=60):
                safe={key:value for key,value in resource.items() if key in (
                    'private_commit_bytes','working_set_bytes','available_commit_bytes','available_physical_bytes',
                    'total_physical_bytes','system_memory_load_pct') and type(value) is int and value>=0}
                if 'private_commit_bytes' in safe:
                    report['resources']=dict(**safe,pressure_reason=pressure_reason(safe),private_limit_bytes=PRIVATE_LIMIT,
                        sampled_at=datetime.fromtimestamp(sampled,timezone.utc).isoformat())
        except (OSError,ValueError,TypeError,AttributeError):pass
        signature=(report['state'],tuple(report['checks'].values()),len(report['events']),
                   tuple(report['order_evidence'].values()))
        if not force and now-self.last_saved<30 and signature==self.last_signature:return report
        self.last_saved,self.last_signature=now,signature
        try:
            target=self.code_root/'reports/opportunity_flow_runtime'
            from opportunity_flow.execution_audit import public_paths
            day=datetime.fromtimestamp(now,TAIPEI).date().isoformat()
            try:
                paths=public_paths(self.state,day)
                save(target/'decision_paths'/(day+'.json'),paths)
                report['decision_path_export']=dict(date=day,count=len(paths['paths']),truncated=paths['truncated'])
            except (OSError,ValueError,TypeError,AttributeError,sqlite3.Error):
                report['decision_path_export']=dict(error='DECISION_PATH_EXPORT_UNAVAILABLE')
            save(target/'latest.json',report)
            atomic_write_text(target/'latest.txt',text_report(report))
            if run_id:
                name=hashlib.sha256(str(run_id).encode()).hexdigest()[:24]
                save(target/'runs'/(name+'.json'),report)
        except OSError:
            report['export_error']='REPOSITORY_EXPORT_UNAVAILABLE'
        # The account copy survives a read-only/moved Git checkout, with the
        # same export observations as the value returned to callers.
        try:save(folder(self.root)/'runtime-audit.json',report)
        except OSError:report['export_error']='ACCOUNT_EXPORT_UNAVAILABLE'
        return report


def wait_for_startup(root,*,timeout=180,not_before=0,clock=time.time,pause=time.sleep):
    began=clock();report={}
    print('正在確認實機排程、新券商對帳及研究迴圈；背景流程會持續運作。',flush=True)
    while clock()-began<timeout:
        try:
            value=read(folder(root)/'runtime-audit.json')
            exported=epoch(value.get('exported_at'))
            if (exported is not None and exported>=not_before and same_runtime_code(value.get('source_revision'),
                    value.get('source_fingerprint'),code_revision(),runtime_fingerprint())):
                report=value
                if report.get('state') in ('AUTOMATION_ACTIVE','WAITING_RESEARCH_WINDOW','STARTUP_CONFIGURATION_BLOCKED',
                    'STARTUP_INSPECTION_UNAVAILABLE','MANUALLY_PAUSED','EXISTING_ACCOUNT_OWNER','RUNNING_VERSION_UNVERIFIED',
                    'STALLED_OWNER_WAITING'):break
        except (OSError,ValueError,TypeError,AttributeError):pass
        pause(2)
    print(json.dumps(dict(live_acceptance_state=report.get('state','AWAITING_RUNTIME'),
        checks=report.get('checks',{}),export_error=report.get('export_error'),
        report='reports/opportunity_flow_runtime/latest.txt',automatic_monitoring_continues=True),ensure_ascii=False),flush=True)
    return report
