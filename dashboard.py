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
import paper_portfolio


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


def decision_text(decision, analysis_set=()):
    asset_types = {c.get('symbol'): c.get('asset_type', 'UNKNOWN') for c in analysis_set}
    names = {c.get('symbol'): c.get('name') or '股票名稱未取得' for c in analysis_set}
    lines = [f"市場觀察\n{decision.get('market_view', '尚無分析資料')}"]
    for item in decision.get('decisions', []):
        warnings = item.get('warnings') or []
        lines.append('\n\n' + '─' * 48)
        symbol = item.get('symbol', '--')
        name = names.get(symbol, '股票名稱未取得')
        asset_type = asset_types.get(symbol, 'UNKNOWN')
        type_label = {'ETF': 'ETF', 'STOCK': '股票'}.get(asset_type, '類型待確認')
        lines.append(f"\n標的：{name}（{symbol}，{type_label}）")
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
        self.paper_account_configured = False
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
        self.paper_text = tk.StringVar(value='模擬資金尚未設定；請先輸入上限並按紅色按鈕。')
        ttk.Label(root, textvariable=self.paper_text, anchor='w', padding=(12, 7),
                  foreground='#8B0000', font=('Microsoft JhengHei UI', 10, 'bold')).pack(fill='x', padx=20, pady=(0, 5))
        bar = ttk.Frame(root)
        bar.pack(fill='x', padx=20, pady=(0, 10))
        ttk.Label(bar, text='模擬資金總上限（TWD）').pack(side='left', padx=(0, 5))
        self.paper_limit_input = ttk.Entry(bar, width=14)
        self.paper_limit_input.pack(side='left', padx=(0, 5), ipady=3)
        tk.Button(bar, text='設定／提高上限', command=self.save_paper_capital,
                  bg='#C62828', fg='white', activebackground='#8E0000', activeforeground='white',
                  relief='flat', padx=10, pady=4, font=('Microsoft JhengHei UI', 9, 'bold')).pack(side='left', padx=(0, 12))
        self.run_button = ttk.Button(bar, text='開始分析（模擬）', command=self.manual)
        self.run_button.pack(side='left', padx=(0, 8), ipady=3)
        for label, command in [('開始自動掃描', self.start_auto), ('停止自動掃描', self.stop_auto),
                               ('重新整理', self.refresh), ('開啟最近分析資料夾', self.open_folder),
                               ('設定持倉', self.position_settings)]:
            ttk.Button(bar, text=label, command=command).pack(side='left', padx=4, ipady=3)
        self.market_text = tk.StringVar(value='行情摘要：尚未取得資料')
        ttk.Label(root, textvariable=self.market_text, anchor='w', padding=(12, 9), relief='groove').pack(fill='x', padx=20, pady=(0, 12))
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill='both', expand=True, padx=20, pady=(0, 12))
        self.top_tree = self.table('量化排名', ['名次','股票代號','股票名稱','最新價','行情狀態','量化分數','AI 建議','信心度'])
        self.union_tree = self.table('AI 分析清單', ['股票代號','股票名稱','納入原因','量化排名','AI 管理股數','建議','信心度'])
        self.position_tree = self.table('持倉概況（手動配置，未經券商核對）', ['股票代號','股票名稱','總股數','人工持有','AI 管理','AI 管理比例','人工平均成本','AI 平均成本','目前參考價','總未實現損益','AI 未實現損益','AI 損益率','AI 建議','建議減碼比例','建議股數'])
        self.quote_tree = self.table('行情與時間（台北時間 UTC+8）', ['股票代號','股票名稱','行情狀態','最新價','資料年齡（秒）','行情時間','請求開始','資料收到','請求完成'])
        self.paper_position_tree = self.table('模擬持倉', ['股票代號','股票名稱','股數','含手續費總成本（TWD）','每股均成本（TWD）'])
        self.paper_trade_tree = self.table('模擬成交紀錄', ['時間','股票代號','股票名稱','方向','股數','模擬成交價','成交金額','手續費','現金變化','已實現損益','分析回合'])
        detail = ttk.Frame(self.notebook)
        self.notebook.add(detail, text='分析結果與理由')
        self.detail = tk.Text(detail, wrap='word', padx=14, pady=12, font=('Microsoft JhengHei UI', 10), spacing1=2, spacing3=5)
        self.detail.pack(fill='both', expand=True, padx=8, pady=8)
        self.detail.configure(state='disabled')
        ttk.Label(root, text='模擬成交只使用有效即時行情；不送出群益委託。模擬手續費按每筆金額 0.1425% 無條件捨去，最低 TWD 1。\n每筆模擬買進最多 999 股；模擬持倉與手動輸入的券商持倉分開記錄。',
                   justify='left', anchor='w', padding=(20, 8)).pack(fill='x', pady=(0, 8))
        self.refresh()
        self.root.after(100, self.tick)

    def table(self, label, columns):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text=label)
        tree = ttk.Treeview(frame, columns=columns, show='headings')
        widths = {
            '名次': 65, '股票代號': 90, '股票名稱': 150, '最新價': 95, '行情狀態': 110, '量化分數': 100, 'AI 建議': 90, '信心度': 85,
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
        if paper_portfolio.account(self.service.db_path) is None:
            messagebox.showinfo('模擬資金', '請先在主畫面輸入模擬資金上限並按紅色按鈕。')
            return
        self.scheduler.start_auto(time.time())

    def stop_auto(self):
        self.scheduler.stop_auto()

    def manual(self):
        if paper_portfolio.account(self.service.db_path) is None:
            messagebox.showinfo('模擬資金', '請先在主畫面輸入模擬資金上限並按紅色按鈕。')
            return
        if not self.closing and self.scheduler.manual(time.time(), time.monotonic()):
            self.launch('MANUAL')

    def save_paper_capital(self):
        raw = self.paper_limit_input.get().strip().replace(',', '')
        if not raw.isdigit() or int(raw) <= 0:
            messagebox.showerror('金額格式錯誤', '請輸入大於 0 的整數新台幣金額。')
            return
        amount = int(raw)
        self.root.configure(cursor='watch')
        def work():
            try:
                state = paper_portfolio.set_capital_limit(self.service.db_path, amount)
                self.events.put(('capital_saved', state))
            except Exception as exc:
                self.events.put(('refresh_error', redact(exc)))
        threading.Thread(target=work, name='paper-capital-worker', daemon=True).start()

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
                        self.paper_account_configured = paper_portfolio.account(self.service.db_path) is not None
                        if self.result:
                            self.last_scan = self.result['manifest']['finished_at']
                        self.render()
                elif kind == 'refresh_error':
                    self.refresh_pending = False
                    self.last_error = payload
                    self.root.configure(cursor='')
                elif kind == 'capital_saved':
                    self.root.configure(cursor='')
                    self.paper_account_configured = True
                    self.paper_limit_input.delete(0, 'end')
                    self.paper_limit_input.insert(0, str(payload['capital_limit_cents'] // 100))
                    self.refresh()
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
        self.run_button.configure(state='disabled' if self.closing or cooldown or self.scheduler.scan_lock.locked() or not self.paper_account_configured else 'normal')
        self.root.after(100, self.tick)

    def render(self):
        for tree in (self.top_tree, self.union_tree, self.position_tree, self.quote_tree):
            tree.delete(*tree.get_children())
        decisions, quotes, names = {}, {}, {}
        if self.result:
            r = self.result
            decisions = {d['symbol']:d for d in r['decision']['decisions']}
            quotes = {q['symbol']:q for q in r['batch']['quotes']}
            names = {s:q.get('name') or '股票名稱未取得' for s, q in quotes.items()}
            names.update({c['symbol']: c.get('name') or names.get(c['symbol'], '股票名稱未取得')
                          for c in r['analysis_set']})
            counts = {s:sum(q['quote_status']==s for q in quotes.values()) for s in ('LIVE','LAST_KNOWN','UNAVAILABLE')}
            self.market_text.set(
                f"本次掃描：股票池 {r['manifest']['universe_count']} 檔　　"
                f"查詢行情 {len(quotes)} 檔（含持倉）　　收到有效行情 {r['batch']['symbols_received']} 檔\n"
                f"即時行情 {counts['LIVE']} 檔　　最近行情 {counts['LAST_KNOWN']} 檔　　無可用行情 {counts['UNAVAILABLE']} 檔")
            for t in r['top']:
                d = decisions.get(t['symbol'], {})
                self.top_tree.insert('', 'end', values=[t['rank'], t['symbol'], names.get(t['symbol'], '股票名稱未取得'), value(quotes[t['symbol']]['last_price']), QUALITY_LABELS.get(t['quote_status'], t['quote_status']), value(t['quant_score']), DECISION_LABELS.get(d.get('decision'), d.get('decision','--')), value(d.get('confidence'),True)])
            for c in r['analysis_set']:
                d = decisions.get(c['symbol'], {})
                self.union_tree.insert('', 'end', values=[c['symbol'],c.get('name') or '股票名稱未取得',SOURCE_LABELS.get(c['analysis_source'],c['analysis_source']),value(c['quant_rank']),c['position']['ai_managed_qty'],DECISION_LABELS.get(d.get('decision'), d.get('decision','--')),value(d.get('confidence'),True)])
            for q in quotes.values():
                try:
                    age = (now() - datetime.fromisoformat(q['quote_timestamp'])).total_seconds()
                    age = f'{age:,.0f}'
                except (TypeError, ValueError):
                    age = '--'
                self.quote_tree.insert('', 'end', values=[q['symbol'],q.get('name') or '股票名稱未取得',QUALITY_LABELS.get(q['quote_status'],q['quote_status']),value(q['last_price']),age,q['quote_timestamp'] or '--',q['request_started_at'],q.get('received_at') or '--',q['request_completed_at']])
            self.detail.configure(state='normal')
            self.detail.delete('1.0','end')
            self.detail.insert('end', decision_text(r['decision'], r['analysis_set']))
            self.detail.configure(state='disabled')
        paper_state = paper_portfolio.snapshot(self.service.db_path)
        if paper_state:
            paper_value = sum(p['qty'] * (quotes.get(p['symbol'], {}).get('last_price') or 0)
                              for p in paper_state['positions'])
            self.paper_text.set(
                f"模擬資金上限：TWD {paper_state['capital_limit_twd']:,.0f}　"
                f"可用現金：TWD {paper_state['available_cash_twd']:,.0f}　"
                f"模擬持倉參考市值：TWD {paper_value:,.2f}"
                f"　（提高上限會將增加額加到可用現金）")
            if not self.paper_limit_input.get().strip():
                self.paper_limit_input.insert(0, str(int(paper_state['capital_limit_twd'])))
            self.paper_position_tree.delete(*self.paper_position_tree.get_children())
            for p in paper_state['positions']:
                total_cost = p['cost_cents'] / 100
                avg_cost = total_cost / p['qty'] if p['qty'] else 0
                self.paper_position_tree.insert('', 'end', values=[p['symbol'], names.get(p['symbol'], '股票名稱未取得'), p['qty'], f'{total_cost:,.2f}', f'{avg_cost:,.4f}'])
        else:
            self.paper_text.set('模擬資金尚未設定；請先輸入上限並按紅色按鈕。')
        self.paper_trade_tree.delete(*self.paper_trade_tree.get_children())
        for t in (self.result or {}).get('paper_trade_history', []):
            self.paper_trade_tree.insert('', 'end', values=[
                t['occurred_at'], t['symbol'], t.get('name') or names.get(t['symbol'], '股票名稱未取得'), '買進' if t['side']=='BUY' else '賣出', t['qty'],
                f"{t['price_cents']/100:.2f}", f"{t['gross_cents']/100:.2f}", f"{t['fee_twd']:.0f}",
                f"{t['cash_change_cents']/100:+.2f}", f"{t['realized_pnl_cents']/100:+.2f}", t['run_id']])
        for p in self.positions:
            d = decisions.get(p['symbol'], {})
            # A changed manual configuration invalidates the displayed prior advice.
            old = next((c['position'] for c in (self.result or {}).get('analysis_set',[]) if c['symbol']==p['symbol']), {})
            if old.get('updated_at') != p.get('updated_at'):
                d = {}
            fields = ['symbol','total_qty','user_qty','ai_managed_qty','ai_managed_ratio','user_average_cost','ai_average_cost','current_price','total_unrealized_pnl','ai_unrealized_pnl','ai_unrealized_pnl_pct']
            vals = [value(p.get(k), k.endswith('ratio') or k.endswith('pct')) for k in fields]
            vals.insert(1, names.get(p['symbol'], '股票名稱未取得'))
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
