"""Read-only ordering and plain-language status. Never changes trade decisions."""
import math
from .notifications import actionable
from .contracts import timestamp, TOKYO
from .field_guide import VALUES


def plain_code(value):
    extra={'SEARCH_FAILED':'尋標未完成','OBSERVATION_READY':'本輪資料整理完成','CONFIGURATION_REQUIRED':'模型設定尚未齊全',
        'UNCHANGED':'本輪狀態沒有變化','FIRST_SEEN':'首次觀察','PARTIAL':'部分完成'}
    return extra.get(value,VALUES.get(value, '待核對，詳見技術紀錄' if isinstance(value,str) and '_' in value else str(value)))


def _number(value, default=0):
    return value if type(value) in (int,float) and math.isfinite(value) else default


def operation(item, report):
    symbol=item['symbol'];manual=item.get('manual_action') or {};o=item.get('opportunity_decision') or {}
    decision=item.get('trade_decision') or {};plan=item.get('execution_plan') or {}
    held=(report.get('portfolio_snapshot') or {}).get('positions',{}).get(symbol) or manual.get('position') or {}
    action=manual.get('action',o.get('action',decision.get('conclusion','WAIT')))
    search=report.get('discovery',{}).get('search') or {}
    selected={s['symbol'] for s in search.get('selection',[]) if search.get('status')=='COMPLETED'}
    selected.update(report.get('desk',{}).get('selected_symbols',[]))
    eligible=not report.get('discovery') or symbol in selected or _number(held.get('quantity'))>0
    return action if eligible and actionable({'action':action,'position':held,'decision_brief':plan}) else None


STAGE_PRIORITY={'CATALYST_CONFIRMED':6,'SHORT_RECLAIM_READY':5,'SHORT_PULLBACK_READY':4,
    'SHORT_MOMENTUM_READY':4,'SUPPORT_RECLAIMED':4,'SUPPORT_STRENGTHENED':3,
    'SUPPORT_HELD':3,'SHORT_RECLAIM_TRIGGER':2,'EARLY_WATCH':1}


def stock_priority(item, report):
    symbol=item['symbol'];o=item.get('opportunity_decision') or {};t=item.get('technical') or {}
    search=report.get('discovery',{}).get('search') or {}
    selected={s['symbol']:s for s in search.get('selection',[]) if search.get('status')=='COMPLETED'}
    choice=selected.get(symbol);op=operation(item,report)
    if op in {'EXIT_REVIEW','REDUCE_REVIEW'}:
        group,label,why=0,'持倉操作提案','已有確認持倉，需要先檢視減碼或退出條件。'
    elif op:
        group,label,why=1,'有效買入提案','價位與正數建議金額齊全，仍須人工核對後自行決定。'
    elif item.get('data_status')=='REJECTED':
        group,label,why=7,'資料待修復','行情未通過檢查，不能據此判斷進場。'
    elif o.get('action')=='AVOID':
        group,label,why=6,'暫不進場','目前機會通道不支持進場。'
    elif choice:
        group,label,why=2,'本輪重點觀察',f'尋標員本輪第{choice.get("agent_order","—")}順位提交；不是買入批准。'
    elif o.get('candidate') or any(p.get('research_candidate') for p in item.get('patterns',[])):
        group,label,why=3,'型態潛力觀察','已有研究線索，按型態成熟度與研究優先分數排列。'
    elif o.get('volatility_watch'):
        group,label,why=4,'波動觀察','波動較大，但方向尚未確認。'
    else:
        group,label,why=5,'一般觀察','目前沒有足夠的短線進場線索。'
    stage=max((STAGE_PRIORITY.get(p.get('stage'),0) for p in item.get('patterns',[])),default=0)
    key=(group,_number((choice or {}).get('agent_order'),9999),-stage,-_number(t.get('priority_score')),symbol)
    return {'group':group,'label':label,'reason':why,'key':key}


def ranked_securities(report):
    return sorted(report.get('securities',[]),key=lambda s:stock_priority(s,report)['key'])


