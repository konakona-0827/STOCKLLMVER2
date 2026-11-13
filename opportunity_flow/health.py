"""Read-only checks of broker evidence, SQLite ledgers and FLOW progress."""
from contextlib import closing
from datetime import datetime,date
from pathlib import Path
import sqlite3
from decimal import Decimal
from time import monotonic

from .common import TAIPEI, read, stamp

REMEDIATIONS={
    'execution_db':'保留帳本與備份，停止新增委託；先恢復可核實備份，再與券商核對，不刪除重建歷史。',
    'analysis_db':'保留歸屬／成本紀錄，依券商與已確認成交核對；成本不能核實時由使用者補入。',
    'research_db':'保留錯誤檔與研究紀錄；資料可讀後由原 FLOW 排程重新研究。',
    'broker':'重新執行券商對帳；逾時要求重啟時先等原 owner 結束，不另開連線或放行舊額度。',
    'inventory':'依券商新鮮完整庫存修正可核實差異；成本、人工歸屬或未確認成交不作推測。',
    'repair':'保留本次修正收據，再核對剩餘差異；仍未核實的交易繼續阻擋。',
    'quotes':'交易時段重新取得非試撮零股行情；未取得時阻擋送單，非交易時段等待開盤。',
    'orders':'沿用券商核對及原 ROD 到期處理；結果未明不重送，不把本地無成交當作未送單。',
    'buy_budget':'檢查券商可動用金額、交割、未結預留及資金上限；不為測試降低保留資金。',
    'flow':'需自動研究時按「啟動 FLOW」；錯誤時先修正原因。健康檢查不代啟動交易。',
    'dataset':'由既有 08:30／18:00 官方資料排程補齊；資料不全的股票繼續等待。',
    'research':'檢查資料、原排程及模型錯誤；沿用研究快取／退避，不硬產生 BUY。',
    'models':'檢查 CLI 登入、配額與錯誤；沿用退避，恢復後重審，不轉用付費 API。',
    'executor':'確認正式模式與原執行器設定；健康檢查不自動開啟停用的交易設定。',
    'timing':'檢查儲存空間及檔案權限；先以正式委託帳本／券商確認，不因缺計時檔重送。',
}


