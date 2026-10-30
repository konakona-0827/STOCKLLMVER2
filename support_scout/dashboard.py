"""Loopback-only manual fills and report viewer. No orders or automatic fills."""
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit

from .contracts import DataError, TOKYO, strict_json_loads
from .notifications import DEFAULT_CONFIG, email_status, flush
from .portfolio import read_snapshot, record_fill
from .settings import ROOT


PANEL = '''<style>
#manual-desk{overflow-wrap:anywhere}#fill-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));gap:14px}
#fill-form label{display:flex;flex-direction:column;min-width:0}#fill-form input,#fill-form select{box-sizing:border-box;width:100%;max-width:100%;padding:9px}
#fill-form label:has(input[type=checkbox]){flex-direction:row;align-items:center;gap:8px}#manual-desk button{max-width:100%;white-space:normal;padding:9px}
</style><section id="manual-desk" style="background:#fff;padding:22px;margin:20px 0;border:2px solid #306e9b;border-radius:8px">
<h2>手動成交回報與持倉</h2><p>這裡只記錄你已在券商完成的交易；沒有下單能力。未勾選確認，不計入持倉。回報會供下一輪三Agent判斷使用。</p>
<form id="fill-form"><label>代號 <input name="symbol" placeholder="6857" required></label>
<label>方向 <select name="side"><option>BUY</option><option>SELL</option></select></label>
<label>股數（可填零股） <input name="quantity" type="number" min="0.00000001" step="any" required></label>
<label>每股成交價 JPY <input name="price" type="number" min="0.001" step="any" required></label>
<label>手續費 JPY <input name="fees" type="number" min="0" step="any" value="0"></label>
<label>成交日期時間 JST <input name="filled_at" type="datetime-local" required></label>
<label>固定失效價（選填） <input name="risk_lower" type="number" min="0.001" step="any"></label>
<label><input name="confirmed" type="checkbox" style="width:auto">我確認已實際成交</label>
<button id="fill-save">儲存成交回報</button></form><p id="fill-status" role="status"></p>
<h3>目前已確認持倉</h3><div id="positions"></div>
<p>成本只用於持倉記錄，不構成攤平或續抱理由。未提供原風險基準時，退出建議保持待確認。</p>
<button id="analyze">執行固定名單三Agent分析</button> <span id="analysis-status"></span> <button id="reload-report" hidden>查看新報告</button>
<h3>決策通知</h3><p id="mail-status"></p><button id="flush-mail">寄送待寄決策事件</button>
<p>同一決策狀態不重複通知。Gmail需本機寄信憑證，請使用 EmailSetup.cmd 設定，勿把密碼填入成交欄。</p>
</section>
<script>
const deskToken=__TOKEN__;
const htmlReportAsOf=__ASOF__;
let fillBusy=false;let pendingFillId=crypto.randomUUID();
async function postDesk(path,value){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Desk-Token':deskToken},body:JSON.stringify(value)});const x=await r.json();if(!r.ok)throw new Error(x.error||'操作未完成');return x;}
async function refreshDesk(){const s=await (await fetch('/api/state')).json();const d=document.getElementById('positions');d.replaceChildren();for(const p of Object.values(s.portfolio.positions)){const row=document.createElement('p');row.textContent=p.symbol+' · '+p.quantity+'股 · 均價 '+p.average_cost.toFixed(2)+' JPY · 開倉 '+p.opened_at;d.append(row);}if(!d.children.length)d.textContent='尚無已確認持倉';document.getElementById('flush-mail').disabled=s.email.status!=='READY';document.getElementById('mail-status').textContent='通知：'+({DISABLED:'已暫停',READY:'可寄送',NEEDS_GMAIL_APP_PASSWORD:'待設定Gmail憑證'}[s.email.status]||s.email.status)+' · 待寄 '+(s.email.counts.PENDING||0)+' · 已寄 '+(s.email.counts.SENT||0);document.getElementById('analysis-status').textContent=s.analysis.status+(s.analysis.error?' · '+s.analysis.error:'');document.getElementById('analyze').disabled=s.analysis.status==='RUNNING';document.getElementById('reload-report').hidden=!s.report_as_of||s.report_as_of===htmlReportAsOf;}
const fillForm=document.getElementById('fill-form');fillForm.elements.filled_at.value=new Date(Date.now()+9*3600000).toISOString().slice(0,16);
fillForm.addEventListener('submit',async e=>{e.preventDefault();if(fillBusy)return;fillBusy=true;document.getElementById('fill-save').disabled=true;const f=new FormData(fillForm);const value={id:pendingFillId,symbol:f.get('symbol'),side:f.get('side'),quantity:Number(f.get('quantity')),price:Number(f.get('price')),fees:Number(f.get('fees')),filled_at:f.get('filled_at')+':00+09:00',confirmed:f.has('confirmed')};if(f.get('risk_lower')&&value.side==='BUY')value.risk_reference={channel:'MANUAL',lower:Number(f.get('risk_lower')),price_basis:'raw_unverified',defined_at:new Date().toISOString(),anchor_at:null};try{await postDesk('/api/fills',value);pendingFillId=crypto.randomUUID();fillForm.elements.confirmed.checked=false;document.getElementById('fill-status').textContent=value.confirmed?'已回報實際成交，下輪分析會使用。':'已存未確認記錄，未計入持倉。';await refreshDesk();}catch(x){document.getElementById('fill-status').textContent=x.message;}finally{fillBusy=false;document.getElementById('fill-save').disabled=false;}});
document.getElementById('analyze').onclick=async()=>{try{await postDesk('/api/analyze',{});await refreshDesk();}catch(x){document.getElementById('analysis-status').textContent=x.message;}};
document.getElementById('flush-mail').onclick=async()=>{try{await postDesk('/api/notifications/flush',{});await refreshDesk();}catch(x){document.getElementById('mail-status').textContent=x.message;}};
document.getElementById('reload-report').onclick=()=>location.reload();
refreshDesk();setInterval(refreshDesk,5000);
</script>'''


class DeskServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, port=8765, *, data_root=None, output_root=None, universe_path=None, observer=None):
        self.data_root = Path(data_root or ROOT / "local_data")
        self.output_root = Path(output_root or ROOT / "output" / "observations")
        self.universe_path, self.observer = universe_path, observer
        self.token = secrets.token_urlsafe(32)
        self.analysis = {"status": "IDLE"}
        self.analysis_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), Handler)

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
            from .observation import observe
            _, target, code = (self.observer or observe)(agents=True, universe_source="fixed", universe_path=self.universe_path,
                scan_limit=20, max_packets=3, max_calls=7, public_information=True,
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
                panel = PANEL.replace("__TOKEN__", json.dumps(self.server.token)).replace("__ASOF__", json.dumps(report.get("as_of") if report else None))
                self.reply(html.replace("</main>", panel + "</main>"), html=True)
            elif path == "/api/state":
                report, _ = self.server.latest()
                now = datetime.now(TOKYO).isoformat()
                self.reply({"portfolio": read_snapshot(self.server.data_root / "manual_fills.json", now),
                    "analysis": self.server.analysis, "email": email_status(self.server.data_root / "email_settings.json", self.server.data_root / "notifications.sqlite3"),
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
            return self.reply({"error": "LOCAL_REQUEST_TOKEN_REQUIRED"}, 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 16384 or self.headers.get_content_type() != "application/json":
                raise DataError("INVALID_LOCAL_JSON_REQUEST")
            value = strict_json_loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise DataError("JSON_OBJECT_REQUIRED")
            path = urlsplit(self.path).path
            if path == "/api/fills":
                row = record_fill(self.server.data_root / "manual_fills.json", value)
                return self.reply({"recorded": True, "counted_as_holding": row["confirmed"], "id": row["id"]})
            if path == "/api/notifications/flush":
                return self.reply(flush(self.server.data_root / "email_settings.json", self.server.data_root / "notifications.sqlite3"))
            if path == "/api/analyze":
                with self.server.analysis_lock:
                    if self.server.analysis["status"] == "RUNNING":
                        return self.reply({"error": "ANALYSIS_ALREADY_RUNNING"}, 409)
                    self.server.analysis = {"status": "RUNNING"}
                    threading.Thread(target=self.server.analyze, daemon=True).start()
                return self.reply({"status": "RUNNING"}, 202)
            return self.reply({"error": "NOT_FOUND"}, 404)
        except Exception as exc:
            return self.reply({"error": str(exc) if isinstance(exc, DataError) else "LOCAL_REQUEST_ERROR"}, 400)


def serve(port=8765):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise DataError("INVALID_DASHBOARD_PORT")
    server = DeskServer(port)
    print(f"本機日股頁面：http://127.0.0.1:{server.server_port}（Ctrl+C結束；沒有自動下單）", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
