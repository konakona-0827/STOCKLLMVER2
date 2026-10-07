"""Responsive research dashboard. Start with Dashboard.cmd. No execution API."""
import argparse
from datetime import datetime
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from config import ROOT, TAIPEI, now, redact
from scheduler import Scheduler, AUTO_INTERVAL_SECONDS
from analysis_service import AnalysisService
import position_store


def value(v, percent=False):
    if v is None:
        return '--'
    if percent:
        return f'{v:.2%}'
    return f'{v:.2f}' if isinstance(v, float) else str(v)


DECISION_LABELS = {'BUY': '買進', 'HOLD': '持有', 'SELL': '賣出', 'WAIT': '觀望'}
QUALITY_LABELS = {'LIVE': '即時行情', 'LAST_KNOWN': '最近行情', 'MIXED': '行情混合', 'UNAVAILABLE': '無可用行情'}
STATUS_LABELS = {
    'IDLE': '閒置', 'SCANNING_QUOTES': '取得行情中', 'QUANT_ANALYSIS': '量化分析中',
    'WAITING_OPENAI': '等待 AI 分析', 'PROCESSING_RESULT': '整理分析結果中',
    'COMPLETE': '完成', 'ERROR': '錯誤', 'RUNNING': '執行中',
}
SOURCE_LABELS = {'TOP10': '量化前十名', 'POSITION': '持倉追蹤', 'TOP10+POSITION': '前十名與持倉'}


def decision_text(decision):
    lines = [f"市場觀察\n{decision.get('market_view', '尚無分析資料')}"]
    for item in decision.get('decisions', []):
        warnings = item.get('warnings') or []
        lines.append('\n\n' + '─' * 48)
        lines.append(f"\n股票代號：{item.get('symbol', '--')}")
        lines.append(f"\n建議：{DECISION_LABELS.get(item.get('decision'), item.get('decision', '--'))}")
        lines.append(f"\n信心度：{value(item.get('confidence'), True)}")
        lines.append(f"\n參考價：{value(item.get('reference_price'))}　建議價：{value(item.get('suggested_price'))}")
        lines.append(f"\n建議股數：{value(item.get('suggested_qty'))}　減碼比例：{value(item.get('action_ratio'), True)}")
        lines.append(f"\n行情狀態：{QUALITY_LABELS.get(item.get('data_quality'), item.get('data_quality', '--'))}")
        lines.append(f"\n理由：{item.get('reason', '--')}")
        if warnings:
            lines.append('\n提醒：' + '；'.join(warnings))
    if not decision.get('decisions'):
        lines.append('\n\n目前沒有個股建議。')
    return ''.join(lines)


