"""Responsive research and guarded live execution dashboard."""
import argparse
from datetime import datetime
from decimal import Decimal, InvalidOperation
import math
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from config import ROOT, TAIPEI, now, redact, MAX_QUOTE_AGE_SECONDS
from scheduler import (
    Scheduler, AUTO_INTERVAL_SECONDS, is_twse_oddlot_market_window,
)
from analysis_service import AnalysisService
import position_store
import paper_portfolio


def value(v, percent=False):
    if v is None:
        return '--'
    if percent:
        return f'{v:.2%}'
    return f'{v:.2f}' if isinstance(v, float) else str(v)


def current_position_quotes(health, result):
    """Only value broker holdings with quotes still fresh at display time."""
    quotes = {}
    candidates = list((result or {}).get('batch', {}).get('quotes', []))
    candidates.extend((health or {}).get('position_quotes') or [])
    at = now()
    for quote in candidates:
        try:
            stamp = datetime.fromisoformat(quote['quote_timestamp'])
            age = (at-stamp).total_seconds()
            symbol = str(quote['symbol']).strip()
            price = float(quote['last_price'])
            if (quote.get('quote_status') != 'LIVE' or stamp.tzinfo is None
                    or not symbol
                    or not 0 <= age <= MAX_QUOTE_AGE_SECONDS
                    or not math.isfinite(price) or price <= 0):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        if symbol not in quotes or stamp > quotes[symbol][0]:
            quotes[symbol] = (stamp, price, quote.get('name'))
    return quotes


DECISION_LABELS = {'BUY': '買進', 'HOLD': '持有', 'SELL': '賣出', 'WAIT': '觀望'}
QUALITY_LABELS = {'LIVE': '即時行情', 'LAST_KNOWN': '最近行情', 'MIXED': '行情混合', 'UNAVAILABLE': '無可用行情'}
STATUS_LABELS = {
    'IDLE': '閒置', 'SCANNING_QUOTES': '取得行情中', 'QUANT_ANALYSIS': '量化分析中',
    'WAITING_OPENAI': '等待 AI 分析', 'PROCESSING_RESULT': '整理分析結果中',
    'COMPLETE': '完成', 'ERROR': '錯誤', 'RUNNING': '執行中',
    'PAUSED_LOW_BALANCE': '券商可買金額低於下限；自動掃描已停止',
}
BROKER_EVENT_LABELS = {
    'FULLY_FILLED': '券商成交事件：全部成交',
    'PARTIALLY_FILLED_REMAINDER_UNCONFIRMED': '券商成交事件：部分成交，餘量未確認',
    'PARTIAL_FILL_REMAINDER_CANCELLED': '券商成交事件：部分成交，餘量已取消',
    'ACKNOWLEDGED_NO_FILL_EVENT': '券商已受理，尚未見成交事件',
    'CANCELLED_NO_FILL': '券商取消，未見成交事件',
    'NO_MATCHING_BROKER_EVENT': '本次未找到對應券商事件',
    'PRIOR_FILL_VERIFIED_REPLAY_NOT_RETURNED': '先前成交已存證，本次未重播',
}
BUY_BLOCK_LABELS = {
    'BROKER_CHECK_ERROR': '券商查詢錯誤',
    'BALANCE_UNVERIFIED': '券商可買額未確認',
    'INVENTORY_UNVERIFIED': '券商庫存未確認',
    'BELOW_MINIMUM_BALANCE': '券商可買額低於保留額',
    'UNRESOLVED_ORDER': '尚有未結委託',
    'INVENTORY_MISMATCH': '券商持倉與正式帳本不一致',
    'BROKER_FILL_LEDGER_MISMATCH': '券商成交尚未同步正式帳本',
    'BROKER_FILL_UNVERIFIED': '券商成交事件未完整核對',
    'PENDING_RESERVE_UNVERIFIED': '未結買單保留額無法核實',
    'AI_CAPITAL_UNVERIFIED': 'AI 持倉投入額無法核實',
    'AI_CAPITAL_LIMIT_REACHED': 'AI 資金總上限已用盡',
    'DAILY_BUY_LIMIT_REACHED': '當日 BUY 額度已用盡',
    'NO_BROKER_HEADROOM_AFTER_RESERVE': '券商可買額扣除保留後不足',
    'PRIOR_EXECUTION_ERROR': '前次委託執行錯誤',
    'OTHER_HEALTH_WARNING': '健康檢查仍有警示',
}
ORDER_LEDGER_LABELS = {
    'FILLED': '全部成交', 'PARTIALLY_FILLED': '部分成交，餘量未結',
    'PARTIALLY_FILLED_CANCELLED': '部分成交，餘量已取消',
    'ACKNOWLEDGED': '券商已受理', 'UNCONFIRMED': '券商回覆待確認',
    'CANCELLED': '已取消',
}
SOURCE_LABELS = {'TOP10': '量化前十名', 'POSITION': '持倉追蹤', 'TOP10+POSITION': '前十名與持倉'}


