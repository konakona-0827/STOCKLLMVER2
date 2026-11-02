"""Loopback-only manual fills and report viewer. No orders or automatic fills."""
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
import json
from pathlib import Path
import secrets
import re
import threading
import time
from urllib.parse import urlsplit

from .contracts import DataError, TOKYO, strict_json_loads
from .notifications import DEFAULT_CONFIG, email_status, flush
from .portfolio import read_snapshot, record_fill
from .settings import ROOT
from .scan_schedule import SCHEDULE_VERSION
from .presentation import runtime_status
from .storage import atomic_json
from .desktop_browser import open_dashboard
from .manual_ui import PANEL
from .manual_desk import desk_state, save_draft, save_fill, error_message, confirmation_id, record_feedback

DESK_UI_VERSION = 9

LIVE_STATUS='''<section class="live-status" aria-label="程式目前狀態"><b id="live-title">正在確認程式狀態…</b><p id="live-report"></p><details><summary>本輪處理情形</summary><p id="live-detail"></p></details><button id="live-reload" hidden type="button" onclick="location.reload()">已有新報告，載入最新排序與結論</button><button id="live-update" hidden type="button"></button></section>'''


SCHEDULER_PANEL='''<section id="scan-control" style="background:#eaf4fa;padding:22px;border:2px solid #306e9b;border-radius:8px;margin:20px 0">
<h2>日股自動掃描控制台</h2><p>日本時間固定掃描：09:00、10:00、11:00、12:30、13:30、14:30、15:15。午休與休市跳過，不需Codex。</p>
<p id="service-status" role="status">尚未確認程式連線，正在檢查…</p><p id="report-freshness"></p>
<p id="scheduler-status" role="status">正在讀取掃描狀態…</p><p id="scheduler-last"></p>
<button id="scheduler-start">啟動自動掃描</button> <button id="scheduler-stop">停止自動掃描</button> <button id="scan-now">立即手動分析</button> <button id="view-latest">重新載入最新報告</button>
<p id="scheduler-operation" role="status"></p>
<p><a href="#decision-brief">看決策與建議金額</a> · <a href="#field-guide">看個股JSON中文說明</a> · <a href="#manual-desk">回報已完成成交</a></p>
<details><summary>第一次使用：如何啟動、確認運行與閱讀結果</summary>
<ol><li>在專案資料夾雙擊 Dashboard.cmd，保留啟動的終端視窗。也可在該資料夾執行 <code>python -X utf8 -m support_scout serve --port 8765 --open-browser</code>。</li>
<li>看到「程式連線正常」才表示服務有回應；再確認「掃描器運行中」及「自動掃描已啟用」。休市待命正常，與程式關閉不同。連線檢查每5秒更新，不使用模型API。</li>
<li>自動掃描若曾被停止，按「啟動自動掃描」。想現在重新分析可按「立即手動分析」；會使用API並可能寄有效操作通知，仍受交易時段限制。</li>
<li>先看報告快照時間及來源報價時間，再看決策總表。WAIT／WATCH、建議金額0都表示目前不新增買進；有參考價不等於買訊。</li>
<li>有效提案請核對買入類型、觸發價、進場上限、風險價、目標及金額；以券商報價重驗。完成實際交易後才在下方勾選確認成交，讓下一輪知道你的持倉。</li>
</ol><p>關閉瀏覽器不停止掃描；關閉程式終端、關機或睡眠會中斷服務。舊報告能開啟不代表程式還在運行。</p></details>
<p>WATCH不寄信；有效買入提案或持倉減碼／退出才寄。停止後不再開始新一輪，已執行中的分析會完成。</p>
<script>
async function refreshScheduler(){try{const s=await(await fetch('/api/state')).json();const a=s.scheduler;
const rt=s.runtime;
document.getElementById('live-title').textContent=rt.title;
document.getElementById('live-report').textContent=rt.report_title;
document.getElementById('live-detail').textContent=rt.report_text;
document.getElementById('live-reload').hidden=!s.report_as_of||s.report_as_of===htmlReportAsOf;
document.getElementById('analysis-status').textContent=rt.analyze_text;
if(s.active_dashboard&&s.active_dashboard.ui_version>pageUIVersion){
const target=new URL(s.active_dashboard.url);if(target.hostname==='127.0.0.1'){
if(!deskFormDirty&&!fillBusy){location.replace(target.href);return;}
const notice=document.getElementById('live-update');notice.hidden=false;notice.textContent='新版已開放。你有未儲存輸入，請先保留內容，再按此開啟新版。';notice.onclick=()=>location.replace(target.href);}}
document.getElementById('service-status').textContent='程式連線正常 · '+(a.worker_alive?'掃描器運行中':'掃描器未運行')+' · 狀態確認時間（日本）：'+s.server_time;
document.getElementById('report-freshness').textContent='目前頁面報告快照：'+(htmlReportAsOf||'尚無')+' · 最新完成報告：'+(s.report_as_of||'尚無')+(s.report_as_of&&s.report_as_of!==htmlReportAsOf?' · 有新報告，請按「重新載入最新報告」':'');
document.getElementById('scheduler-status').textContent=rt.title;
const last=a.last_result;const labels={COMPLETE:'完成',DEGRADED:'完成但有資料或研究缺口',RUNNING:'執行中',FAILED:'失敗',SKIPPED_MARKET_CLOSED:'午休或休市，已略過',SKIPPED_DUPLICATE_SLOT:'本時段已執行',SKIPPED_NO_USABLE_DELAYED_QUOTES:'延遲報價過期，已略過模型',SKIPPED_BUSY:'已有分析執行中'};
document.getElementById('scheduler-last').textContent=rt.last_text+' · 模型請求 '+(last&&last.model_calls||0)+' 次';
document.getElementById('scheduler-start').disabled=a.enabled&&a.worker_alive;document.getElementById('scheduler-stop').disabled=!a.enabled;
const manual=document.getElementById('analyze');if(manual)manual.disabled=a.running||s.analysis.status==='RUNNING';
document.getElementById('scan-now').disabled=a.running||s.analysis.status==='RUNNING';
}catch(e){document.getElementById('live-title').textContent='目前連不到程式，畫面是先前報告。請重新開啟 Dashboard.cmd。';document.getElementById('service-status').textContent='程式連線中斷，先前畫面可能已過期；請重新執行 Dashboard.cmd。';document.getElementById('scheduler-status').textContent='目前掃描狀態無法確認，請檢查程式是否運行。';for(const id of ['scheduler-start','scheduler-stop','scan-now'])document.getElementById(id).disabled=true;}}
async function schedulerAction(path,value){try{await postDesk(path,value);document.getElementById('scheduler-operation').textContent=path==='/api/analyze'?'已提交手動分析；完成後請載入最新報告。':'掃描設定已更新。';await refreshScheduler();}catch(e){document.getElementById('scheduler-operation').textContent='操作未完成：'+e.message;}}
document.getElementById('scheduler-start').onclick=()=>schedulerAction('/api/scheduler',{enabled:true});
document.getElementById('scheduler-stop').onclick=()=>schedulerAction('/api/scheduler',{enabled:false});
document.getElementById('scan-now').onclick=()=>schedulerAction('/api/analyze',{});
document.getElementById('view-latest').onclick=()=>location.reload();
document.addEventListener('DOMContentLoaded',()=>{refreshScheduler();setInterval(refreshScheduler,5000);});
</script></section>'''


class DeskServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    def __init__(self, port=8765, *, data_root=None, output_root=None, universe_path=None, observer=None,start_scheduler=False,session_runner=None):
        self.data_root = Path(data_root or ROOT / "local_data")
        self.output_root = Path(output_root or ROOT / "output" / "observations")
        self.universe_path, self.observer = universe_path, observer
        self.token = secrets.token_urlsafe(32)
        self.analysis = {"status": "IDLE"}
        self.retiring = False
        self.analysis_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), Handler)
        from .scheduler import ScanScheduler
        self.scheduler=ScanScheduler(self.data_root,output_root=self.output_root,universe_path=self.universe_path,runner=session_runner)
        if start_scheduler:self.scheduler.start()

    def server_close(self):
        if getattr(self,'scheduler',None):self.scheduler.close()
        super().server_close()

    def active_dashboard(self):
        fallback={'url':f'http://127.0.0.1:{self.server_port}/','ui_version':DESK_UI_VERSION}
        try:
            value=strict_json_loads((self.data_root/'dashboard_current.json').read_text(encoding='utf-8'))
            address=urlsplit(value['url'])
            if (value.get('project_root')==str(ROOT.resolve()) and type(value.get('ui_version')) is int
                and value['ui_version']>=DESK_UI_VERSION and address.scheme=='http' and address.hostname=='127.0.0.1'
                and address.port and 1024<=address.port<=65535 and address.path=='/' and not address.query
                and not address.fragment and not address.username and not address.password):return value
        except (OSError,ValueError,KeyError,TypeError):pass
        return fallback

    def latest(self):
        pointer = self.output_root / "latest.json"
        if not pointer.exists():
            return None, None
        info = strict_json_loads(pointer.read_text(encoding="utf-8"))
        path = Path(info["report"]).resolve()
        if not path.is_relative_to(self.output_root.resolve()) or path.name != "report.html":
            raise DataError("INVALID_LOCAL_REPORT_PATH")
        if not (path.parent / "committed.json").exists():
            raise DataError("REPORT_NOT_COMMITTED")
        return strict_json_loads((path.parent / "report.json").read_text(encoding="utf-8")), path

    def analyze(self):
        try:
            if self.observer is None:
                result=self.scheduler.run_now(rerun=True)
                self.analysis=result
                return
            from .observation import observe
            _, target, code = (self.observer or observe)(agents=True, universe_source="fixed", universe_path=self.universe_path,
                scan_limit=20, max_packets=4, max_calls=9, public_information=True, session_prices=True,
                db_path=self.data_root / "observations.sqlite3", portfolio_path=self.data_root / "manual_fills.json",
                output_root=self.output_root)
            self.analysis = {"status": "COMPLETE" if code == 0 else "DEGRADED", "report": str(target / "report.html"), "exit_code": code}
        except Exception as exc:
            self.analysis = {"status": "FAILED", "error": str(exc) if isinstance(exc, DataError) else type(exc).__name__}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value, code=200, *, html=False):
        raw = value.encode("utf-8") if html else json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def valid_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def do_GET(self):
        if not self.valid_host():
            return self.reply({"error": "LOOPBACK_HOST_REQUIRED"}, 403)
        try:
            path = urlsplit(self.path).path
            if path == "/":
                report, file = self.server.latest()
                html = file.read_text(encoding="utf-8") if file else '<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>日股決策與持倉</title><main><h1>Japan Support Desk</h1></main></html>'
                if report and file and (file.parent / 'input.json').exists():
                    # Re-render locally with current labels, without mutating archived artifacts or calling APIs.
                    from .reporting import render_html
                    html = render_html(report, strict_json_loads((file.parent / 'input.json').read_text(encoding='utf-8')))
                elif report and 'id="field-guide"' not in html:
                    from .field_guide import render_field_guide
                    html=html.replace('</main>',render_field_guide(report)+'</main>',1)
                panel = PANEL.replace("__TOKEN__", json.dumps(self.server.token)).replace("__ASOF__", json.dumps(report.get("as_of") if report else None)).replace('__UIVERSION__',str(DESK_UI_VERSION))
                if '<!--LIVE_STATUS-->' in html:html=html.replace('<!--LIVE_STATUS-->',LIVE_STATUS,1)
                else:html=html.replace('<main>','<main>'+LIVE_STATUS,1)
                # The live header already explains this round; keep the static summary in archived HTML only.
                html=html.replace('<section class="round-summary">','<section class="round-summary" hidden>',1)
                html=html.replace('http://127.0.0.1:8765/','/')
                if '<!--SCAN_CONTROL-->' in html:html=html.replace('<!--SCAN_CONTROL-->',SCHEDULER_PANEL,1)
                else:html=html.replace('<main>','<main>'+SCHEDULER_PANEL,1)
                # The controller script needs postDesk, declared in PANEL.
                if '<!--MANUAL_DESK-->' in html:
                    html=html.replace('<!--MANUAL_DESK-->',panel,1).replace('<p class="static-manual-note">成交回報與通知請開啟本機Dashboard服務。</p>','')
                else:html=html.replace('</main>',panel+'</main>')
                self.reply(html, html=True)
            elif path == "/api/state":
                report, _ = self.server.latest()
                now = datetime.now(TOKYO).isoformat()
                scheduler=self.server.scheduler.status()
                manual=desk_state(self.server.data_root,report,now)
                self.reply({"portfolio": manual['portfolio'],"manual_desk":manual,
                    "project_root":str(ROOT.resolve()), "server_time":now, "ui_version":DESK_UI_VERSION,
                    "analysis": self.server.analysis, "email": email_status(self.server.data_root / "email_settings.json", self.server.data_root / "notifications.sqlite3"),
                    "scheduler":scheduler,"runtime":runtime_status(scheduler,report,self.server.analysis),
                    "active_dashboard":self.server.active_dashboard(),"can_retire":True,
                    "report_as_of": report.get("as_of") if report else None,
                    "manual_actions": report.get("manual_actions", []) if report else []})
            else:
                self.reply({"error": "NOT_FOUND"}, 404)
        except Exception as exc:
            self.reply({"error": str(exc) if isinstance(exc, DataError) else "LOCAL_DASHBOARD_ERROR"}, 400)

    def do_POST(self):
        port = self.server.server_port
        if (not self.valid_host() or self.headers.get("Origin") not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
                or not secrets.compare_digest(self.headers.get("X-Desk-Token", ""), self.server.token)):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if 0 <= size <= 16384:
                    self.rfile.read(size)
            except (ValueError, OSError):
                pass
            return self.reply({"error": "LOCAL_REQUEST_TOKEN_REQUIRED","message":error_message('LOCAL_REQUEST_TOKEN_REQUIRED')}, 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 16384 or self.headers.get_content_type() != "application/json":
                raise DataError("INVALID_LOCAL_JSON_REQUEST")
            value = strict_json_loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise DataError("JSON_OBJECT_REQUIRED")
            path = urlsplit(self.path).path
            if path == "/api/fills":
                row = save_fill(self.server.data_root, value)
                response={"recorded": True, "counted_as_holding": row["confirmed"], "id": row["id"],
                    'confirmation_id':confirmation_id(row['id']),
                    'message':'成交已成功儲存，持倉與資金已更新；下一輪會使用你的實際持倉。' if row['confirmed'] else '已儲存為未確認紀錄，目前不計入持倉與資金。核對後勾選確認即可入帳。'}
                try:
                    report,_=self.server.latest()
                    response['manual_desk']=desk_state(self.server.data_root,report,datetime.now(TOKYO).isoformat())
                except Exception:
                    response['message']='成交紀錄已成功保存，但列表或資金摘要暫時無法更新。請保留此訊息並重新整理，不要新增同一筆。'
                record_feedback(self.server.data_root,status='SAVED',code='CONFIRMED' if row['confirmed'] else 'UNCONFIRMED',
                    message=response['message'],identifier=row['id'],symbol=row['symbol'])
                return self.reply(response)
            if path == '/api/fill-drafts':
                return self.reply(save_draft(self.server.data_root,value))
            if path == "/api/notifications/flush":
                return self.reply(flush(self.server.data_root / "email_settings.json", self.server.data_root / "notifications.sqlite3"))
            if path == "/api/analyze":
                with self.server.analysis_lock:
                    if self.server.retiring:return self.reply({'error':'DASHBOARD_UPDATING_OPEN_NEW_VERSION'},409)
                    if self.server.analysis["status"] == "RUNNING":
                        return self.reply({"error": "ANALYSIS_ALREADY_RUNNING"}, 409)
                    self.server.analysis = {"status": "RUNNING"}
                    threading.Thread(target=self.server.analyze, daemon=True).start()
                return self.reply({"status": "RUNNING"}, 202)
            if path == "/api/scheduler":
                if set(value)!={'enabled'}:raise DataError('ONLY_SCHEDULER_ENABLED_CAN_BE_CHANGED')
                with self.server.analysis_lock:
                    if self.server.retiring:return self.reply({'error':'DASHBOARD_UPDATING_OPEN_NEW_VERSION'},409)
                    self.server.scheduler.set_enabled(value['enabled'])
                return self.reply(self.server.scheduler.status())
            if path == '/api/retire':
                if value:raise DataError('RETIRE_BODY_MUST_BE_EMPTY')
                with self.server.analysis_lock:
                    if self.server.scheduler.status()['running'] or self.server.analysis['status']=='RUNNING':
                        return self.reply({'error':'ANALYSIS_RUNNING_CANNOT_RETIRE'},409)
                    if not self.server.scheduler.job_lock.acquire(blocking=False):
                        return self.reply({'error':'ANALYSIS_RUNNING_CANNOT_RETIRE'},409)
                    try:
                        self.server.scheduler.set_enabled(False)
                        self.server.retiring=True
                    finally:self.server.scheduler.job_lock.release()
                self.reply({'retiring':True})
                threading.Thread(target=self.server.shutdown,daemon=True).start()
                return
            return self.reply({"error": "NOT_FOUND"}, 404)
        except (BrokenPipeError,ConnectionResetError):
            return  # A lost response must not relabel an already committed fill as rejected.
        except Exception as exc:
            if urlsplit(self.path).path in {'/api/fills','/api/fill-drafts'}:
                request_value=value if isinstance(locals().get('value'),dict) else {}
                record_feedback(self.server.data_root,status='REJECTED',code=str(exc) if isinstance(exc,DataError) else type(exc).__name__,
                    message=error_message(exc) if isinstance(exc,DataError) else '操作未完成',identifier=request_value.get('id'),symbol=request_value.get('symbol'))
            return self.reply({"error": str(exc) if isinstance(exc, DataError) else "LOCAL_REQUEST_ERROR",
                'message':error_message(exc) if isinstance(exc,DataError) else '操作未完成，請保留輸入並重新整理後再試。'}, 400)


def serve(port=8765,*,open_browser=False,_migration_depth=0,_resume_enabled=None):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise DataError("INVALID_DASHBOARD_PORT")
    from urllib.request import urlopen,Request
    try:
        with urlopen(f'http://127.0.0.1:{port}/api/state',timeout=2) as response:state=json.load(response)
    except (OSError,ValueError):state=None
    if state is not None:
        if not isinstance(state,dict) or state.get('scheduler',{}).get('engine')!='PROGRAM_INTERNAL' or state.get('project_root')!=str(ROOT.resolve()):
            raise DataError('OLD_DASHBOARD_RUNNING_RESTART_REQUIRED')
        if state.get('ui_version',0)<DESK_UI_VERSION or state['scheduler'].get('schedule_version')!=SCHEDULE_VERSION:
            if _migration_depth>=16:raise DataError('TOO_MANY_OLD_DASHBOARDS_RESTART_REQUIRED')
            # Use the old app's normal CSRF-protected pause control, never terminate its process.
            if state['scheduler'].get('running'):raise DataError('OLD_DASHBOARD_ANALYSIS_RUNNING_TRY_AFTER_COMPLETION')
            with urlopen(f'http://127.0.0.1:{port}/',timeout=5) as response:old_html=response.read().decode('utf-8')
            match=re.search(r'const deskToken=("[^"\r\n]+")',old_html)
            if not match:raise DataError('OLD_DASHBOARD_PAUSE_CONTROL_UNAVAILABLE')
            req=Request(f'http://127.0.0.1:{port}/api/scheduler',data=b'{"enabled":false}',headers={
                'Content-Type':'application/json','Origin':f'http://127.0.0.1:{port}',
                'X-Desk-Token':json.loads(match.group(1))})
            with urlopen(req,timeout=5) as response:paused=json.load(response)
            if paused.get('enabled') is not False:raise DataError('OLD_DASHBOARD_NOT_PAUSED')
            resume=state['scheduler']['enabled'] or bool(_resume_enabled)
            if state.get('can_retire') is True:
                retire=Request(f'http://127.0.0.1:{port}/api/retire',data=b'{}',headers={
                    'Content-Type':'application/json','Origin':f'http://127.0.0.1:{port}',
                    'X-Desk-Token':json.loads(match.group(1))})
                with urlopen(retire,timeout=5) as response:retired=json.load(response)
                if retired.get('retiring') is not True:raise DataError('DASHBOARD_RETIRE_NOT_CONFIRMED')
                for _ in range(40):
                    time.sleep(.05)
                    try:
                        with urlopen(f'http://127.0.0.1:{port}/api/state',timeout=.2):pass
                    except OSError:break
                else:raise DataError('DASHBOARD_RETIRE_TIMEOUT')
                return serve(port,open_browser=open_browser,_migration_depth=_migration_depth+1,_resume_enabled=resume)
            print(f'Old automatic scanning paused; using dashboard port {port+1}.',flush=True)
            return serve(port+1,open_browser=open_browser,_migration_depth=_migration_depth+1,_resume_enabled=resume)
        if open_browser:
            open_dashboard(f'http://127.0.0.1:{port}/#scan-control')
        return
    try:server = DeskServer(port,start_scheduler=True)
    except OSError:raise DataError('DASHBOARD_PORT_IN_USE_CLOSE_OLD_VERSION_OR_USE_EXISTING_DASHBOARD') from None
    try:
        if _resume_enabled is not None:server.scheduler.set_enabled(_resume_enabled)
        atomic_json(server.data_root/'dashboard_current.json',{'url':f'http://127.0.0.1:{port}/',
            'ui_version':DESK_UI_VERSION,'project_root':str(ROOT.resolve())})
        if open_browser:
            open_dashboard(f'http://127.0.0.1:{port}/#scan-control')
        print(f"Japan Support Desk: http://127.0.0.1:{server.server_port}/#scan-control (internal scheduler; Ctrl+C to stop)",flush=True)
        server.serve_forever()
    finally:
        server.server_close()