class Dashboard:
    def __init__(self, root, directory=ROOT/'data'/'analysis', interval=AUTO_INTERVAL_SECONDS):
        self.root = root
        self.service = AnalysisService(directory)
        self.scheduler = Scheduler(interval)
        self.events = queue.Queue()
        self.result = None
        self.positions = []
        self.status = 'IDLE'
        self.last_error = ''
        self.last_scan = None
        self.closing = False
        self.refresh_pending = False
        self.heartbeat = 0
        root.title('STOCKLLM｜台股行情與 AI 分析')
        root.geometry('1500x920')
        root.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style(root)
        style.configure('Treeview', rowheight=29)
        style.configure('Treeview.Heading', font=('Microsoft JhengHei UI', 10, 'bold'))
        self.status_text = tk.StringVar()
        ttk.Label(root, textvariable=self.status_text, justify='left', anchor='w', padding=(12, 10)).pack(fill='x', padx=12, pady=(10, 8))
        bar = ttk.Frame(root)
        bar.pack(fill='x', padx=20, pady=(0, 10))
        self.run_button = ttk.Button(bar, text='立即分析', command=self.manual)
        self.run_button.pack(side='left', padx=(0, 8), ipady=3)
        for label, command in [('開始自動掃描', self.start_auto), ('停止自動掃描', self.stop_auto),
                               ('重新整理', self.refresh), ('開啟最近分析資料夾', self.open_folder),
                               ('設定持倉', self.position_settings)]:
            ttk.Button(bar, text=label, command=command).pack(side='left', padx=4, ipady=3)
        self.market_text = tk.StringVar(value='行情摘要：尚未取得資料')
        ttk.Label(root, textvariable=self.market_text, anchor='w', padding=(12, 9), relief='groove').pack(fill='x', padx=20, pady=(0, 12))
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill='both', expand=True, padx=20, pady=(0, 12))
        self.top_tree = self.table('量化排名', ['名次','股票代號','最新價','行情狀態','量化分數','AI 建議','信心度'])
        self.union_tree = self.table('AI 分析清單', ['股票代號','納入原因','量化排名','AI 管理股數','建議','信心度'])
        self.position_tree = self.table('持倉概況（手動配置，未經券商核對）', ['股票代號','總股數','人工持有','AI 管理','AI 管理比例','人工平均成本','AI 平均成本','目前參考價','總未實現損益','AI 未實現損益','AI 損益率','AI 建議','建議減碼比例','建議股數'])
        self.quote_tree = self.table('行情與時間（台北時間 UTC+8）', ['股票代號','行情狀態','最新價','資料年齡（秒）','行情時間','請求開始','資料收到','請求完成'])
        detail = ttk.Frame(self.notebook)
        self.notebook.add(detail, text='分析結果與理由')
        self.detail = tk.Text(detail, wrap='word', padx=14, pady=12, font=('Microsoft JhengHei UI', 10), spacing1=2, spacing3=5)
        self.detail.pack(fill='both', expand=True, padx=8, pady=8)
        self.detail.configure(state='disabled')
        ttk.Label(root, text='今日已確認成交：無資料　　今日買進股數：無資料　　今日賣出股數：無資料\n本畫面只提供分析建議，不送出委託、不模擬成交、不更新持倉。價格與損益依本輪行情快照計算。',
                   justify='left', anchor='w', padding=(20, 8)).pack(fill='x', pady=(0, 8))
        self.refresh()
        self.root.after(100, self.tick)

    def table(self, label, columns):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text=label)
        tree = ttk.Treeview(frame, columns=columns, show='headings')
        widths = {
            '名次': 65, '股票代號': 90, '最新價': 95, '行情狀態': 110, '量化分數': 100, 'AI 建議': 90, '信心度': 85,
            '納入原因': 130, '量化排名': 90, 'AI 管理股數': 105, '建議': 85,
            '總股數': 80, '人工持有': 85, 'AI 管理': 85, 'AI 管理比例': 100, '人工平均成本': 110,
            'AI 平均成本': 105, '目前參考價': 100, '總未實現損益': 115, 'AI 未實現損益': 115,
            'AI 損益率': 90, '建議減碼比例': 110, '建議股數': 90,
            '資料年齡（秒）': 115, '行情時間': 180, '請求開始': 180, '資料收到': 180, '請求完成': 180,
        }
        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=widths.get(col, 110), minwidth=75, anchor='center' if col in widths else 'w')
        ys = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        xs = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        tree.grid(row=0, column=0, sticky='nsew')
        ys.grid(row=0, column=1, sticky='ns')
        xs.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def start_auto(self):
        self.scheduler.start_auto(time.time())

    def stop_auto(self):
        self.scheduler.stop_auto()

    def manual(self):
        if not self.closing and self.scheduler.manual(time.time(), time.monotonic()):
            self.launch('MANUAL')

    def launch(self, trigger):
        self.status = 'SCANNING_QUOTES'
        self.last_error = ''
        self.run_button.configure(state='disabled')
        def work():
            try:
                result = self.service.run(trigger, lambda phase:self.events.put(('phase', phase)))
                self.events.put(('result', result))
            except Exception as exc:
                self.events.put(('error', redact(f'{type(exc).__name__}: {exc}')))
            finally:
                self.events.put(('finished', None))
        threading.Thread(target=work, name='analysis-worker', daemon=False).start()

    def refresh(self):
        if self.refresh_pending or self.scheduler.scan_lock.locked() or self.closing:
            return
        self.refresh_pending = True
        def work():
            try:
                self.events.put(('refresh', self.service.refresh()))
            except Exception as exc:
                self.events.put(('refresh_error', redact(exc)))
        threading.Thread(target=work, name='refresh-worker', daemon=True).start()

    def tick(self):
        self.heartbeat += 1
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == 'phase':
                    self.status = payload
                elif kind == 'result':
                    self.result, self.positions = payload, payload['positions']
                    self.last_scan = payload['manifest']['finished_at']
                    self.status = payload['manifest']['status']
                    self.last_error = payload['manifest']['error']
                    self.render()
                elif kind == 'error':
                    self.status, self.last_error = 'ERROR', payload
                elif kind == 'finished':
                    self.scheduler.finish()
                elif kind == 'refresh':
                    self.refresh_pending = False
                    if not self.scheduler.scan_lock.locked():
                        self.result, self.positions = payload
                        if self.result:
                            self.last_scan = self.result['manifest']['finished_at']
                        self.render()
                elif kind == 'refresh_error':
                    self.refresh_pending = False
                    self.last_error = payload
                elif kind == 'position_saved':
                    self.refresh()
        except queue.Empty:
            pass
        wall, mono = time.time(), time.monotonic()
        if not self.closing and self.scheduler.tick(wall):
            self.launch('AUTO')
        if self.closing and not self.scheduler.scan_lock.locked():
            self.root.destroy()
            return
        def stamp(ts):
            return datetime.fromtimestamp(ts, TAIPEI).isoformat(timespec='seconds') if ts else '--'
        cooldown = self.scheduler.cooldown(mono)
        nxt = self.scheduler.next_auto
        self.status_text.set(
            f'現在時間：{now().isoformat(timespec="seconds")}（台北時間 UTC+8）\n'
            f'最近完成分析：{self.last_scan or "尚無"}\n\n'
            f'自動掃描：{("下次 " + stamp(nxt) + "　倒數 " + str(max(0, int(nxt-wall))) + " 秒") if nxt else "未啟用"}　　'
            f'掃描間隔：{self.scheduler.interval // 60} 分鐘　　略過重疊時段：{self.scheduler.skipped} 次\n'
            f'手動分析：{stamp(self.scheduler.last_manual)}　　冷卻時間：{str(cooldown)+" 秒" if cooldown else "可立即使用"}\n\n'
            f'目前狀態：{STATUS_LABELS.get(self.status, self.status)}　　最近錯誤：{self.last_error or "無"}')
        self.run_button.configure(state='disabled' if self.closing or cooldown or self.scheduler.scan_lock.locked() else 'normal')
        self.root.after(100, self.tick)

    def render(self):
        for tree in (self.top_tree, self.union_tree, self.position_tree, self.quote_tree):
            tree.delete(*tree.get_children())
        decisions, quotes = {}, {}
        if self.result:
            r = self.result
            decisions = {d['symbol']:d for d in r['decision']['decisions']}
            quotes = {q['symbol']:q for q in r['batch']['quotes']}
            counts = {s:sum(q['quote_status']==s for q in quotes.values()) for s in ('LIVE','LAST_KNOWN','UNAVAILABLE')}
            self.market_text.set(
                f"本次掃描：股票池 {r['manifest']['universe_count']} 檔　　"
                f"查詢行情 {len(quotes)} 檔（含持倉）　　收到有效行情 {r['batch']['symbols_received']} 檔\n"
                f"即時行情 {counts['LIVE']} 檔　　最近行情 {counts['LAST_KNOWN']} 檔　　無可用行情 {counts['UNAVAILABLE']} 檔")
            for t in r['top']:
                d = decisions.get(t['symbol'], {})
                self.top_tree.insert('', 'end', values=[t['rank'], t['symbol'], value(quotes[t['symbol']]['last_price']), QUALITY_LABELS.get(t['quote_status'], t['quote_status']), value(t['quant_score']), DECISION_LABELS.get(d.get('decision'), d.get('decision','--')), value(d.get('confidence'),True)])
            for c in r['analysis_set']:
                d = decisions.get(c['symbol'], {})
                self.union_tree.insert('', 'end', values=[c['symbol'],SOURCE_LABELS.get(c['analysis_source'],c['analysis_source']),value(c['quant_rank']),c['position']['ai_managed_qty'],DECISION_LABELS.get(d.get('decision'), d.get('decision','--')),value(d.get('confidence'),True)])
            for q in quotes.values():
                try:
                    age = (now() - datetime.fromisoformat(q['quote_timestamp'])).total_seconds()
                    age = f'{age:,.0f}'
                except (TypeError, ValueError):
                    age = '--'
                self.quote_tree.insert('', 'end', values=[q['symbol'],QUALITY_LABELS.get(q['quote_status'],q['quote_status']),value(q['last_price']),age,q['quote_timestamp'] or '--',q['request_started_at'],q.get('received_at') or '--',q['request_completed_at']])
            self.detail.configure(state='normal')
            self.detail.delete('1.0','end')
            self.detail.insert('end', decision_text(r['decision']))
            self.detail.configure(state='disabled')
        for p in self.positions:
            d = decisions.get(p['symbol'], {})
            # A changed manual configuration invalidates the displayed prior advice.
            old = next((c['position'] for c in (self.result or {}).get('analysis_set',[]) if c['symbol']==p['symbol']), {})
            if old.get('updated_at') != p.get('updated_at'):
                d = {}
            fields = ['symbol','total_qty','user_qty','ai_managed_qty','ai_managed_ratio','user_average_cost','ai_average_cost','current_price','total_unrealized_pnl','ai_unrealized_pnl','ai_unrealized_pnl_pct']
            vals = [value(p.get(k), k.endswith('ratio') or k.endswith('pct')) for k in fields]
            vals += [DECISION_LABELS.get(d.get('decision'), d.get('decision','--')),value(d.get('action_ratio'),True),value(d.get('suggested_qty'))]
            self.position_tree.insert('', 'end', values=vals)

    def open_folder(self):
        if self.result:
            os.startfile(self.result['manifest']['run_directory'])

    def position_settings(self):
        if self.scheduler.scan_lock.locked():
            messagebox.showinfo('持倉設定', '本輪分析中，完成後再編輯。')
            return
        win = tk.Toplevel(self.root)
        win.title('手動持倉配置（不會送出委託）')
        entries = {}
        for i, name in enumerate(['symbol','user_qty','ai_managed_qty','user_average_cost','ai_average_cost']):
            label = {'symbol':'股票代號', 'user_qty':'人工持有股數', 'ai_managed_qty':'AI 管理股數',
                     'user_average_cost':'人工平均成本', 'ai_average_cost':'AI 平均成本'}[name]
            ttk.Label(win, text=label).grid(row=i,column=0,sticky='w',padx=10,pady=9)
            e = ttk.Entry(win)
            e.grid(row=i,column=1,sticky='ew',padx=10,pady=5)
            entries[name] = e
        def save():
            try:
                data = {k:e.get().strip() for k,e in entries.items()}
                args = (data['symbol'], int(data['user_qty']), int(data['ai_managed_qty']),
                        float(data['user_average_cost']) if data['user_average_cost'] else None,
                        float(data['ai_average_cost']) if data['ai_average_cost'] else None)
            except ValueError:
                messagebox.showerror('格式錯誤','股數請輸入整數；成本請輸入數字，或留白。',parent=win)
                return
            def work():
                try:
                    position_store.save_manual(self.service.db_path, *args)
                    self.events.put(('position_saved',None))
                except Exception as exc:
                    self.events.put(('refresh_error',redact(exc)))
            threading.Thread(target=work, daemon=True).start()
            win.destroy()
        win.columnconfigure(1, weight=1)
        ttk.Button(win,text='儲存持倉設定',command=save).grid(row=5,columnspan=2,pady=(12,8),ipady=3)
        ttk.Label(win,text='成本未知可留白。股數設為 0 可清除該項持倉設定。').grid(row=6,columnspan=2,padx=10,pady=(4,12))

    def close(self):
        self.closing = True
        self.scheduler.stop_auto()
        self.service.stop.set()
        if self.scheduler.scan_lock.locked():
            self.status = '等待本輪分析完成並儲存資料'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=ROOT/'data'/'analysis')
    parser.add_argument('--test-mode', action='store_true')
    parser.add_argument('--interval', type=int, default=AUTO_INTERVAL_SECONDS)
    args = parser.parse_args()
    if args.interval <= 0 or (args.interval != AUTO_INTERVAL_SECONDS and not args.test_mode):
        parser.error('非 1800 秒間隔需要 --test-mode；間隔須大於 0')
    # comtypes imports initialize the importing thread; explicit worker STA
    # initialization is paired separately inside AnalysisService.
    import comtypes
    root = tk.Tk()
    app = Dashboard(root, args.data_dir, args.interval)
    root.mainloop()


if __name__ == '__main__':
    main()