def decision_text(decision, analysis_set=(), analysis_at=None, budget_info=None):
    asset_types = {c.get('symbol'): c.get('asset_type', 'UNKNOWN') for c in analysis_set}
    names = {c.get('symbol'): c.get('name') or '股票名稱未取得' for c in analysis_set}
    lines = [f"AI 分析時間：{analysis_at or '未取得'}（台北時間 UTC+8）\n\n",
             f"市場觀察\n{decision.get('market_view', '尚無分析資料')}"]
    if budget_info:
        buys = [d for d in decision.get('decisions', []) if d.get('decision') == 'BUY']
        candidates = {c.get('symbol'): c for c in analysis_set}
        subtotal = 0.0
        reserve = 0.0
        for item in buys:
            candidate = candidates.get(item.get('symbol'), {})
            price = candidate.get('budget_price') or candidate.get('ask') or candidate.get('last_price')
            qty = item.get('suggested_qty') or 0
            if isinstance(price, (int, float)) and qty > 0:
                gross = price * qty
                subtotal += gross
                reserve += max(float(budget_info.get('min_buy_fee_reserve_twd') or 0),
                               gross * float(budget_info.get('buy_cash_buffer_rate') or 0))
        available = budget_info.get('available_cash_twd')
        total = subtotal + reserve
        if budget_info.get('budget_status') != 'READY':
            check = '預算未確認／額度為零，本輪已停止'
        else:
            check = '通過' if available is not None and total <= available else ('無 BUY 建議' if not buys else '超額／未確認')
        lines.insert(1,
            f"本輪可用買進預算：NT$ {available:,.0f}　"
            f"AI BUY 估算總額（含預留）：NT$ {total:,.0f}　檢查：{check}\n"
            f"券商可買餘額：NT$ {budget_info.get('broker_buying_power_twd') or 0:,.0f}　"
            f"最低保留：NT$ {budget_info.get('minimum_balance_reserve_twd') or 0:,.0f}　"
            f"未結買單保留：NT$ {budget_info.get('pending_buy_reserve_twd') or 0:,.0f}　"
            f"AI 持倉投入估算：NT$ {budget_info.get('ai_committed_capital_twd') or 0:,.0f}　"
            f"總上限剩餘：NT$ {budget_info.get('strategy_remaining_twd') or 0:,.0f}\n"
            f"當日剩餘額度：NT$ {budget_info.get('daily_buy_remaining_twd') or 0:,.0f}\n\n")
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
        if item.get('decision') == 'BUY':
            candidate = next((c for c in analysis_set if c.get('symbol') == symbol), {})
            price = candidate.get('budget_price') or candidate.get('ask') or candidate.get('last_price')
            qty = item.get('suggested_qty') or 0
            if isinstance(price, (int, float)) and qty > 0:
                gross = price * qty
                reserve = max(float((budget_info or {}).get('min_buy_fee_reserve_twd') or 0),
                              gross * float((budget_info or {}).get('buy_cash_buffer_rate') or 0))
                lines.append(f"\n本筆預估委託（含費用預留）：NT$ {gross+reserve:,.0f}")
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
        self.scheduler = Scheduler(
            interval,
            allowed_slot=(
                is_twse_oddlot_market_window
                if interval == AUTO_INTERVAL_SECONDS else None
            ),
            phase_offset_seconds=300 if interval == AUTO_INTERVAL_SECONDS else 0,
        )
        self.next_health_at = time.time()
        self.health_running = False
        self.last_health = None
        self.balance_hold = False
        self.events = queue.Queue()
        self.result = None
        self.positions = []
        self.status = 'IDLE'
        self.paper_account_configured = False
        self.last_error = ''
        self.last_scan = None
        self.closing = False
        self.broker_shutdown_started = False
        self.broker_shutdown_complete = False
        self.refresh_pending = False
        self.heartbeat = 0
        root.title('STOCKLLM｜台股行情與 AI 分析')
        root.geometry('1500x920')
        root.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style(root)
        style.configure('Treeview', rowheight=29)
        style.configure('Treeview.Heading', font=('Microsoft JhengHei UI', 10, 'bold'))
        style.configure('CapitalReadonly.TEntry', fieldbackground='#FFF2CC', foreground='#333333')
        style.map('CapitalReadonly.TEntry',
                  fieldbackground=[('readonly', '#FFF2CC')],
                  foreground=[('readonly', '#333333')])
        self.status_text = tk.StringVar()
        self.system_status_text = tk.StringVar()
        ttk.Label(root, textvariable=self.status_text, anchor='w',
                  padding=(8, 5), font=('Microsoft JhengHei UI', 10, 'bold')).pack(
                      fill='x', padx=20, pady=(8, 4))
        summary = ttk.Frame(root)
        summary.pack(fill='x', padx=20, pady=(0, 4))
        for column in range(3):
            summary.columnconfigure(column, weight=1, uniform='summary')
        self.paper_text = tk.StringVar(value='資金總上限尚未設定；按紅色按鈕輸入上限。')
        self.finance_text = tk.StringVar(value='券商資金：等待對帳。')
        self.settlement_text = tk.StringVar(value='券商預計交割款：等待查詢。')
        for column, (title, variable, color) in enumerate((
                ('券商資金', self.finance_text, '#184D47'),
                ('AI 額度', self.paper_text, '#8B0000'),
                ('交割與成交', self.settlement_text, '#6B4E16'))):
            card = ttk.LabelFrame(summary, text=title, padding=(8, 5))
            card.grid(row=0, column=column, sticky='nsew',
                      padx=(0, 6) if column < 2 else 0)
            ttk.Label(card, textvariable=variable, anchor='w', justify='left',
                      wraplength=440, foreground=color,
                      font=('Microsoft JhengHei UI', 10, 'bold')).pack(
                          fill='both', expand=True)
        self.spend_text = tk.StringVar(value='券商成交價金：請重新整理並與券商對帳。')
        self.system_budget_text = tk.StringVar()
        self.system_finance_text = tk.StringVar()
        self.system_settlement_text = tk.StringVar()
        self.budget_reason_text = tk.StringVar(value='買進新委託狀態：待券商對帳。')
        ttk.Label(root, textvariable=self.budget_reason_text, anchor='w', justify='left',
                  padding=(8, 3), foreground='#8B4513').pack(fill='x', padx=20,
                                                              pady=(0, 3))
        bar = ttk.Frame(root)
        bar.pack(fill='x', padx=20, pady=(0, 7))
        ttk.Label(bar, text='資金總上限（TWD）').pack(side='left', padx=(0, 5))
        self.paper_limit_var = tk.StringVar(value='尚未設定')
        self.paper_limit_input = ttk.Entry(
            bar, width=14, textvariable=self.paper_limit_var,
            state='readonly', style='CapitalReadonly.TEntry'
        )
        self.paper_limit_input.pack(side='left', padx=(0, 5), ipady=3)
        self.paper_capital_button = tk.Button(
            bar, text='設定／提高上限', command=self.save_paper_capital,
            bg='#C62828', fg='white', activebackground='#8E0000', activeforeground='white',
            relief='flat', padx=10, pady=4, font=('Microsoft JhengHei UI', 9, 'bold')
        )
        self.paper_capital_button.pack(side='left', padx=(0, 12))
        self.run_button = ttk.Button(bar, text='分析並送交實盤風控', command=self.manual)
        self.run_button.pack(side='left', padx=(0, 8), ipady=3)
        for label, command in [('開始自動掃描', self.start_auto), ('停止自動掃描', self.stop_auto),
                               ('重新整理並與券商對帳', lambda: self.refresh(sync_broker=True)), ('開啟最近分析資料夾', self.open_folder),
                               ('設定持倉', self.position_settings)]:
            ttk.Button(bar, text=label, command=command).pack(side='left', padx=4, ipady=3)
        self.market_text = tk.StringVar(value='行情摘要：尚未取得資料')
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill='both', expand=True, padx=20, pady=(0, 8))
        self.top_tree = self.table('量化排名', ['名次','股票代號','股票名稱','最新價','行情狀態','量化分數','AI 建議','信心度'])
        self.union_tree = self.table('AI 分析清單',
            ['股票代號','股票名稱','納入原因','量化排名','目前 AI 持股','持股核對','持股對帳時間','建議','信心度'],
            hint='目前 AI 持股取自最近券商對帳後的正式帳本；建議與量化排名仍屬分析當時結果。')
        self.position_tree = self.table('持倉概況',
            ['股票代號','股票名稱','券商現股','AI 管理股數','人工設定股數','參考價','持倉參考市值','行情時間','對帳狀態','對帳時間'],
            hint='請按「重新整理並與券商對帳」以取得最新資訊；持倉數量以券商回傳為準。')
        self.quote_tree = self.table('行情與時間（台北時間 UTC+8）', ['股票代號','股票名稱','行情狀態','最新價','資料年齡（秒）','行情時間','請求開始','資料收到','請求完成'])
        self.broker_position_tree = self.table('券商持倉對帳', ['對帳時間','股票代號','券商現股','正式 AI 股數','人工設定股數','差異狀態'])
        self.live_trade_tree = self.table('正式委託與成交紀錄',
            ['委託時間','股票代號','方向','委託股數','帳本成交股數','券商回報成交明細',
             '券商回報成交價金','未確認成交股數','已取消餘量','券商事件推定','事件數',
             '委託價','帳本狀態','券商回報核對','券商序號','分析回合'])
        detail = ttk.Frame(self.notebook)
        self.notebook.add(detail, text='分析結果與理由')
        self.detail = tk.Text(detail, wrap='word', padx=14, pady=12, font=('Microsoft JhengHei UI', 10), spacing1=2, spacing3=5)
        self.detail.pack(fill='both', expand=True, padx=8, pady=8)
        self.detail.configure(state='disabled')
        system = ttk.Frame(self.notebook)
        self.notebook.add(system, text='系統資訊')
        for title, variable in (
                ('執行與健康檢查', self.system_status_text),
                ('AI 額度與持倉參考市值', self.system_budget_text),
                ('券商餘額與新買預算算法', self.system_finance_text),
                ('預計交割明細', self.system_settlement_text),
                ('券商成交與風控占用', self.spend_text),
                ('行情掃描', self.market_text)):
            ttk.Label(system, text=title, font=('Microsoft JhengHei UI', 10, 'bold'),
                      padding=(12, 8, 12, 2)).pack(fill='x')
            ttk.Label(system, textvariable=variable, anchor='w', justify='left',
                      wraplength=1380, padding=(20, 0, 12, 2)).pack(fill='x')
        self.refresh()
        self.root.after(100, self.tick)

    def table(self, label, columns, hint=None):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text=label)
        tree = ttk.Treeview(frame, columns=columns, show='headings')
        widths = {
            '名次': 65, '股票代號': 90, '股票名稱': 150, '最新價': 95, '行情狀態': 110, '量化分數': 100, 'AI 建議': 90, '信心度': 85,
            '納入原因': 130, '量化排名': 90, 'AI 管理股數': 105,
            '目前 AI 持股': 110, '持股核對': 120, '持股對帳時間': 205, '建議': 85,
            '總股數': 80, '人工持有': 85, 'AI 管理': 85, 'AI 管理比例': 100, '人工平均成本': 110,
            'AI 平均成本': 105, '目前參考價': 100, '總未實現損益': 115, 'AI 未實現損益': 115,
            'AI 損益率': 90, '建議減碼比例': 110, '建議股數': 90,
            '資料年齡（秒）': 115, '行情時間': 180, '請求開始': 180, '資料收到': 180, '請求完成': 180,
            '券商回報成交明細': 235, '券商回報成交價金': 155,
            '券商事件推定': 260, '事件數': 155,
        }
        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=widths.get(col, 110), minwidth=75, anchor='center' if col in widths else 'w')
        ys = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        xs = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        table_row = 1 if hint else 0
        if hint:
            ttk.Label(frame, text=hint, anchor='w', padding=(10, 8)).grid(
                row=0, column=0, columnspan=2, sticky='ew')
        tree.grid(row=table_row, column=0, sticky='nsew')
        ys.grid(row=table_row, column=1, sticky='ns')
        xs.grid(row=table_row+1, column=0, sticky='ew')
        frame.rowconfigure(table_row, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def start_auto(self):
        if paper_portfolio.account(self.service.db_path) is None:
            messagebox.showinfo('資金總上限', '請先在主畫面設定資金總上限。')
            return
        if self.balance_hold:
            messagebox.showwarning('餘額下限', '券商可買金額低於設定下限，已停止自動掃描。請確認餘額並等待下一次健康檢查。')
            return
        self.scheduler.start_auto(time.time())

    def health_delay(self):
        """Check open orders every minute; otherwise refresh broker state every five."""
        snapshot = self.last_health or {}
        pending = bool(snapshot.get('unresolved_attempts')) or any(
            int(row.get('unconfirmed_qty') or 0) > 0
            for row in snapshot.get('broker_order_reconciliation') or [])
        return 60 if pending else 300

    def launch_health(self):
        if self.health_running:
            return
        self.health_running = True
        def work():
            try:
                from execution.health_monitor import sync_broker_state
                self.events.put(('health', sync_broker_state(ROOT)))
            except Exception as exc:
                self.events.put(('health_error', redact(f'{type(exc).__name__}: {exc}')))
        threading.Thread(target=work, name='health-worker', daemon=True).start()

    def stop_auto(self):
        self.scheduler.stop_auto()

    def manual(self):
        if paper_portfolio.account(self.service.db_path) is None:
            messagebox.showinfo('資金總上限', '請先在主畫面設定資金總上限。')
            return
        if not self.closing and self.scheduler.manual(time.time(), time.monotonic()):
            self.launch('MANUAL')

    def save_paper_capital(self):
        current = paper_portfolio.account(self.service.db_path)
        current_cents = current['capital_limit_cents'] if current else 0
        current_twd = current_cents // 100
        if current:
            prompt = (
                '請輸入新的資金總上限（新台幣整數）。\n'
                f'目前上限：NT$ {current_twd:,}\n'
                '此金額限制 AI 買進預算；券商可買額須另行對帳。'
            )
        else:
            prompt = '請輸入資金總上限（新台幣整數）。'
        amount = simpledialog.askinteger(
            '設定資金總上限', prompt,
            parent=self.root, initialvalue=current_twd if current else 10000,
            minvalue=current_twd if current else 1
        )
        if amount is None:
            return
        if not messagebox.askyesno(
            '確認資金總上限',
            f'將 AI 交易的資金總上限設定為 NT$ {amount:,}。\n'
            '實際可買額仍以券商回報為準；此設定不會變更券商帳戶餘額。\n\n'
            '確認套用嗎？',
            parent=self.root
        ):
            return
        self.root.configure(cursor='watch')
        self.paper_capital_button.configure(state='disabled')
        def work():
            try:
                state = paper_portfolio.set_capital_limit(self.service.db_path, amount)
                self.events.put(('capital_saved', dict(account=state)))
            except Exception as exc:
                self.events.put(('capital_error', redact(exc)))
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

    def refresh(self, sync_broker=False):
        if self.refresh_pending or self.scheduler.scan_lock.locked() or self.closing:
            return
        self.refresh_pending = True
        def work():
            try:
                self.events.put(('refresh', self.service.refresh(sync_broker=sync_broker)))
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
                    if payload.get('broker_snapshot'):
                        self.last_health = payload['broker_snapshot']
                        self.next_health_at = time.time() + self.health_delay()
                        self.balance_hold = bool(self.last_health.get('balance_below_floor'))
                        if self.balance_hold:
                            self.scheduler.stop_auto()
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
                        self.result, self.positions, broker_snapshot = payload
                        if broker_snapshot is not None:
                            self.last_health = broker_snapshot
                            self.next_health_at = time.time() + self.health_delay()
                            self.balance_hold = bool(broker_snapshot.get('balance_below_floor'))
                            if self.balance_hold:self.scheduler.stop_auto()
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
                    self.paper_capital_button.configure(state='normal')
                    self.paper_account_configured = True
                    account = payload['account']
                    self.paper_limit_var.set(f"{account['capital_limit_cents'] // 100:,}")
                    self.refresh()
                    messagebox.showinfo(
                        '資金總上限已更新',
                        f"資金總上限：NT$ {account['capital_limit_cents'] // 100:,}\n"
                        '請按「重新整理並與券商對帳」取得最新可買額與持倉。',
                        parent=self.root
                    )
                elif kind == 'capital_error':
                    self.root.configure(cursor='')
                    self.paper_capital_button.configure(state='normal')
                    messagebox.showerror('資金總上限設定失敗', payload, parent=self.root)
                elif kind == 'position_saved':
                    self.refresh()
                elif kind == 'health':
                    self.health_running = False
                    self.last_health = payload
                    self.next_health_at = time.time() + self.health_delay()
                    self.balance_hold = bool(payload.get('balance_below_floor'))
                    if self.balance_hold:
                        self.scheduler.stop_auto()
                        self.status = 'PAUSED_LOW_BALANCE'
                    self.render()
                elif kind == 'health_error':
                    self.health_running = False
                    self.last_error = payload
                    self.next_health_at = time.time() + 60
                elif kind == 'broker_shutdown_complete':
                    self.broker_shutdown_complete = True
                elif kind == 'broker_shutdown_error':
                    self.last_error = payload
                    self.broker_shutdown_complete = True
        except queue.Empty:
            pass
        wall, mono = time.time(), time.monotonic()
        if (not self.closing and wall >= self.next_health_at
                and not self.health_running and not self.refresh_pending
                and not self.scheduler.scan_lock.locked()):
            self.next_health_at = wall + self.health_delay()
            self.launch_health()
        if not self.closing and self.scheduler.tick(wall):
            self.launch('AUTO')
        if (not self.closing and self.heartbeat % 300 == 0
                and not self.refresh_pending and not self.scheduler.scan_lock.locked()):
            self.render()
        if (self.closing and not self.scheduler.scan_lock.locked()
                and not self.health_running):
            if not self.broker_shutdown_started:
                self.broker_shutdown_started = True
                def shutdown_broker():
                    try:
                        from execution.broker_runtime import shutdown_broker_runtime
                        shutdown_broker_runtime(ROOT)
                        self.events.put(('broker_shutdown_complete', None))
                    except Exception as exc:
                        self.events.put((
                            'broker_shutdown_error',
                            redact(f'{type(exc).__name__}: {exc}'),
                        ))
                threading.Thread(
                    target=shutdown_broker, name='broker-shutdown', daemon=True,
                ).start()
            if self.broker_shutdown_complete:
                self.root.destroy()
                return
            self.root.after(100, self.tick)
            return
        def stamp(ts):
            return datetime.fromtimestamp(ts, TAIPEI).isoformat(timespec='seconds') if ts else '--'
        cooldown = self.scheduler.cooldown(mono)
        nxt = self.scheduler.next_auto
        health = self.last_health or {}
        comparisons = health.get('inventory_comparison') or []
        mismatches = [x['symbol'] for x in comparisons if x.get('status') != 'MATCH']
        broker_balance = health.get('broker_balance_twd')
        current_buy_budget = health.get('current_buy_budget_twd')
        fill_mismatch = any(
            row.get('status') == 'FILL_MISMATCH'
            for row in health.get('broker_order_reconciliation') or [])
        pending_orders = [row for row in health.get('broker_order_reconciliation') or []
                          if int(row.get('unconfirmed_qty') or 0) > 0]
        health_note = ('（券商成交股數與正式帳本不一致；請重新整理對帳）' if fill_mismatch else
                       '（有委託部分成交或尚未見成交事件）' if pending_orders else
                       '（前次分析失敗；本次券商資料已確認）'
                       if health.get('status') == 'WARN'
                       and health.get('last_scan_status') == 'ERROR'
                       and health.get('buy_sources_verified') else '')
        if health.get('latest_health_write_warning'):
            health_note += '（健康檢查檔案寫入受阻；券商資料已另存正式資料庫）'
        broker_line = (f"券商可買：{broker_balance if broker_balance is not None else '未確認'} 元　"
                       f"持倉核對：{'不一致 '+','.join(mismatches) if mismatches else '相符' if comparisons else '未確認'}　"
                       f"未解委託：{len(health.get('unresolved_attempts') or [])} 筆")
        checked_at = (health.get('checked_at') or '--').replace('T', ' ')
        if checked_at != '--':
            checked_at = checked_at[5:19]
        budget_label = (f'NT$ {float(current_buy_budget):,.0f}'
                        if current_buy_budget is not None else '待確認')
        self.status_text.set(
            f'健康 {health.get("status", "待檢查")}　｜　'
            f'持倉 {"不一致" if mismatches else "相符" if comparisons else "待核對"}　｜　'
            f'券商對帳 {checked_at}　｜　未確認委託 {len(pending_orders)} 筆　｜　'
            f'可新買 {budget_label}　｜　{STATUS_LABELS.get(self.status, self.status)}')
        self.system_status_text.set(
            f'現在時間：{now().isoformat(timespec="seconds")}（台北時間 UTC+8）\n'
            f'最近完成分析：{self.last_scan or "尚無"}\n\n'
            f'自動掃描：{("下次 " + stamp(nxt) + "　倒數 " + str(max(0, int(nxt-wall))) + " 秒") if nxt else "未啟用"}　　'
            f'掃描間隔：{self.scheduler.interval // 60} 分鐘　　因分析進行中而略過的自動掃描：{self.scheduler.skipped} 次\n'
            f'手動分析：{stamp(self.scheduler.last_manual)}　　冷卻時間：{str(cooldown)+" 秒" if cooldown else "可立即使用"}\n\n'
            f'目前狀態：{STATUS_LABELS.get(self.status, self.status)}　　最近錯誤：{self.last_error or "無"}\n'
            f'健康檢查：{health.get("status", "尚未完成")}{health_note}'
            f'　　最近檢查：{health.get("checked_at") or "--"}\n'
            f'券商對帳：{"持倉已核對" if health.get("broker_inventory_verified") else "尚未完成"}'
            f'　　對帳時間：{health.get("checked_at") or "--"}'
            f'　　餘額保護：{"已停止 AUTO" if self.balance_hold else "正常"}\n'
            f'下次自動對帳：{stamp(self.next_health_at)}'
            f'（有未結委託每 1 分鐘，其他每 5 分鐘；APP 開啟期間）\n'
            f'{broker_line}　新買單可用預算：'
            f'{f"NT$ {float(current_buy_budget):,.0f}" if current_buy_budget is not None else "未確認"}'
            f'（{health.get("budget_status") or "UNVERIFIED"}）　'
            f'正式委託：{len(health.get("execution_attempts") or [])} 筆'
            f'　資料驗證：{"券商庫存已查詢" if health.get("broker_inventory_verified") else "未完成"}')
        self.run_button.configure(state='disabled' if self.closing or cooldown or self.scheduler.scan_lock.locked() or not self.paper_account_configured else 'normal')
        self.root.after(100, self.tick)

    def render(self):
        for tree in (self.top_tree, self.union_tree, self.position_tree, self.quote_tree):
            tree.delete(*tree.get_children())
        health = self.last_health or {}
        verified = bool(health.get('broker_inventory_verified'))
        live_ai_positions = health.get('ai_positions')
        current_holdings_ready = (verified and isinstance(live_ai_positions, dict)
                                  and bool(health.get('checked_at')))
        comparisons = {row['symbol']: row for row in health.get('inventory_comparison') or []}
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
                symbol = c['symbol']
                ai_qty = live_ai_positions.get(symbol, 0) if current_holdings_ready else '待核對'
                comparison = comparisons.get(symbol)
                holding_status = (
                    {'MATCH': '相符',
                     'AI_EXCEEDS_BROKER': 'AI 股數超過券商',
                     'CONFIGURED_EXCEEDS_BROKER': '設定股數超過券商',
                     'BROKER_EXCESS_UNATTRIBUTED': '券商多出未歸屬股數'}.get(
                         comparison.get('status'), comparison.get('status', '待核對'))
                    if current_holdings_ready and comparison else
                    '無持股' if current_holdings_ready else '待券商核對')
                self.union_tree.insert('', 'end', values=[
                    symbol, c.get('name') or '股票名稱未取得',
                    SOURCE_LABELS.get(c['analysis_source'], c['analysis_source']),
                    value(c['quant_rank']), ai_qty, holding_status,
                    health.get('checked_at') or '--',
                    DECISION_LABELS.get(d.get('decision'), d.get('decision', '--')),
                    value(d.get('confidence'), True)])
            for q in quotes.values():
                try:
                    age = (now() - datetime.fromisoformat(q['quote_timestamp'])).total_seconds()
                    age = f'{age:,.0f}'
                except (TypeError, ValueError):
                    age = '--'
                self.quote_tree.insert('', 'end', values=[q['symbol'],q.get('name') or '股票名稱未取得',QUALITY_LABELS.get(q['quote_status'],q['quote_status']),value(q['last_price']),age,q['quote_timestamp'] or '--',q['request_started_at'],q.get('received_at') or '--',q['request_completed_at']])
            self.detail.configure(state='normal')
            self.detail.delete('1.0','end')
            self.detail.insert('end', decision_text(r['decision'], r['analysis_set'],
                r['manifest'].get('llm_response_received_at'), r.get('execution_budget')))
            if r['manifest'].get('display_notice'):
                self.detail.insert('1.0', r['manifest']['display_notice'] + '\n\n')
            self.detail.configure(state='disabled')
        self.broker_position_tree.delete(*self.broker_position_tree.get_children())
        for row in health.get('inventory_comparison') or []:
            self.broker_position_tree.insert('', 'end', values=[
                health.get('checked_at') or '--', row.get('symbol'), row.get('broker_cash_qty'),
                row.get('ai_qty'), row.get('configured_user_qty'),
                row.get('status') if verified else '券商查詢未完整驗證'])
        self.live_trade_tree.delete(*self.live_trade_tree.get_children())
        order_checks = {x['decision_id']: x for x in health.get('broker_order_reconciliation') or []}
        for attempt in health.get('execution_attempts') or []:
            detail = attempt.get('detail') or {}
            order_check = order_checks.get(attempt.get('decision_id'), {})
            fill_parts = []
            for fill in order_check.get('broker_replay_fill_details') or []:
                try:
                    qty = int(fill['quantity'])
                    price = Decimal(str(fill['price_twd']))
                    fill_parts.append(f'{qty} 股 × NT$ {price:,.2f}')
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    fill_parts.append('成交價待確認')
            gross = order_check.get('broker_replay_fill_gross_twd')
            try:
                gross_text = (f'NT$ {Decimal(str(gross)):,.2f}' if gross is not None
                              else '待確認' if order_check.get('broker_verified_filled_qty',
                                                          order_check.get('broker_replay_filled_qty')) else '--')
            except (ValueError, InvalidOperation):
                gross_text = '待確認'
            self.live_trade_tree.insert('', 'end', values=[
                attempt.get('started_at'), attempt.get('symbol'), attempt.get('side'),
                detail.get('quantity', '--'), detail.get('filled_quantity', '--'),
                '；'.join(fill_parts) if fill_parts else '--', gross_text,
                order_check.get('unconfirmed_qty', '--'),
                order_check.get('cancelled_qty', '--'),
                BROKER_EVENT_LABELS.get(order_check.get('broker_event_status'),
                                        '待券商事件核對'),
                '受理 {ack}／成交 {fill}／取消 {cancel}'.format(
                    **(order_check.get('broker_event_counts') or
                       dict(ack=0, fill=0, cancel=0))),
                detail.get('limit_price', '--'),
                ORDER_LEDGER_LABELS.get(attempt.get('status'), attempt.get('status')),
                {'FILL_MATCH':'成交與帳本相符',
                 'ARCHIVED_BROKER_FILL_VERIFIED':'先前券商成交已存證',
                 'FILL_MISMATCH':'券商成交與帳本不一致',
                 'NO_FILL_CONFIRMED':'券商取消，確認無成交',
                 'NO_FILL_YET_VERIFIED':'券商已受理，尚未見成交',
                 'FILL_UNVERIFIED':'成交尚未確認'}.get(
                    order_check.get('status'),
                    order_check.get('status', '無券商成交核對')),
                detail.get('seq13', '--'), attempt.get('run_id')])
        checks = health.get('broker_order_reconciliation') or []
        known_gross = Decimal('0')
        unpriced_fill = False
        for check in checks:
            if int(check.get('broker_verified_filled_qty',
                             check.get('broker_replay_filled_qty')) or 0) <= 0:
                continue
            try:
                amount = Decimal(str(check['broker_replay_fill_gross_twd']))
                if not amount.is_finite() or amount < 0:
                    raise InvalidOperation
                known_gross += amount
            except (KeyError, TypeError, ValueError, InvalidOperation):
                unpriced_fill = True
        spend = ('待券商成交核對' if not health.get('checked_at')
                 or (not checks and health.get('execution_attempts')) else
                 f'已知 NT$ {known_gross:,.2f}，另有成交價待確認' if unpriced_fill else
                 f'NT$ {known_gross:,.2f}')
        if checks and any(a.get('decision_id') not in order_checks
                          for a in health.get('execution_attempts') or []):
            spend += '，另有委託待核對'
        reserved = health.get('daily_buy_reserved_twd')
        try:
            reserved_text = (f'NT$ {Decimal(str(reserved)):,.2f}'
                             if reserved is not None else '待確認')
        except (ValueError, InvalidOperation):
            reserved_text = '待確認'
        self.spend_text.set(
            f'券商回報已確認成交價金（未含實際手續費）：{spend}　'
            f'核對時間：{health.get("checked_at") or "--"}\n'
            f'當日 BUY 風控額度占用（含已成交與未結委託，非交割款）：{reserved_text}')
        block_reasons = [BUY_BLOCK_LABELS.get(code, code)
                         for code in health.get('buy_block_reasons') or []]
        if health.get('budget_status') == 'READY':
            self.budget_reason_text.set(
                '新買委託：可依可新買額度評估；送單前仍會重新查券商餘額。')
        elif block_reasons:
            self.budget_reason_text.set(
                '新買委託暫停：' + '、'.join(block_reasons) +
                '。詳見「系統資訊」。')
        else:
            self.budget_reason_text.set('新買委託：待券商對帳確認。')
        account = paper_portfolio.account(self.service.db_path)
        self.paper_limit_var.set(f"{account['capital_limit_cents']/100:,.0f}" if account else '尚未設定')
        def money(raw):
            try:
                amount = Decimal(str(raw))
                return f'NT$ {amount:,.2f}' if amount.is_finite() else '待確認'
            except (TypeError, ValueError, InvalidOperation):
                return '待確認'
        dues = health.get('broker_settlement_dues') or []
        if health.get('settlement_query_error'):
            self.settlement_text.set('交割款待券商查詢\n已確認成交價金：' + spend)
            self.system_settlement_text.set('券商近三日交割應收付：暫無可核實資料；請重新整理。')
        elif dues:
            due_lines = []
            payable = Decimal('0')
            nearest_payable = None
            for row in dues:
                amount = Decimal(str(row['net_twd']))
                if amount < 0:
                    payable -= amount
                    label = '應付'
                    if nearest_payable is None:
                        nearest_payable = (row['settlement_date'], -amount)
                elif amount > 0:
                    label = '應收'
                else:
                    label = '淨額'
                due_lines.append(f"{row['settlement_date']} {label} {money(abs(amount))}")
            self.settlement_text.set(
                (f'{nearest_payable[0]} 應付 {money(nearest_payable[1])}'
                 if nearest_payable else '近三日無應付交割款') +
                f'\n已確認成交價金：{spend}')
            self.system_settlement_text.set(
                '券商近三日交割應收付（不是銀行已扣款）：' +
                '；'.join(due_lines) + f'\n列示應付合計：{money(payable)}'
                '；每日期額以券商回報為準，未從當日可買額重複扣除。')
        else:
            self.settlement_text.set('交割款待券商查詢\n已確認成交價金：' + spend)
            self.system_settlement_text.set('券商近三日交割應收付：待券商查詢。')
        fresh_quotes = current_position_quotes(health, self.result)
        broker_rows = health.get('inventory_comparison') or []
        held = [row for row in broker_rows if int(row.get('broker_cash_qty') or 0) > 0]
        priced = [row for row in held if row['symbol'] in fresh_quotes]
        if not verified:
            market_value = '待券商對帳'
        elif len(priced) != len(held):
            market_value = f'待更新行情（{len(held)-len(priced)} 檔）'
        else:
            total_value = sum(int(row['broker_cash_qty'])*fresh_quotes[row['symbol']][1]
                              for row in held)
            market_value = f'NT$ {total_value:,.2f}'
        cap = f"NT$ {account['capital_limit_cents']/100:,.0f}" if account else '尚未設定'
        broker_cash = money(health.get('broker_balance_twd'))
        buy_budget = money(health.get('current_buy_budget_twd'))
        if health.get('budget_status') == 'BLOCKED':
            buy_budget += '（暫停新買單）'
        self.paper_text.set(
            f'上限 {cap}　已投入 {money(health.get("ai_committed_capital_twd"))}\n'
            f'未結保留 {money(health.get("pending_buy_reserve_twd"))}　'
            f'可新買 {buy_budget}')
        self.system_budget_text.set(
            f'AI 資金總上限：{cap}　AI 持倉投入估算（成交價＋費用緩衝）：'
            f'{money(health.get("ai_committed_capital_twd"))}　'
            f'未結 BUY 安全保留：{money(health.get("pending_buy_reserve_twd"))}\n'
            f'總上限剩餘：{money(health.get("strategy_remaining_twd"))}　'
            f'可供新買：{buy_budget}　持倉參考市值：{market_value}')
        self.finance_text.set(
            f'一戶通餘額 {money(health.get("broker_one_account_balance_twd"))}　'
            f'可出金 {money(health.get("broker_withdrawable_twd"))}\n'
            f'當日可買進 {broker_cash}')
        self.system_finance_text.set(
            f'券商一戶通餘額：{money(health.get("broker_one_account_balance_twd"))}　'
            f'可出金：{money(health.get("broker_withdrawable_twd"))}　'
            f'當日可買進：{broker_cash}　查詢時間：{health.get("checked_at") or "--"}\n'
            f'當日可買進 − 最低保留 {money(health.get("minimum_balance_twd"))}'
            f' − 未結 BUY 安全保留 {money(health.get("pending_buy_reserve_twd"))}'
            f' = 保守可用 {money(health.get("broker_remaining_after_pending_twd"))}；'
            f'再與總上限剩餘及當日剩餘額度 {money(health.get("daily_buy_remaining_twd"))} 取最低。'
            f'未結保留可能與券商已圈存金額重疊，作為額外安全緩衝。')
        for row in broker_rows:
            symbol = row['symbol']
            quote = fresh_quotes.get(symbol)
            qty = int(row.get('broker_cash_qty') or 0)
            reference = f'{quote[1]:,.2f}' if quote else '--'
            value_twd = f'NT$ {qty*quote[1]:,.2f}' if quote else '--'
            self.position_tree.insert('', 'end', values=[
                symbol, quote[2] if quote and quote[2] else names.get(symbol, '股票名稱未取得'),
                qty if verified else '--', row.get('ai_qty') if verified else '--',
                row.get('configured_user_qty') if verified else '--',
                reference, value_twd, quote[0].isoformat(timespec='seconds') if quote else '--',
                row.get('status') if verified else '待券商對帳',
                health.get('checked_at') or '--'])

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