def inspect_health(account_root, state, dataset, snapshot, *, controller_status, controller_error='',
                   read_only=False, at=None, progress=None,reference_quotes=None):
    at = stamp(at or datetime.now(TAIPEI))
    root, state = Path(account_root), Path(state)
    rows = []
    progress = progress or (lambda label: None)
    def add(key, label, status, detail):
        rows.append(dict(key=key, label=label, status=status, detail=detail,
            remediation=REMEDIATIONS.get(key,'查看原始證據後重新檢查。') if status!='PASS' else '無需處理。'))
    def database(key, label, path, required=True):
        progress(label)
        if not path.exists():
            add(key, label, 'FAIL' if required else 'WARN', '帳本不存在' if required else '尚無研究帳本，首次研究後建立')
            return
        try:
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=3)) as db:
                deadline=monotonic()+3
                db.set_progress_handler(lambda: int(monotonic()>deadline),10000)
                answer = [r[0] for r in db.execute('PRAGMA quick_check')]
            add(key, label, 'PASS' if answer == ['ok'] else 'FAIL', 'SQLite 完整性通過' if answer == ['ok'] else '；'.join(answer))
        except (OSError, sqlite3.Error) as exc:
            add(key, label, 'FAIL', type(exc).__name__+': '+str(exc))
    database('execution_db', '正式交易帳本', root/'data/execution/live_execution.sqlite3')
    database('analysis_db', '持股歸屬／資金帳本', root/'data/analysis/market_history.sqlite3')
    database('research_db', 'FLOW 研究帳本', state/'flow.sqlite3', required=False)
    progress('核對券商與帳本證據')
    try:
        age = (at-stamp(snapshot['checked_at'])).total_seconds()
        verified = 0 <= age <= 120 and not snapshot.get('errors') and snapshot.get('status') != 'ERROR'
        add('broker', '券商查詢', 'PASS' if verified else 'FAIL',
            f'資料時間 {snapshot["checked_at"]}；年齡 {max(0,int(age))} 秒' if verified else '；'.join(snapshot.get('errors') or ['查詢未成功或結果已過期']))
    except (KeyError, TypeError, ValueError):
        add('broker', '券商查詢', 'FAIL', '缺少可驗證的券商對帳時間')
    mismatches = [r for r in snapshot.get('inventory_comparison', []) if r.get('status') != 'MATCH']
    inventory_ok = snapshot.get('broker_inventory_verified') is True and not mismatches and not snapshot.get('reconciliation_errors')
    add('inventory', '券商庫存與 AI 歸屬', 'PASS' if inventory_ok else 'FAIL',
        f'已核對 {len(snapshot.get("inventory_comparison", []))} 檔；差異 {len(mismatches)} 檔' if inventory_ok else '庫存未完成核對或存在帳本差異，請查看券商持倉對帳')
    receipt=snapshot.get('reconciliation_receipt') or {}
    if receipt.get('state')=='REPAIRED':
        add('repair','本地差異修正','WARN',f'本次已依券商修正 {len(receipt.get("position_repairs",[]))} 檔股數／歸屬與 {sum(bool(r.get("changed")) for r in receipt.get("fill_repairs",[]))} 筆成交；保留修正前後證據')
    elif receipt.get('state') in ('UNRESOLVED','BROKER_UNVERIFIED'):
        add('repair','本地差異修正','FAIL','券商證據不足或仍有差異；保留最後已確認帳本，不用本地估算放行交易')
    quotes=snapshot.get('position_quotes') or []
    held=any(int(r.get('broker_cash_qty') or 0)>0 for r in snapshot.get('inventory_comparison',[]))
    unusable=[]
    for quote in quotes:
        try:
            age=(at-stamp(quote['quote_timestamp'])).total_seconds()
            if quote.get('quote_status')!='LIVE' or quote.get('is_trial') is not False or not 0<=age<=120:unusable.append(quote.get('symbol','?'))
        except (KeyError,TypeError,ValueError):unusable.append(quote.get('symbol','?'))
    if reference_quotes is not None:
        held_symbols={r['symbol'] for r in snapshot.get('inventory_comparison',[]) if int(r.get('broker_cash_qty') or 0)>0}
        available={}
        for s,q in reference_quotes.items():
            try:
                price=Decimal(str(q['last_price']));day=date.fromisoformat(q['price_as_of'])
                if (s in held_symbols and q.get('quote_status')=='DAILY_CLOSE_REFERENCE' and q.get('display_only') is True
                        and q.get('execution_usable') is False and day<=at.date() and price.is_finite() and price>0):available[s]=q
            except (ValueError,TypeError,KeyError,ArithmeticError):continue
        missing=sorted(held_symbols-set(available))
        dates=sorted({str(q.get('price_as_of')) for q in available.values()})
        add('quotes','持股行情','WARN' if missing else 'PASS',
            f'盤外已取得 {len(available)}/{len(held_symbols)} 檔官方收盤參考價；交易日 '+('、'.join(dates) or '無持股')+
            ('；缺資料：'+'、'.join(missing) if missing else '。＊僅確認資料可取得，非盤中可執行報價。'))
        if not missing:rows[-1]['asterisk']=True
    elif snapshot.get('quote_check_status')=='OUTSIDE_TRADING_SESSION':
        add('quotes','持股行情','PASS','非交易時段，未要求盤中零股報價；保留最後行情時間')
    else:
        add('quotes','持股行情','WARN' if unusable or held and not quotes else 'PASS',
            '部分行情過期、試撮或尚未取得；送單前仍須取得有效報價' if unusable or held and not quotes else f'已核對 {len(quotes)} 檔行情' if held else '沒有持股需要查價；新交易仍在送單前取價')
    from execution.health_monitor import VERIFIED_BROKER_ORDER_STATUSES
    checks = snapshot.get('broker_order_reconciliation') or []
    uncertain = [r for r in checks if r.get('status') not in VERIFIED_BROKER_ORDER_STATUSES]
    unresolved = snapshot.get('unresolved_attempts') or []
    verified_orders={r.get('decision_id') for r in checks if r.get('status') in VERIFIED_BROKER_ORDER_STATUSES}
    unknown = any(r.get('status') == 'UNCONFIRMED' or not r.get('decision_id') or r['decision_id'] not in verified_orders for r in unresolved)
    add('orders', '未結／未確認委託', 'WARN' if uncertain or unknown else 'PASS',
        '存在待核對的委託結果；沿用原核對與防重送機制' if uncertain or unknown else f'未結紀錄 {len(unresolved)} 筆；沒有本次查出的未確認結果')
    try:amount=Decimal(str(snapshot.get('current_buy_budget_twd')));ready=snapshot.get('budget_status')=='READY' and amount.is_finite() and amount>0
    except ArithmeticError:ready=False
    add('buy_budget', '新增買進額度', 'PASS' if ready else 'WARN',
        '帳戶買進額度可用，仍須通過逐筆研究與風控' if ready else '暫停新增買進：'+'、'.join(snapshot.get('buy_block_reasons') or [snapshot.get('budget_status') or 'UNVERIFIED']))
    add('flow', '自動流程', 'FAIL' if controller_error else 'PASS' if controller_status == 'RUNNING' else 'WARN',
        controller_error or ('FLOW 運行中' if controller_status == 'RUNNING' else 'FLOW '+controller_status+'；健康檢查不會替你啟動交易'))
    progress('檢查資料及研究紀錄')
    try:
        from .data import load_dataset
        from .__main__ import refresh_due
        ds = load_dataset(dataset)
        target = refresh_due(at, read(Path(dataset)/'manifest.json'))
        add('dataset', '官方日線資料', 'WARN' if target else 'PASS',
            f'需更新資料至 {target}' if target else f'資料可讀；{len(ds["grouped"])} 檔、{len(ds["sessions"])} 個交易日')
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        add('dataset', '官方日線資料', 'WARN', '資料尚不可用：'+type(exc).__name__+': '+str(exc))
    try:
        latest = read(state/'latest.json') if (state/'latest.json').exists() else {}
        from scheduler import is_twse_oddlot_market_window
        market_open = is_twse_oddlot_market_window(at.timestamp())
        summary = latest.get('summary') or {}
        age = (at-stamp(summary['as_of'])).total_seconds() if summary.get('as_of') else None
        fresh = age is not None and 0 <= age <= 300
        add('research', '研究進度', 'PASS' if fresh or not market_open else 'WARN',
            ('最近數值研究 '+str(summary['as_of'])) if fresh else '目前非交易時段；等待下一個研究時段' if not market_open else '交易時段內尚無近期數值研究，請查看資料或模型工作狀態')
    except (OSError, ValueError, TypeError, KeyError) as exc:
        add('research', '研究進度', 'FAIL', '研究紀錄無法驗證：'+type(exc).__name__)
    research_db=state/'flow.sqlite3'
    if research_db.exists():
        try:
            with closing(sqlite3.connect(research_db.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
                exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='calls'").fetchone()
                calls=list(db.execute('SELECT role,status FROM calls ORDER BY id DESC LIMIT 20')) if exists else []
            last={}
            for role,status in calls:last.setdefault(role,status)
            failed=[role for role,status in last.items() if status!='VALID']
            add('models','近期模型工作','WARN' if failed else 'PASS',
                '最近工作失敗／退避：'+'、'.join(failed) if failed else f'檢查 {len(last)} 種角色的最後結果；本檢查不呼叫模型')
        except sqlite3.Error as exc:add('models','近期模型工作','FAIL','模型紀錄讀取失敗：'+type(exc).__name__)
    try:
        config = read(root/'config/execution_live.json')
        add('executor', '原執行器設定', 'PASS' if config.get('enabled') is True and not read_only else 'WARN',
            '原執行器已啟用；送單仍由正式流程負責' if config.get('enabled') is True and not read_only else '目前為只讀模式' if read_only else '原執行器未啟用')
    except (OSError, ValueError, TypeError) as exc:
        add('executor', '原執行器設定', 'FAIL', '設定讀取失敗：'+type(exc).__name__)
    try:
        path=root/'data/execution/latest_live_execution.json'
        execution=read(path) if path.exists() else {}
        timing=execution.get('execution_timing') or {}
        if not isinstance(timing,dict):raise ValueError('TIMING_SCHEMA')
        lost=bool(timing.get('write_errors')) or bool(timing.get('record_path') and not Path(timing['record_path']).exists())
        add('timing','執行計時紀錄','WARN' if lost else 'PASS',
            '計時檔寫入未完成，請保留原委託帳本並檢查儲存空間／權限' if lost else '最近執行已有分段計時' if timing else '尚無新版執行計時；交易提案發生後自動記錄')
    except (OSError,ValueError,TypeError,AttributeError):add('timing','執行計時紀錄','WARN','最近執行紀錄格式無法核實，請查看原始檔案')
    flow_test=None
    diagnostic=root/'data/execution/health_order_test/latest.json'
    if diagnostic.exists():
        try:
            from .health_order import checks
            probe=read(diagnostic)
            if not isinstance(probe,dict):raise ValueError('INVALID_HEALTH_TEST_REPORT')
            evidence=checks(probe,snapshot);rows.extend(evidence)
            submitted=next((r for r in evidence if r['key']=='diagnostic_submission'),{})
            if submitted.get('status')=='PASS':
                submitted.update(label='FLOW 送單路徑＊',asterisk=True,
                    remediation='＊固定假設決策經正式格式與原執行器取得真實券商收單證據；非模型研究績效。人工單不占 AI 配置／日額、不交由 AI 管理。')
                flow_test=dict(status='PASS',asterisk=True,tested_at=probe.get('checked_at'),
                    evidence='USER_CONFIRMED_ASSUMPTION_WITH_REAL_BROKER_ACK',
                    note=submitted['remediation'])
        except (OSError,ValueError,TypeError,KeyError,AttributeError):
            add('diagnostic_record','測試下單紀錄','WARN','診斷檔無法讀取；不能推定沒有送單。')
            rows[-1]['remediation']='保留正式委託帳本，先與券商核對；不要刪檔或重送診斷單。'
    status = 'FAIL' if any(r['status'] == 'FAIL' for r in rows) else 'WARN' if any(r['status'] == 'WARN' for r in rows) else 'PASS'
    return dict(checked_at=at.isoformat(), status=status, checks=rows, broker_checked_at=snapshot.get('checked_at'),
                reconciliation_receipt=receipt,
                flow_execution_test=flow_test,
                scope='BROKER_READS_LEDGER_INTEGRITY_AND_FLOW_OBSERVATIONS', order_submission_calls=0, cancellation_calls=0)
