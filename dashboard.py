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
        root.title('STOCKLLM — 行情與 AI 建議 / REAL ORDER SENT = NO')
        root.geometry('1460x880')
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.status_text = tk.StringVar()
        ttk.Label(root, textvariable=self.status_text, justify='left').pack(fill='x', padx=12, pady=8)
        bar = ttk.Frame(root)
        bar.pack(fill='x', padx=12)
        self.run_button = ttk.Button(bar, text='立即重新分析 / Run Analysis Now', command=self.manual)
        self.run_button.pack(side='left')
        for label, command in [('Start Auto Scan', self.start_auto), ('Stop Auto Scan', self.stop_auto),
                               ('Refresh', self.refresh), ('Open Latest Run Folder', self.open_folder),
                               ('Position Settings', self.position_settings)]:
            ttk.Button(bar, text=label, command=command).pack(side='left', padx=4)
        self.market_text = tk.StringVar(value='Market: 尚未取得行情')
        ttk.Label(root, textvariable=self.market_text).pack(fill='x', padx=12, pady=8)
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill='both', expand=True, padx=12)
        self.top_tree = self.table('Quant Top 10', ['Rank','Symbol','Price','Quote Status','Quant Score','AI Decision','Confidence'])
        self.union_tree = self.table('OpenAI Analysis Set', ['Symbol','Source','Quant Rank','AI Managed Qty','Decision','Confidence'])
        self.position_tree = self.table('Positions（手動配置，非券商核對）', ['Symbol','Total Qty','User Qty','AI Managed Qty','AI Managed Ratio','User Avg Cost','AI Avg Cost','Current Price','Total PnL','AI Managed PnL','AI PnL %','AI Decision','Action Ratio','Suggested Qty'])
        self.quote_tree = self.table('行情與時間（UTC+08 台北）', ['Symbol','Status','Price','Quote Time','Request Started','Received','Request Completed'])
        detail = ttk.Frame(self.notebook)
        self.notebook.add(detail, text='決策 JSON / 理由')
        self.detail = tk.Text(detail, wrap='word')
        self.detail.pack(fill='both', expand=True)
        self.detail.configure(state='disabled')
        ttk.Label(root, text="Today's Confirmed Trades: unavailable  |  Today's Buy Qty: unavailable  |  Today's Sell Qty: unavailable\nREAL ORDER SENT = NO · 不模擬成交、不更新持倉 · 價格/損益為該輪行情快照，非持續即時報價").pack(pady=8)
        self.refresh()
        self.root.after(100, self.tick)

    def table(self, label, columns):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text=label)
        tree = ttk.Treeview(frame, columns=columns, show='headings')
        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=135 if 'Time' not in col else 235, minwidth=100)
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
        self.status_text.set(f'Current Time: {now().isoformat(timespec="seconds")} (UTC+08 台北)\n'
            f'Last Scan: {self.last_scan or "--"}    Next Auto Scan: {stamp(nxt)}    Auto Countdown: {max(0,int(nxt-wall)) if nxt else "OFF"}s\n'
            f'Last Manual Scan: {stamp(self.scheduler.last_manual)}    Manual Cooldown: {str(cooldown)+"s" if cooldown else "READY"}    Auto Interval: {self.scheduler.interval}s\n'
            f'Status: {self.status}    Skipped overlapping auto slots: {self.scheduler.skipped}    Last Error: {self.last_error or "--"}')
        self.run_button.configure(state='disabled' if self.closing or cooldown or self.scheduler.scan_lock.locked() else 'normal')
        self.root.after(100, self.tick)

    def render(self):
        import json
        for tree in (self.top_tree, self.union_tree, self.position_tree, self.quote_tree):
            tree.delete(*tree.get_children())
        decisions, quotes = {}, {}
        if self.result:
            r = self.result
            decisions = {d['symbol']:d for d in r['decision']['decisions']}
            quotes = {q['symbol']:q for q in r['batch']['quotes']}
            counts = {s:sum(q['quote_status']==s for q in quotes.values()) for s in ('LIVE','LAST_KNOWN','UNAVAILABLE')}
            self.market_text.set(f"Universe: {r['manifest']['universe_count']} | Quotes Requested (含持倉): {len(quotes)} | Quotes Received: {r['batch']['symbols_received']} | " + ' | '.join(f'{k}: {v}' for k,v in counts.items()))
            for t in r['top']:
                d = decisions.get(t['symbol'], {})
                self.top_tree.insert('', 'end', values=[t['rank'], t['symbol'], value(quotes[t['symbol']]['last_price']), t['quote_status'], value(t['quant_score']), d.get('decision','--'), value(d.get('confidence'),True)])
            for c in r['analysis_set']:
                d = decisions.get(c['symbol'], {})
                self.union_tree.insert('', 'end', values=[c['symbol'],c['analysis_source'],value(c['quant_rank']),c['position']['ai_managed_qty'],d.get('decision','--'),value(d.get('confidence'),True)])
            for q in quotes.values():
                self.quote_tree.insert('', 'end', values=[q['symbol'],q['quote_status'],value(q['last_price']),q['quote_timestamp'] or '--',q['request_started_at'],q.get('received_at') or '--',q['request_completed_at']])
            self.detail.configure(state='normal')
            self.detail.delete('1.0','end')
            self.detail.insert('end', json.dumps(r['decision'], ensure_ascii=False, indent=2))
            self.detail.configure(state='disabled')
        for p in self.positions:
            d = decisions.get(p['symbol'], {})
            # A changed manual configuration invalidates the displayed prior advice.
            old = next((c['position'] for c in (self.result or {}).get('analysis_set',[]) if c['symbol']==p['symbol']), {})
            if old.get('updated_at') != p.get('updated_at'):
                d = {}
            fields = ['symbol','total_qty','user_qty','ai_managed_qty','ai_managed_ratio','user_average_cost','ai_average_cost','current_price','total_unrealized_pnl','ai_unrealized_pnl','ai_unrealized_pnl_pct']
            vals = [value(p.get(k), k.endswith('ratio') or k.endswith('pct')) for k in fields]
            vals += [d.get('decision','--'),value(d.get('action_ratio'),True),value(d.get('suggested_qty'))]
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
            ttk.Label(win, text=name).grid(row=i,column=0,padx=8,pady=6)
            e = ttk.Entry(win)
            e.grid(row=i,column=1,padx=8)
            entries[name] = e
        def save():
            try:
                data = {k:e.get().strip() for k,e in entries.items()}
                args = (data['symbol'], int(data['user_qty']), int(data['ai_managed_qty']),
                        float(data['user_average_cost']) if data['user_average_cost'] else None,
                        float(data['ai_average_cost']) if data['ai_average_cost'] else None)
            except ValueError:
                messagebox.showerror('格式錯誤','股數請輸入整數，成本請填數字或留白。',parent=win)
                return
            def work():
                try:
                    position_store.save_manual(self.service.db_path, *args)
                    self.events.put(('position_saved',None))
                except Exception as exc:
                    self.events.put(('refresh_error',redact(exc)))
            threading.Thread(target=work, daemon=True).start()
            win.destroy()
        ttk.Button(win,text='儲存手動配置',command=save).grid(row=5,columnspan=2,pady=8)
        ttk.Label(win,text='成本未知請留白；股數設為 0 可清除該部分配置。').grid(row=6,columnspan=2,padx=8,pady=6)

    def close(self):
        self.closing = True
        self.scheduler.stop_auto()
        self.service.stop.set()
        if self.scheduler.scan_lock.locked():
            self.status = '等待當前請求結束並保存'


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