def stock_status(item, report):
    op=operation(item,report);o=item.get('opportunity_decision') or {};manual=item.get('manual_action') or {}
    if op=='EXIT_REVIEW':return '已有持倉，請優先評估退出'
    if op=='REDUCE_REVIEW':return '已有持倉，請優先評估減碼'
    if op:return '有條件買入提案，等待觸發與人工核對' if (item.get('execution_plan') or {}).get('trigger_required') else '有買入評估提案，請人工核對價位與金額'
    if item.get('data_status')=='REJECTED':return '行情資料未通過檢查，暫不判斷'
    if o.get('action')=='AVOID':return '目前條件不合，暫不進場'
    if item.get('agent_errors') or any(e.get('symbol')==item['symbol'] and e.get('status')=='ERROR' for e in report.get('desk',{}).get('events',[])):
        return '研究或反證回覆未完成，尚無有效交易提案'
    if _number((manual.get('position') or {}).get('quantity'))>0:return '已有持倉，暫無調整提案' if manual.get('action')=='HOLD' else '已有持倉，持有條件仍待核對'
    if o.get('research_status')=='REVIEWED':return '已完成雙方評估，進場條件尚未確認'
    group=stock_priority(item,report)['group']
    if group==2:return '本輪優先研究，雙方審查尚未齊全'
    if group==3:return '有型態線索，本輪尚未深入研究'
    if group==4:return '波動較大，方向尚未確認'
    return '繼續觀察，尚無進場條件'


def report_status(report):
    if not report:return {'title':'尚無分析報告','text':'請啟動掃描，或在交易時段手動分析。','tone':'waiting'}
    items=report.get('securities',[]);ops=[operation(s,report) for s in items]
    buy=sum(op in {'PROBE_BUY_REVIEW','ENTRY_REVIEW','CONSIDER'} for op in ops)
    exit_count=sum(op in {'EXIT_REVIEW','REDUCE_REVIEW'} for op in ops)
    search=report.get('discovery',{}).get('search') or {};errors=report.get('desk',{}).get('error_count',0)
    title=f'本輪有 {buy} 檔買入提案、{exit_count} 檔持倉調整提案' if buy or exit_count else '本輪沒有有效交易提案，繼續觀察'
    facts=[f'已整理 {len(items)} 檔資料']
    if search.get('status')=='FAILED':facts.append('尋標回覆未通過檢查，尚未完成三角色決策')
    elif search.get('status')=='COMPLETED':facts.append(f'尋標員提交 {len(search.get("selection",[]))} 檔深入研究')
    if errors:facts.append(f'研究／反證有 {errors} 筆回覆未完成或未通過檢查')
    if report.get('collection',{}).get('status')=='DEGRADED':facts.append('部分行情資料仍有缺口')
    facts.append('排序代表本輪先看順序，不是上漲機率')
    return {'title':title,'text':'；'.join(facts)+'。','tone':'action' if buy or exit_count else 'warning' if errors or search.get('status')=='FAILED' else 'waiting'}


def human_time(value):
    try:
        return timestamp(value).astimezone(TOKYO).strftime('%m/%d %H:%M')+'（日本）'
    except (ValueError,TypeError):return '尚未排定'


def runtime_status(scheduler, report, analysis=None):
    next_time=human_time(scheduler.get('next_run_jst'))
    if scheduler.get('running'):title='正在取得行情與進行三角色分析，請稍候'
    elif not scheduler.get('enabled'):title='自動掃描已暫停，不會開始新一輪'
    elif not scheduler.get('worker_alive'):title='掃描器未運行，請重新啟動程式'
    elif scheduler.get('market_open'):title=f'程式正常待命，下次掃描 {next_time}'
    else:title=f'目前休市或午休，程式待命；下次掃描 {next_time}'
    if scheduler.get('error'):title='上一輪執行遇到問題，請查看掃描紀錄；'+title
    summary=report_status(report)
    last=scheduler.get('last_result') or {};status=last.get('status')
    last_text={'COMPLETE':'上一輪已完成','DEGRADED':'上一輪已完成，但資料或研究仍有缺口','RUNNING':'這一輪分析尚在進行',
        'FAILED':'上一輪未完成，請查看技術紀錄','SKIPPED_NO_USABLE_DELAYED_QUOTES':'報價過期或缺少，這輪略過模型分析',
        'SKIPPED_MARKET_CLOSED':'午休或休市，未呼叫模型','SKIPPED_DUPLICATE_SLOT':'這個時點已分析過，沒有重複執行','SKIPPED_BUSY':'已有分析執行中，未重複啟動'}.get(status,'尚無執行紀錄')
    a=(analysis or {}).get('status')
    analyze_text='正在執行手動分析' if a=='RUNNING' else '手動分析未完成，請查看技術紀錄' if a=='FAILED' else '可在交易時段手動重新分析'
    return {'title':title,'report_title':summary['title'],'report_text':summary['text'],'last_text':last_text,
        'next_time':next_time,'analyze_text':analyze_text}
