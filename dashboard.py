"""Responsive research and guarded live execution dashboard."""
import argparse
import json
import sqlite3
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import math
import os
from pathlib import Path
import queue
import re
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from config import ROOT, TAIPEI, now, redact, MAX_QUOTE_AGE_SECONDS
from scheduler import (
    Scheduler, AUTO_INTERVAL_SECONDS, is_twse_oddlot_market_window,
)
from market_calendar import twse_closed_reason
from analysis_service import AnalysisService
from execution.auto_advice_executor import LiveExecutionConfig
import position_store
import paper_portfolio
import strategy_catalog
from lifecycle_log import LifecycleLog


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
    'PAUSED_LOW_BALANCE': '券商資金低於保留額；自動掃描已停止',
}
BROKER_EVENT_LABELS = {
    'ROD_REMAINDER_EXPIRED': 'ROD 跨日，未成交餘量已失效',
    'FULLY_FILLED': '券商成交事件：全部成交',
    'PARTIALLY_FILLED_REMAINDER_UNCONFIRMED': '券商成交事件：部分成交，餘量未確認',
    'PARTIAL_FILL_REMAINDER_CANCELLED': '券商成交事件：部分成交，餘量已取消',
    'ACKNOWLEDGED_NO_FILL_EVENT': '券商已受理，尚未見成交事件',
    'CANCELLED_NO_FILL': '券商取消，未見成交事件',
    'USER_CONFIRMED_CANCELLED_NO_FILL': '使用者確認取消，券商未見成交事件',
    'NO_MATCHING_BROKER_EVENT': '本次未找到對應券商事件',
    'PRIOR_FILL_VERIFIED_REPLAY_NOT_RETURNED': '先前成交已存證，本次未重播',
    'USER_PRIOR_FILL_CONFIRMED_REPLAY_NOT_RETURNED': '使用者確認先前成交，本次未重播',
}
BUY_BLOCK_LABELS = {
    'BROKER_CHECK_ERROR': '券商查詢錯誤',
    'BALANCE_UNVERIFIED': '券商資金未確認',
    'INVENTORY_UNVERIFIED': '券商庫存未確認',
    'BELOW_MINIMUM_BALANCE': '券商資金低於保留額',
    'UNRESOLVED_ORDER': '尚有未結委託',
    'INVENTORY_MISMATCH': '券商持倉與正式帳本不一致',
    'BROKER_FILL_LEDGER_MISMATCH': '券商成交尚未同步正式帳本',
    'BROKER_FILL_UNVERIFIED': '券商成交事件未完整核對',
    'PENDING_RESERVE_UNVERIFIED': '未結買單保留額無法核實',
    'AI_CAPITAL_UNVERIFIED': 'AI 持倉投入額無法核實',
    'AI_CAPITAL_LIMIT_REACHED': 'AI 資金總上限已用盡',
    'DAILY_BUY_LIMIT_REACHED': '當日 BUY 額度已用盡',
    'NO_BROKER_HEADROOM_AFTER_RESERVE': '券商資金扣除保留後不足',
    'BROKER_ACCOUNT_BELOW_CAP': '券商戶頭餘額低於資金總上限',
    'PRIOR_EXECUTION_ERROR': '前次委託執行錯誤',
    'OTHER_HEALTH_WARNING': '健康檢查仍有警示',
}
ORDER_LEDGER_LABELS = {
    'FILLED': '全部成交', 'PARTIALLY_FILLED': '部分成交，餘量未結',
    'PARTIALLY_FILLED_CANCELLED': '部分成交，餘量已取消',
    'ACKNOWLEDGED': '券商已受理', 'UNCONFIRMED': '券商回覆待確認',
    'CANCELLED': '已取消',
    'EXPIRED': 'ROD 跨日失效',
    'PARTIALLY_FILLED_EXPIRED': '部分成交，ROD 餘量跨日失效',
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


def update_detail_text(widget, content):
    """Keep the reader's position while refreshing the analysis tab."""
    if widget.get('1.0', 'end-1c') == content:
        return
    scroll_position = widget.yview()[0]
    widget.configure(state='normal')
    widget.delete('1.0', 'end')
    widget.insert('1.0', content)
    widget.configure(state='disabled')
    widget.yview_moveto(scroll_position)


def inventory_temporarily_busy(snapshot):
    return bool(snapshot and snapshot.get('inventory_checked') is False
                and any('rc=1019' in str(error) and 'inventory:' in str(error)
                        for error in snapshot.get('errors') or []))


def confirmed_trade_gross(checks):
    """Summarize verified gross fills without assigning an unknown fill date."""
    totals = {'BUY': Decimal('0'), 'SELL': Decimal('0')}
    by_date = {}
    undated = False
    incomplete = False
    for check in checks:
        try:
            quantity = int(check.get('broker_verified_filled_qty',
                                     check.get('broker_replay_filled_qty')) or 0)
        except (TypeError, ValueError):
            incomplete = True
            continue
        if quantity <= 0:
            continue
        side = check.get('side')
        if side not in totals:
            incomplete = True
            continue
        fills = check.get('broker_replay_fill_details') or []
        if fills:
            try:
                if sum(int(fill['quantity']) for fill in fills) != quantity:
                    raise ValueError('成交股數不符')
                order_total = Decimal('0')
                order_by_date = {}
                order_undated = False
                for fill in fills:
                    amount = Decimal(str(fill['price_twd'])) * int(fill['quantity'])
                    if not amount.is_finite() or amount <= 0:
                        raise ValueError('成交價金無效')
                    order_total += amount
                    trade_date = fill.get('trade_date')
                    try:
                        trade_day = date.fromisoformat(str(trade_date))
                    except (TypeError, ValueError):
                        order_undated = True
                    else:
                        order_by_date[trade_day] = (
                            order_by_date.get(trade_day, Decimal('0')) + amount)
                totals[side] += order_total
                undated = undated or order_undated
                for trade_day, amount in order_by_date.items():
                    day_totals = by_date.setdefault(trade_day, {
                        'BUY': Decimal('0'), 'SELL': Decimal('0')})
                    day_totals[side] += amount
            except (KeyError, TypeError, ValueError, InvalidOperation):
                incomplete = True
        else:
            try:
                amount = Decimal(str(check['broker_replay_fill_gross_twd']))
                if not amount.is_finite() or amount < 0:
                    raise InvalidOperation
                totals[side] += amount
                undated = True
            except (KeyError, TypeError, ValueError, InvalidOperation):
                incomplete = True
    return totals, by_date, undated, incomplete


def settlement_windows(grouped, today):
    """Separate dated broker net amounts from any claim of completed payment."""
    windows = {key: {'payable': Decimal('0'), 'receivable': Decimal('0')}
               for key in ('future', 'today', 'past')}
    for due_day, entries in grouped.items():
        net = sum((amount for _, amount in entries), Decimal('0'))
        key = 'future' if due_day > today else 'today' if due_day == today else 'past'
        windows[key]['payable'] += max(Decimal('0'), -net)
        windows[key]['receivable'] += max(Decimal('0'), net)
    return windows


def settlement_snapshot_current(checked_at, at):
    """A recent broker query is required before labeling figures as current."""
    try:
        checked = datetime.fromisoformat(str(checked_at))
        if checked.tzinfo is None:
            return False
        age = (at - checked.astimezone(at.tzinfo)).total_seconds()
        return checked.astimezone(at.tzinfo).date() == at.date() and 0 <= age <= 900
    except (TypeError, ValueError, OverflowError):
        return False


class Dashboard:
    def __init__(self, root, directory=ROOT/'data'/'analysis', interval=AUTO_INTERVAL_SECONDS,
                 lifecycle=None):
        self.root = root
        self.lifecycle = lifecycle
        self.last_lifecycle_heartbeat = time.monotonic()
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
        self.inventory_retry_attempts = 0
        self.inventory_retry_scheduled = False
        self.heartbeat = 0
        self.strategy_audit_cache = {}
        self.simulation_window = None
        root.title('STOCKLLM｜台股行情與 AI 分析')
        root.geometry('1500x920')
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.main_tabs = ttk.Notebook(root)
        self.main_tabs.pack(fill='both',expand=True)
        self.live_page = ttk.Frame(self.main_tabs)
        self.sim_page = ttk.Frame(self.main_tabs)
        self.main_tabs.add(self.live_page,text='正式交易')
        self.main_tabs.add(self.sim_page,text='策略模擬')
        self.main_tabs.bind('<<NotebookTabChanged>>',self.on_main_tab_changed)
        style = ttk.Style(root)
        style.configure('Treeview', rowheight=29)
        style.configure('Treeview.Heading', font=('Microsoft JhengHei UI', 10, 'bold'))
        style.configure('CapitalReadonly.TEntry', fieldbackground='#FFF2CC', foreground='#333333')
        style.map('CapitalReadonly.TEntry',
                  fieldbackground=[('readonly', '#FFF2CC')],
                  foreground=[('readonly', '#333333')])
        self.status_text = tk.StringVar()
        self.system_status_text = tk.StringVar()
        ttk.Label(self.live_page, textvariable=self.status_text, anchor='w',
                  padding=(8, 5), font=('Microsoft JhengHei UI', 10, 'bold')).pack(
                      fill='x', padx=20, pady=(8, 4))
        summary = ttk.Frame(self.live_page)
        summary.pack(fill='x', padx=20, pady=(0, 4))
        for column, weight in enumerate((2, 3, 2)):
            summary.columnconfigure(column, weight=weight)
        self.paper_text = tk.StringVar(value='資金總上限尚未設定；按紅色按鈕輸入上限。')
        self.finance_text = tk.StringVar(value='券商資金：等待對帳。')
        self.settlement_text = tk.StringVar(value='交割資料：等待券商查詢。')
        for column, (title, variable, color) in enumerate((
                ('券商資金', self.finance_text, '#184D47'),
                ('AI 額度', self.paper_text, '#8B0000'),
                ('交割概況（券商預估）', self.settlement_text, '#6B4E16'))):
            card = ttk.LabelFrame(summary, text=title, padding=(8, 5))
            card.grid(row=0, column=column, sticky='nsew',
                      padx=(0, 6) if column < 2 else 0)
            ttk.Label(card, textvariable=variable, anchor='w', justify='left',
                      wraplength=440, foreground=color,
                      font=('Microsoft JhengHei UI', 10, 'bold')).pack(
                          fill='both', expand=True)
            if column == 0:
                limits = ttk.Frame(card)
                limits.pack(fill='x', pady=(6, 0))
                limits.columnconfigure(1, weight=1)
                self.paper_limit_var = tk.StringVar(value='尚未設定')
                self.daily_limit_var = tk.StringVar(value='讀取中')
                for row, (label, value) in enumerate((
                        ('資金總上限', self.paper_limit_var),
                        ('單日買進上限', self.daily_limit_var))):
                    ttk.Label(limits, text=label).grid(row=row, column=0, sticky='w', pady=2)
                    ttk.Entry(limits, width=12, textvariable=value, state='readonly',
                              style='CapitalReadonly.TEntry').grid(
                                  row=row, column=1, sticky='ew', padx=5, pady=2)
                self.paper_capital_button = tk.Button(
                    limits, text='設定／提高', command=self.save_paper_capital,
                    bg='#C62828', fg='white', activebackground='#8E0000',
                    activeforeground='white', relief='flat', padx=8, pady=3,
                    font=('Microsoft JhengHei UI', 9, 'bold'))
                self.paper_capital_button.grid(row=0, column=2, sticky='ew', pady=2)
                self.daily_limit_button = tk.Button(
                    limits, text='設定／調整', command=self.save_daily_buy_limit,
                    bg='#184D47', fg='white', activebackground='#103B36',
                    activeforeground='white', relief='flat', padx=8, pady=3,
                    font=('Microsoft JhengHei UI', 9, 'bold'))
                self.daily_limit_button.grid(row=1, column=2, sticky='ew', pady=2)
        self.spend_text = tk.StringVar(value='正式 AI 委託成交：請重新整理並與券商對帳。')
        self.system_budget_text = tk.StringVar()
        self.system_finance_text = tk.StringVar()
        self.system_settlement_text = tk.StringVar()
        self.budget_reason_text = tk.StringVar(value='買進新委託狀態：待券商對帳。')
        ttk.Label(self.live_page, textvariable=self.budget_reason_text, anchor='w', justify='left',
                  padding=(8, 3), foreground='#8B4513').pack(fill='x', padx=20,
                                                              pady=(0, 3))
        bar = ttk.Frame(self.live_page)
        bar.pack(fill='x', padx=20, pady=(0, 7))
        self.run_button = ttk.Button(bar, text='分析並送交實盤風控', command=self.manual)
        self.run_button.pack(side='left', padx=(0, 8), ipady=3)
        for label, command in [('開始自動掃描', self.start_auto), ('停止自動掃描', self.stop_auto),
                               ('重新整理並與券商對帳', lambda: self.refresh(sync_broker=True)), ('開啟最近分析資料夾', self.open_folder),
                               ('設定持倉', self.position_settings)]:
            ttk.Button(bar, text=label, command=command).pack(side='left', padx=4, ipady=3)
        ttk.Button(bar, text='選擇 MD 策略', command=self.choose_live_strategy).pack(
            side='left', padx=4, ipady=3)
        ttk.Label(bar, text='目前策略：').pack(side='left', padx=(8, 0))
        self.live_strategy_text = tk.StringVar()
        ttk.Label(bar, textvariable=self.live_strategy_text,
                  font=('Microsoft JhengHei UI', 9, 'bold')).pack(side='left')
        self.update_live_strategy_label()
        self.market_text = tk.StringVar(value='行情摘要：尚未取得資料')
        self.notebook = ttk.Notebook(self.live_page)
        self.notebook.pack(fill='both', expand=True, padx=20, pady=(0, 8))
        self.top_tree = self.table('量化排名', ['名次','股票代號','股票名稱','最新價','行情狀態','量化分數','AI 建議','信心度'])
        self.union_tree = self.table('AI 分析清單',
            ['股票代號','股票名稱','納入原因','量化排名','目前 AI 持股','持股核對','持股對帳時間','建議','信心度'],
            hint='目前 AI 持股取自最近券商對帳後的正式帳本；建議與量化排名仍屬分析當時結果。')
        self.position_tree = self.table('持倉概況',
            ['股票代號','股票名稱','券商現股','AI 管理股數','AI 運用','人工設定股數','AI 最近買進策略','參考價','持倉參考市值','行情時間','對帳狀態','對帳時間'],
            hint='選取股票後按「設定持倉」，或雙擊股票，設定 AI 管理股數；設為 0 即禁止 AI 買賣。')
        self.position_tree.bind('<Double-1>', lambda event: self.position_settings())
        self.quote_tree = self.table('行情與時間（台北時間 UTC+8）', ['股票代號','股票名稱','行情狀態','最新價','資料年齡（秒）','行情時間','請求開始','資料收到','請求完成'])
        self.broker_position_tree = self.table('券商持倉對帳', ['對帳時間','股票代號','券商現股','正式 AI 股數','人工設定股數','差異狀態'])
        self.live_trade_tree = self.table('正式委託與成交紀錄',
            ['委託時間','股票代號','方向','採用策略','AI 理由','委託股數','帳本成交股數','券商回報成交明細',
             '券商回報成交價金','未確認成交股數','已取消餘量','已失效餘量','券商事件推定','事件數',
             '委託價','帳本狀態','券商回報核對','券商序號','分析回合'])
        settlement_tab = ttk.Frame(self.notebook)
        self.notebook.add(settlement_tab, text='交割明細')
        self.settlement_status_text = tk.StringVar(value='等待券商查詢交割資料。')
        ttk.Label(settlement_tab, textvariable=self.settlement_status_text,
                  anchor='w', justify='left', wraplength=1380,
                  padding=(10, 8)).pack(fill='x')
        ttk.Button(settlement_tab, text='查看正式委託與成交紀錄',
                   command=lambda: self.notebook.select(self.live_trade_tree.master)).pack(
                       anchor='w', padx=10, pady=(0, 6))
        settlement_grid = ttk.Frame(settlement_tab)
        settlement_grid.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        settlement_grid.columnconfigure(0, weight=1)
        settlement_grid.rowconfigure(0, weight=1)
        settlement_columns = ('交易日', '應付', '應收', '淨額',
                              '已核實買進', '已核實賣出', '交割／對照狀態')
        self.settlement_tree = ttk.Treeview(
            settlement_grid, columns=settlement_columns, show='tree headings')
        self.settlement_tree.heading('#0', text='交割日／展開日彙總')
        self.settlement_tree.column('#0', width=250, minwidth=200, stretch=False)
        for column, width in (('交易日', 130), ('應付', 150), ('應收', 150),
                              ('淨額', 160), ('已核實買進', 150),
                              ('已核實賣出', 150), ('交割／對照狀態', 310)):
            self.settlement_tree.heading(column, text=column)
            self.settlement_tree.column(column, width=width,
                                        minwidth=width if column != '交割／對照狀態' else 220,
                                        anchor='w' if column == '交割／對照狀態' else 'e')
        settlement_y = ttk.Scrollbar(settlement_grid, orient='vertical',
                                     command=self.settlement_tree.yview)
        settlement_x = ttk.Scrollbar(settlement_grid, orient='horizontal',
                                     command=self.settlement_tree.xview)
        self.settlement_tree.configure(yscrollcommand=settlement_y.set,
                                       xscrollcommand=settlement_x.set)
        self.settlement_tree.grid(row=0, column=0, sticky='nsew')
        settlement_y.grid(row=0, column=1, sticky='ns')
        settlement_x.grid(row=1, column=0, sticky='ew')
        detail = ttk.Frame(self.notebook)
        self.notebook.add(detail, text='分析結果與理由')
        self.detail = tk.Text(detail, wrap='word', padx=14, pady=12, font=('Microsoft JhengHei UI', 10), spacing1=2, spacing3=5)
        self.detail.pack(fill='both', expand=True, padx=8, pady=8)
        self.detail.configure(state='disabled')
        system = ttk.Frame(self.notebook)
        self.notebook.add(system, text='系統資訊')
        for title, variable in (
                ('執行與健康檢查：', self.system_status_text),
                ('AI 額度與持倉參考市值：', self.system_budget_text),
                ('券商餘額與新買預算檢查：', self.system_finance_text),
                ('交割資料：', self.system_settlement_text),
                ('券商成交與風控占用：', self.spend_text),
                ('行情掃描：', self.market_text)):
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
            'AI 最近買進策略': 180, '採用策略': 175, 'AI 理由': 360,
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

    def update_live_strategy_label(self):
        try:
            strategy = strategy_catalog.live_selection()
            self.live_strategy_text.set(Path(strategy['path']).name)
        except Exception as exc:
            self.live_strategy_text.set('策略檔案無法讀取：' + redact(exc))

    def choose_live_strategy(self):
        path = filedialog.askopenfilename(
            parent=self.root, title='選擇正式交易的 MD 策略', initialdir=str(ROOT),
            filetypes=[('Markdown 策略', '*.md')])
        if not path:
            return
        try:
            strategy_catalog.save_live_selection(path)
            self.update_live_strategy_label()
        except Exception as exc:
            messagebox.showerror('策略選擇失敗', redact(exc), parent=self.root)

    def show_live(self):
        self.main_tabs.select(self.live_page)

    def open_simulation(self):
        if self.simulation_window is None or not self.simulation_window.window.winfo_exists():
            from simulation_window import SimulationWindow
            self.simulation_window = SimulationWindow(self.sim_page)
            self.simulation_window.window.pack(fill='both',expand=True)
        self.main_tabs.select(self.sim_page)

    def on_main_tab_changed(self, _event=None):
        if self.main_tabs.select() == str(self.sim_page):
            self.open_simulation()

    def run_strategy_info(self, run_id, symbol):
        if not run_id or not re.fullmatch(r'[A-Za-z0-9_-]+', str(run_id)):
            return '--', '--'
        if run_id not in self.strategy_audit_cache:
            directory = self.service.directory/'runs'/run_id
            try:
                manifest = json.loads((directory/'run.json').read_text(encoding='utf-8'))
                decision = json.loads((directory/'selection_decision.json').read_text(encoding='utf-8'))
                strategy = manifest.get('strategy') or {}
                label = f"{strategy.get('name','--')} v{strategy.get('version','--')}" if strategy else '--'
                reasons = {d['symbol']: d.get('reason','--') for d in decision.get('decisions', [])}
                self.strategy_audit_cache[run_id] = (label, reasons)
            except (OSError, ValueError, KeyError):
                self.strategy_audit_cache[run_id] = ('--', {})
        label, reasons = self.strategy_audit_cache[run_id]
        return label, reasons.get(symbol, '--')

    def historical_buy_strategy_labels(self, symbols):
        """Find the latest confirmed AI BUY even when it is older than health's 100 rows."""
        if not symbols:
            return {}
        path = ROOT/'data'/'execution'/'live_execution.sqlite3'
        if not path.is_file():
            return {}
        labels = {}
        try:
            with closing(sqlite3.connect('file:'+str(path.resolve())+'?mode=ro',uri=True,timeout=5)) as db:
                for run_id,symbol,detail_json,status in db.execute(
                        "SELECT run_id,symbol,detail_json,status FROM execution_attempts "
                        "WHERE side='BUY' ORDER BY started_at DESC"):
                    if symbol not in symbols or symbol in labels:
                        continue
                    detail = json.loads(detail_json or '{}')
                    if status not in ('FILLED','PARTIALLY_FILLED','PARTIALLY_FILLED_CANCELLED') \
                            and int(detail.get('filled_quantity') or 0) <= 0:
                        continue
                    label,_ = self.run_strategy_info(run_id,symbol)
                    if label != '--':
                        labels[symbol] = label
                    if len(labels) == len(symbols):
                        break
        except (OSError, sqlite3.Error, ValueError):
            return labels
        return labels

    def start_auto(self):
        if paper_portfolio.account(self.service.db_path) is None:
            messagebox.showinfo('資金總上限', '請先在主畫面設定資金總上限。')
            return
        if self.balance_hold:
            messagebox.showwarning('餘額下限', '券商資金低於設定的保留額，已停止自動掃描。請確認餘額並等待下一次健康檢查。')
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
                '此金額是本次 AI 交易活動的累計資金上限；實際買進前仍須核對券商餘額。'
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
        if not settlement_snapshot_current(
                (self.last_health or {}).get('checked_at'), now()):
            messagebox.showerror('券商餘額資料逾時',
                                 '請先按「重新整理並與券商對帳」，取得近期一戶通餘額。',
                                 parent=self.root)
            return
        balance_raw = (self.last_health or {}).get('broker_one_account_balance_twd')
        try:
            broker_balance = Decimal(str(balance_raw))
            if not broker_balance.is_finite():
                raise ValueError()
        except (TypeError, ValueError, InvalidOperation):
            messagebox.showerror('券商餘額未確認',
                                 '請先按「重新整理並與券商對帳」，確認一戶通餘額後再設定資金總上限。',
                                 parent=self.root)
            return
        if Decimal(amount) > broker_balance:
            messagebox.showerror('資金總上限高於券商餘額',
                                 f'目前券商一戶通餘額為 NT$ {broker_balance:,.2f}；'
                                 '資金總上限不得超過此餘額。', parent=self.root)
            return
        if not messagebox.askyesno(
            '確認資金總上限',
            f'將 AI 交易的資金總上限設定為 NT$ {amount:,}。\n'
            '可買額以資金總上限的剩餘額為基礎，並扣除已投入與未結保留；仍須符合券商餘額、最低保留額及當日額度檢查。\n\n'
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

    def save_daily_buy_limit(self):
        config_path = ROOT/'config'/'execution_live.json'
        try:
            current = LiveExecutionConfig.load(config_path).max_daily_buy_twd
        except Exception as exc:
            messagebox.showerror('單日上限讀取失敗', redact(exc), parent=self.root)
            return
        amount = simpledialog.askinteger(
            '設定單日買進上限',
            '請輸入新的單日買進上限（新台幣整數）。\n'
            f'目前上限：NT$ {current:,.0f}\n'
            '可調高或調低；不受券商餘額限制。已預留的當日買單仍會占用額度。',
            parent=self.root, initialvalue=int(current), minvalue=1)
        if amount is None:
            return
        if not messagebox.askyesno(
            '確認單日買進上限',
            f'將單日買進上限設定為 NT$ {amount:,}。\n'
            '變更後，新買單會依當日已預留金額重新計算剩餘額度。\n\n確認套用嗎？',
            parent=self.root):
            return
        self.daily_limit_button.configure(state='disabled')
        def work():
            try:
                LiveExecutionConfig.set_daily_buy_limit(config_path, amount)
                self.events.put(('daily_limit_saved', amount))
            except Exception as exc:
                self.events.put(('daily_limit_error', redact(f'{type(exc).__name__}: {exc}')))
        threading.Thread(target=work, name='daily-limit-worker', daemon=True).start()

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

    def schedule_inventory_refresh(self, delay_ms=12000):
        if (self.closing or self.inventory_retry_scheduled
                or self.inventory_retry_attempts >= 3
                or not inventory_temporarily_busy(self.last_health)):
            return
        self.inventory_retry_scheduled = True

        def retry():
            self.inventory_retry_scheduled = False
            if self.closing or not inventory_temporarily_busy(self.last_health):
                return
            if self.refresh_pending or self.scheduler.scan_lock.locked():
                self.schedule_inventory_refresh(delay_ms=2000)
                return
            self.inventory_retry_attempts += 1
            self.refresh(sync_broker=True)

        self.root.after(delay_ms, retry)

    def tick(self):
        self.heartbeat += 1
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == 'phase':
                    self.status = payload
                elif kind == 'result':
                    self.result, self.positions = payload, payload['positions']
                    self.inventory_retry_attempts = 0
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
                    if self.lifecycle:
                        self.lifecycle.event('WORKER_ERROR', worker='analysis', error=payload)
                elif kind == 'finished':
                    self.scheduler.finish()
                    self.schedule_inventory_refresh()
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
                        self.schedule_inventory_refresh()
                elif kind == 'refresh_error':
                    self.refresh_pending = False
                    self.last_error = payload
                    if self.lifecycle:
                        self.lifecycle.event('WORKER_ERROR', worker='refresh', error=payload)
                    self.root.configure(cursor='')
                    self.schedule_inventory_refresh()
                elif kind == 'capital_saved':
                    self.root.configure(cursor='')
                    self.paper_capital_button.configure(state='normal')
                    self.paper_account_configured = True
                    account = payload['account']
                    self.paper_limit_var.set(f"{account['capital_limit_cents'] // 100:,}")
                    self.next_health_at = time.time()
                    self.refresh(sync_broker=True)
                    messagebox.showinfo(
                        '資金總上限已更新',
                        f"資金總上限：NT$ {account['capital_limit_cents'] // 100:,}\n"
                        '正在重新整理並與券商對帳。',
                        parent=self.root
                    )
                elif kind == 'capital_error':
                    self.root.configure(cursor='')
                    self.paper_capital_button.configure(state='normal')
                    messagebox.showerror('資金總上限設定失敗', payload, parent=self.root)
                elif kind == 'daily_limit_saved':
                    self.daily_limit_button.configure(state='normal')
                    self.daily_limit_var.set(f'{payload:,}')
                    self.next_health_at = time.time()
                    self.refresh(sync_broker=True)
                    messagebox.showinfo('單日買進上限已更新',
                                        f'單日買進上限：NT$ {payload:,}\n'
                                        '正在重新整理與券商對帳，更新當日剩餘額度。',
                                        parent=self.root)
                elif kind == 'daily_limit_error':
                    self.daily_limit_button.configure(state='normal')
                    messagebox.showerror('單日買進上限設定失敗', payload,
                                         parent=self.root)
                elif kind == 'position_saved':
                    win, result = payload
                    if win.winfo_exists():
                        win.destroy()
                    self.refresh(sync_broker=True)
                    messagebox.showinfo('持倉設定已更新',
                        f"{result['symbol']}：AI 管理 {result['ai_qty']} 股，人工持有 {result['user_qty']} 股。\n"
                        + ('已禁止 AI 買進與賣出此股票。' if not result['enabled'] else 'AI 可以運用此股票。'),
                        parent=self.root)
                elif kind == 'position_save_error':
                    win, button, error = payload
                    if win.winfo_exists():
                        button.configure(state='normal')
                    messagebox.showerror('持倉設定失敗', error,
                                         parent=win if win.winfo_exists() else self.root)
                elif kind == 'health':
                    self.health_running = False
                    self.last_health = payload
                    self.next_health_at = time.time() + self.health_delay()
                    self.balance_hold = bool(payload.get('balance_below_floor'))
                    if self.balance_hold:
                        self.scheduler.stop_auto()
                        self.status = 'PAUSED_LOW_BALANCE'
                    self.render()
                    self.schedule_inventory_refresh()
                elif kind == 'health_error':
                    self.health_running = False
                    self.last_error = payload
                    self.next_health_at = time.time() + 60
                    if self.lifecycle:
                        self.lifecycle.event('WORKER_ERROR', worker='health', error=payload)
                elif kind == 'broker_shutdown_complete':
                    self.broker_shutdown_complete = True
                    if self.lifecycle:
                        self.lifecycle.event('BROKER_SHUTDOWN_COMPLETE')
                elif kind == 'broker_shutdown_error':
                    self.last_error = payload
                    self.broker_shutdown_complete = True
                    if self.lifecycle:
                        self.lifecycle.event('BROKER_SHUTDOWN_ERROR', error=payload)
        except queue.Empty:
            pass
        wall, mono = time.time(), time.monotonic()
        if self.lifecycle and mono - self.last_lifecycle_heartbeat >= 60:
            self.last_lifecycle_heartbeat = mono
            self.lifecycle.event('HEARTBEAT', status=self.status, closing=self.closing,
                                 analysis_running=self.scheduler.scan_lock.locked(),
                                 health_running=self.health_running,
                                 refresh_pending=self.refresh_pending,
                                 auto_enabled=self.scheduler.next_auto is not None,
                                 last_error=self.last_error)
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
                if self.lifecycle:
                    self.lifecycle.event('WINDOW_DESTROY', last_error=self.last_error)
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
        broker_balance = health.get('broker_one_account_balance_twd')
        current_buy_budget = health.get('current_buy_budget_twd')
        fill_mismatch = any(
            row.get('status') == 'FILL_MISMATCH'
            for row in health.get('broker_order_reconciliation') or [])
        pending_orders = health.get('unresolved_attempts') or []
        health_status = health.get('status', '待檢查')
        health_label = {
            'OK': '正常', 'PENDING_ORDER': '未結委託（待券商回報）',
            'WARN': '警示', 'ALERT': '異常', 'ERROR': '錯誤',
        }.get(health_status, health_status)
        health_note = ('（券商成交股數與正式帳本不一致；請重新整理對帳）' if fill_mismatch else
                       f'（{len(pending_orders)} 筆委託尚未結束；未結買單餘量如有，已納入預算保留）'
                       if health_status == 'PENDING_ORDER' else '')
        if health_status in ('WARN', 'ALERT', 'ERROR'):
            reasons = [BUY_BLOCK_LABELS.get(code, code)
                       for code in health.get('buy_block_reasons') or []]
            if reasons:
                health_note += '（' + '；'.join(dict.fromkeys(reasons)) + '）'
            if pending_orders:
                health_note += f'（另有 {len(pending_orders)} 筆未結委託）'
        if health.get('last_scan_status') == 'ERROR':
            health_note += '（上次分析失敗；請查看分析紀錄）'
        if health.get('last_execution_status') == 'ERROR':
            health_note += '（上次委託執行失敗；請查看正式委託紀錄）'
        if health.get('latest_health_write_warning'):
            health_note += '（健康檢查檔案寫入受阻；券商資料已另存正式資料庫）'
        broker_line = (f"券商戶頭餘額：{broker_balance if broker_balance is not None else '未確認'} 元　"
                       f"持倉核對：{'不一致 '+','.join(mismatches) if mismatches else '相符' if comparisons else '未確認'}　"
                       f"未解委託：{len(health.get('unresolved_attempts') or [])} 筆")
        checked_at = (health.get('checked_at') or '--').replace('T', ' ')
        if checked_at != '--':
            checked_at = checked_at[5:19]
        broker_balance_label = (f'NT$ {float(broker_balance):,.0f}'
                                if broker_balance is not None else '待確認')
        budget_label = (f'NT$ {float(current_buy_budget):,.0f}'
                        if current_buy_budget is not None else '待確認')
        auto_label = (
            f'下次 {datetime.fromtimestamp(nxt, TAIPEI):%m/%d %H:%M}，'
            f'倒數 {max(0, math.ceil(nxt-wall))} 秒'
            if nxt else '未啟用（倒數 --）')
        self.status_text.set(
            f'現在時間 {now():%m/%d %H:%M:%S}（台北）　｜　'
            f'自動掃描：{auto_label}　｜　'
            f'目前狀態：{STATUS_LABELS.get(self.status, self.status)}　｜　'
            f'健康：{health_label}　｜　'
            f'券商對帳：{checked_at}　｜　'
            f'資料驗證：{"券商庫存已查詢" if health.get("broker_inventory_verified") else "未完成"}\n'
            f'券商餘額：{broker_balance_label}　｜　'
            f'未解委託：{len(health.get("unresolved_attempts") or [])} 筆　｜　'
            f'新買單預算：{budget_label}　｜　'
            f'正式委託：{len(health.get("execution_attempts") or [])} 筆　｜　'
            f'持倉核對：{"不一致" if mismatches else "相符" if comparisons else "待核對"}')
        self.system_status_text.set(
            f'現在時間：{now().isoformat(timespec="seconds")}（台北時間 UTC+8）\n'
            f'最近完成分析：{self.last_scan or "尚無"}\n\n'
            f'自動掃描：{("下次 " + stamp(nxt) + "　倒數 " + str(max(0, int(nxt-wall))) + " 秒") if nxt else "未啟用"}　　'
            f'掃描間隔：{self.scheduler.interval // 60} 分鐘　　因分析進行中而略過的自動掃描：{self.scheduler.skipped} 次\n'
            f'手動分析：{stamp(self.scheduler.last_manual)}　　冷卻時間：{str(cooldown)+" 秒" if cooldown else "可立即使用"}\n\n'
            f'目前狀態：{STATUS_LABELS.get(self.status, self.status)}　　最近錯誤：{self.last_error or "無"}\n'
            f'健康檢查：{health_label}{health_note}'
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
        self.update_live_strategy_label()
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
            strategy = r['manifest'].get('strategy') or {}
            detail_text = (f"正式交易策略：{strategy.get('name','--')} v{strategy.get('version','--')}\n\n"
                           if strategy else '')
            detail_text += decision_text(r['decision'], r['analysis_set'],
                r['manifest'].get('llm_response_received_at'), r.get('execution_budget'))
            if r['manifest'].get('display_notice'):
                detail_text = r['manifest']['display_notice'] + '\n\n' + detail_text
            update_detail_text(self.detail, detail_text)
        self.broker_position_tree.delete(*self.broker_position_tree.get_children())
        for row in health.get('inventory_comparison') or []:
            self.broker_position_tree.insert('', 'end', values=[
                health.get('checked_at') or '--', row.get('symbol'), row.get('broker_cash_qty'),
                row.get('ai_qty'), row.get('configured_user_qty'),
                row.get('status') if verified else '券商查詢未完整驗證'])
        self.live_trade_tree.delete(*self.live_trade_tree.get_children())
        order_checks = {x['decision_id']: x for x in health.get('broker_order_reconciliation') or []}
        latest_buy_strategy = {}
        for attempt in health.get('execution_attempts') or []:
            if attempt.get('side') != 'BUY':
                continue
            detail = attempt.get('detail') or {}
            check = order_checks.get(attempt.get('decision_id'), {})
            filled = int(check.get('broker_verified_filled_qty') or detail.get('filled_quantity') or 0)
            if filled <= 0:
                continue
            label, _ = self.run_strategy_info(attempt.get('run_id'), attempt.get('symbol'))
            symbol = attempt.get('symbol')
            stamp = attempt.get('started_at') or ''
            if label != '--' and (symbol not in latest_buy_strategy or stamp > latest_buy_strategy[symbol][0]):
                latest_buy_strategy[symbol] = (stamp, label)
        held_ai_symbols = {row['symbol'] for row in health.get('inventory_comparison') or []
                           if int(row.get('ai_qty') or 0) > 0}
        for symbol,label in self.historical_buy_strategy_labels(
                held_ai_symbols-set(latest_buy_strategy)).items():
            latest_buy_strategy[symbol] = ('',label)
        for attempt in health.get('execution_attempts') or []:
            detail = attempt.get('detail') or {}
            order_check = order_checks.get(attempt.get('decision_id'), {})
            strategy_label, ai_reason = self.run_strategy_info(
                attempt.get('run_id'), attempt.get('symbol'))
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
                strategy_label, ai_reason,
                detail.get('quantity', '--'), detail.get('filled_quantity', '--'),
                '；'.join(fill_parts) if fill_parts else '--', gross_text,
                order_check.get('unconfirmed_qty', '--'),
                order_check.get('cancelled_qty', '--'),
                order_check.get('expired_qty', 0),
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
                 'USER_CANCEL_CONFIRMED_NO_FILL':'使用者確認取消，未見成交',
                 'USER_FILL_CONFIRMED_REPLAY_NOT_RETURNED':'使用者確認成交，本次未重播',
                 'NO_FILL_YET_VERIFIED':'券商已受理，尚未見成交',
                 'ROD_EXPIRED':'ROD 已跨日失效，依帳本保留成交紀錄',
                 'FILL_UNVERIFIED':'成交尚未確認'}.get(
                    order_check.get('status'),
                    order_check.get('status', '無券商成交核對')),
                detail.get('seq13', '--'), attempt.get('run_id')])
        checks = health.get('broker_order_reconciliation') or []
        fill_totals, fills_by_date, undated_fills, incomplete_fills = (
            confirmed_trade_gross(checks))
        unverified_orders = (not health.get('checked_at')
                             or (not checks and health.get('execution_attempts'))
                             or any(a.get('decision_id') not in order_checks
                                    for a in health.get('execution_attempts') or []))
        fills_pending = (not health.get('checked_at')
                         or (not checks and health.get('execution_attempts')))
        buy_fill_text = ('待核對' if fills_pending else
                         f'NT$ {fill_totals["BUY"]:,.2f}')
        sell_fill_text = ('待核對' if fills_pending else
                          f'NT$ {fill_totals["SELL"]:,.2f}')
        fill_scope = ('；另有成交價待確認' if incomplete_fills else '')
        if unverified_orders:
            fill_scope += '；另有委託待核對'
        if undated_fills:
            fill_scope += '；部分成交未附交易日，不能歸入逐日交割'
        reserved = health.get('daily_buy_reserved_twd')
        try:
            reserved_text = (f'NT$ {Decimal(str(reserved)):,.2f}'
                             if reserved is not None else '待確認')
        except (ValueError, InvalidOperation):
            reserved_text = '待確認'
        self.spend_text.set(
            f'正式 AI 委託已核實買進成交價金：{buy_fill_text}　'
            f'賣出成交價金：{sell_fill_text}\n'
            f'成交核對時間：{health.get("checked_at") or "--"}'
            f'{fill_scope}。本次快照最多涵蓋最近 100 筆正式委託；'
            '以上為跨日成交價金合計，未含實際費稅，並非交割款。\n'
            f'當日 BUY 風控額度占用（含已成交與未結委託，非交割款）：{reserved_text}')
        block_reasons = [BUY_BLOCK_LABELS.get(code, code)
                         for code in health.get('buy_block_reasons') or []]
        closed_reason = twse_closed_reason(now().date())
        if closed_reason:
            self.budget_reason_text.set(
                f'新買委託：證交所今日休市（{closed_reason}）；目前行情不可用於交易，請於下個交易日再掃描。')
        elif health.get('budget_status') == 'READY':
            self.budget_reason_text.set(
                '新買委託：可依可新買額度評估；送單前仍會重新查券商餘額。')
        elif inventory_temporarily_busy(health):
            self.budget_reason_text.set(
                '新買委託暫停：券商庫存查詢繁忙；'
                + ('系統會自動重新對帳。' if self.inventory_retry_attempts < 3
                   else '自動重試未恢復，請稍後按「重新整理並與券商對帳」。'))
        elif block_reasons:
            self.budget_reason_text.set(
                '新買委託暫停：' + '、'.join(block_reasons) +
                '。詳見「系統資訊」。')
        else:
            self.budget_reason_text.set('新買委託：待券商對帳確認。')
        account = paper_portfolio.account(self.service.db_path)
        self.paper_limit_var.set(f"{account['capital_limit_cents']/100:,.0f}" if account else '尚未設定')
        try:
            daily_limit = LiveExecutionConfig.load(
                ROOT/'config'/'execution_live.json').max_daily_buy_twd
            self.daily_limit_var.set(f'{daily_limit:,.0f}')
        except (OSError, ValueError, InvalidOperation, json.JSONDecodeError):
            self.daily_limit_var.set('讀取失敗')
        def money(raw):
            try:
                amount = Decimal(str(raw))
                return f'NT$ {amount:,.2f}' if amount.is_finite() else '待確認'
            except (TypeError, ValueError, InvalidOperation):
                return '待確認'
        dues = health.get('broker_settlement_dues') or []
        balance_evidence = health.get('settlement_balance_evidence') or {}
        def balance_note(due_day):
            item = balance_evidence.get(due_day.isoformat()) or {}
            status = item.get('status')
            if status == 'BALANCE_MATCH':
                return (f'截至 {item.get("observed_at") or "--"} 一戶通餘額 '
                        f'{money(item.get("baseline_balance_twd"))} → '
                        f'{money(item.get("observed_balance_twd"))}，'
                        f'變化 {money(item.get("observed_delta_twd"))}；'
                        '與當日淨額吻合，仍非交割完成證明')
            if status == 'BALANCE_DIFFERENT':
                return (f'截至 {item.get("observed_at") or "--"} 一戶通餘額變化 '
                        f'{money(item.get("observed_delta_twd"))}；'
                        '與當日淨額不符，可能另有收支')
            if status == 'MULTIPLE_DUE_DATES':
                return '兩個以上交割日跨越查詢空檔，無法歸因餘額變化'
            return '缺交割前後可比較餘額，無法推估'
        current_time = now()
        settlement_current = settlement_snapshot_current(
            health.get('checked_at'), current_time)
        expanded_dates = {
            self.settlement_tree.item(item, 'text')
            for item in self.settlement_tree.get_children()
            if self.settlement_tree.item(item, 'open')
        }
        self.settlement_tree.delete(*self.settlement_tree.get_children())
        if health.get('settlement_query_error'):
            self.settlement_text.set('交割款：券商查詢失敗\n目前應收付無法確認')
            self.system_settlement_text.set('券商近三個交易日的交割資料暫不可用；請重新整理並與券商對帳。')
            self.settlement_status_text.set(
                '券商交割查詢失敗；目前沒有可核實的預計交割明細。\n'
                f'正式 AI 委託已核實買進 {buy_fill_text}、賣出 {sell_fill_text}；'
                '此為成交價金，尚無法與交割淨額核對。')
        elif dues:
            grouped = {}
            invalid_rows = 0
            for row in dues:
                try:
                    due_day = datetime.strptime(row['settlement_date'], '%Y-%m-%d').date()
                    trade_day = datetime.strptime(row['trade_date'], '%Y-%m-%d').date()
                    amount = Decimal(str(row['net_twd']))
                    if not amount.is_finite():
                        raise ValueError('非有限金額')
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    invalid_rows += 1
                    continue
                grouped.setdefault(due_day, []).append((trade_day, amount))
            if invalid_rows or not grouped:
                self.settlement_text.set('交割款：資料格式異常\n目前應收付無法確認')
                self.system_settlement_text.set('券商交割資料格式不完整；請重新整理並與券商對帳。')
                self.settlement_status_text.set(
                    f'券商交割資料有 {invalid_rows} 筆格式不符，暫不顯示不完整的合計；請重新整理並與券商對帳。')
            else:
                windows = settlement_windows(grouped, current_time.date())
                future = windows['future']
                today_dues = windows['today']
                past = windows['past']
                for due_day in sorted(grouped):
                    entries = grouped[due_day]
                    net = sum((amount for _, amount in entries), Decimal('0'))
                    if due_day > current_time.date():
                        due_status = '尚未到交割日'
                    elif due_day == current_time.date():
                        due_status = '今日交割；扣款／入帳待核實'
                    else:
                        due_status = '交割日已過；是否完成待核實'
                    if not settlement_current:
                        due_status = '券商資料逾時；' + due_status
                    if due_day <= current_time.date() and net != 0:
                        due_status += '；' + balance_note(due_day)
                    label = f'{due_day:%m/%d}（{due_day:%Y-%m-%d}）'
                    parent = self.settlement_tree.insert('', 'end', text=label,
                        open=label in expanded_dates,
                        values=('--', money(max(Decimal('0'), -net)),
                                money(max(Decimal('0'), net)), f'NT$ {net:+,.2f}',
                                '--', '--', due_status))
                    for index, (trade_day, amount) in enumerate(entries, 1):
                        tracked = fills_by_date.get(trade_day)
                        compared = ('缺實際費稅與完整券商成交，尚無法核對'
                                    if tracked else '無逐筆成交日期，尚無法核對'
                                    if undated_fills else '尚無可對照成交，無法判定')
                        self.settlement_tree.insert(parent, 'end',
                            text=f'券商日彙總 {index}',
                            values=(trade_day.isoformat(),
                                    money(max(Decimal('0'), -amount)),
                                    money(max(Decimal('0'), amount)),
                                    f'NT$ {amount:+,.2f}',
                                    money(tracked['BUY']) if tracked else '--',
                                    money(tracked['SELL']) if tracked else '--',
                                    compared))
                pending_days = [day for day, entries in grouped.items()
                                if day > current_time.date()
                                and sum((amount for _, amount in entries),
                                        Decimal('0')) != 0]
                nearest_text = (f'{min(pending_days):%m/%d}' if pending_days else '--')
                if settlement_current:
                    today_text = (f'今日待核：扣 {money(today_dues["payable"])}／'
                                  f'入 {money(today_dues["receivable"])}'
                                  if any(today_dues.values()) else '今日無交割款')
                    if any(today_dues.values()):
                        today_evidence = (balance_evidence.get(
                            current_time.date().isoformat()) or {})
                        if today_evidence.get('status') == 'BALANCE_MATCH':
                            today_text += '；餘額曾吻合（推估）'
                        elif today_evidence.get('status') == 'BALANCE_DIFFERENT':
                            today_text += '；餘額未吻合'
                    past_days = [day for day, entries in grouped.items()
                                 if day < current_time.date()
                                 and sum((amount for _, amount in entries),
                                         Decimal('0')) != 0]
                    matched_past = sum(
                        (balance_evidence.get(day.isoformat()) or {}).get('status')
                        == 'BALANCE_MATCH' for day in past_days)
                    past_text = (f'\n已過交割日 {len(past_days)} 日：'
                                 f'{matched_past} 日餘額曾吻合（推估）'
                                 if past_days else '')
                    self.settlement_text.set(
                        f'未到期將扣：{money(future["payable"])}　'
                        f'將收：{money(future["receivable"])}\n'
                        f'{today_text}　下次 {nearest_text}\n'
                        f'實際未扣／未入：無法核實{past_text}')
                else:
                    self.settlement_text.set(
                        '券商交割資料已逾時\n請重新整理；暫不顯示當前應收付')
                self.system_settlement_text.set(
                    f'券商查詢時間：{health.get("checked_at") or "--"}；'
                    f'未到期將扣 {money(future["payable"])}、將收 {money(future["receivable"])}；'
                    f'今日交割待核實：應付 {money(today_dues["payable"])}、'
                    f'應收 {money(today_dues["receivable"])}；'
                    f'已過交割日回傳：應付 {money(past["payable"])}、'
                    f'應收 {money(past["receivable"])}。'
                    '已過日期的回傳金額不代表仍未付款。券商 API 無完成交割旗標；'
                    '實際未扣與未入帳金額須核對一戶通或銀行交易明細。'
                    '交割款未從券商當日可買額重複扣除。')
                today_balance_text = (f'{balance_note(current_time.date())}。'
                                      if any(today_dues.values()) else '')
                self.settlement_status_text.set(
                    f'券商查詢時間：{health.get("checked_at") or "--"}'
                    + ('（資料逾時，請重新整理）' if not settlement_current else '')
                    + '\n'
                    f'未到期將扣：{money(future["payable"])}　'
                    f'未到期將收：{money(future["receivable"])}\n'
                    f'今日交割待核：應付 {money(today_dues["payable"])}　'
                    f'應收 {money(today_dues["receivable"])}；{today_balance_text}'
                    f'已過日期回傳：應付 {money(past["payable"])}　'
                    f'應收 {money(past["receivable"])}（不視為未付／未收）。\n'
                    '實際未扣款與未入帳金額：券商資料無完成旗標，無法確認；'
                    '請核對一戶通或銀行交易明細。\n'
                    f'正式 AI 委託已核實買進成交價金：{buy_fill_text}　'
                    f'賣出成交價金：{sell_fill_text}（最多最近 100 筆，跨日合計）。'
                    '點交割日左側箭頭可看交易日淨額；成交價金不含實際費稅。'
                    + ('部分成交缺交易日，不能歸入逐日對照。' if undated_fills else '')
                    + ('另有成交價待確認。' if incomplete_fills else '')
                    + '目前尚無法判定是否有未追蹤款項。')
        else:
            self.settlement_text.set('交割款：待券商查詢\n目前應收付無法確認')
            self.system_settlement_text.set('券商近三個交易日的交割資料：待券商查詢。')
            self.settlement_status_text.set(
                '等待券商查詢交割資料；請重新整理並與券商對帳。\n'
                f'正式 AI 委託已核實買進 {buy_fill_text}、賣出 {sell_fill_text}；'
                '此為成交價金，尚無法與交割淨額核對。')
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
        buy_budget = money(health.get('current_buy_budget_twd'))
        if health.get('budget_status') == 'BLOCKED':
            buy_budget += '（暫停新買單）'
        self.paper_text.set(
            f'總上限：{cap}　已投入：{money(health.get("ai_committed_capital_twd"))}\n'
            f'未結保留：{money(health.get("pending_buy_reserve_twd"))}　'
            f'上限剩餘：{money(health.get("strategy_remaining_twd"))}\n'
            f'AI 可新買：{buy_budget}　持倉參考市值：{market_value}')
        self.system_budget_text.set(
            f'AI 資金總上限：{cap}　AI 持倉投入估算（成交價＋費用緩衝）：'
            f'{money(health.get("ai_committed_capital_twd"))}　'
            f'未結 BUY 安全保留：{money(health.get("pending_buy_reserve_twd"))}\n'
            f'總上限剩餘：{money(health.get("strategy_remaining_twd"))}　'
            f'可供新買：{buy_budget}　持倉參考市值：{market_value}')
        self.finance_text.set(
            f'一戶通餘額：{money(health.get("broker_one_account_balance_twd"))}\n'
            f'當日買進已預留：{money(health.get("daily_buy_reserved_twd"))}　'
            f'剩餘：{money(health.get("daily_buy_remaining_twd"))}')
        self.system_finance_text.set(
            f'券商一戶通餘額：{money(health.get("broker_one_account_balance_twd"))}　'
            f'可出金：{money(health.get("broker_withdrawable_twd"))}　'
            f'查詢時間：{health.get("checked_at") or "--"}\n'
            f'本次交易以資金總上限剩餘 {money(health.get("strategy_remaining_twd"))} 為累計額度；'
            f'本輪可新買 {buy_budget}。券商戶頭餘額須至少達到資金總上限，'
            f'送單後券商資金仍須保留至少 '
            f'{money(health.get("minimum_balance_twd"))}，且不得超過當日剩餘額度 '
            f'{money(health.get("daily_buy_remaining_twd"))}。送單前會重新核對券商資金。')
        for row in broker_rows:
            symbol = row['symbol']
            quote = fresh_quotes.get(symbol)
            qty = int(row.get('broker_cash_qty') or 0)
            ai_qty = int(row.get('ai_qty') or 0)
            if not verified or not 0 <= ai_qty <= qty:
                ai_usage = '待核對'
            elif ai_qty == 0:
                ai_usage = '不可運用'
            elif ai_qty == qty:
                ai_usage = '全數運用'
            else:
                ai_usage = '部分運用'
            reference = f'{quote[1]:,.2f}' if quote else '--'
            value_twd = f'NT$ {qty*quote[1]:,.2f}' if quote else '--'
            self.position_tree.insert('', 'end', values=[
                symbol, quote[2] if quote and quote[2] else names.get(symbol, '股票名稱未取得'),
                qty if verified else '--', row.get('ai_qty') if verified else '--',
                ai_usage,
                row.get('configured_user_qty') if verified else '--',
                latest_buy_strategy.get(symbol, ('','--'))[1] if verified and int(row.get('ai_qty') or 0)>0 else '--',
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
        if self.service.directory.resolve() != (ROOT/'data'/'analysis').resolve():
            messagebox.showinfo('持倉設定', '正式 AI 持股設定請在正式資料目錄的 Dashboard 操作。')
            return
        win = tk.Toplevel(self.root)
        win.title('設定每檔股票的 AI 管理股數')
        health = self.last_health or {}
        rows = {row['symbol']: row for row in health.get('inventory_comparison') or []}
        symbols = sorted(set(rows) | {p['symbol'] for p in self.positions})
        symbol = tk.StringVar()
        ai_qty = tk.StringVar(value='0')
        cost = tk.StringVar()
        summary = tk.StringVar()
        ttk.Label(win, text='股票代號').grid(row=0, column=0, padx=12, pady=9, sticky='w')
        choice = ttk.Combobox(win, textvariable=symbol, values=symbols)
        choice.grid(row=0, column=1, padx=12, pady=9, sticky='ew')
        ttk.Label(win, textvariable=summary, wraplength=390).grid(row=1, columnspan=2, padx=12, pady=8, sticky='w')
        ttk.Label(win, text='AI 管理股數').grid(row=2, column=0, padx=12, pady=9, sticky='w')
        ttk.Entry(win, textvariable=ai_qty).grid(row=2, column=1, padx=12, pady=9, sticky='ew')
        ttk.Label(win, text='轉入股數的每股成本').grid(row=3, column=0, padx=12, pady=9, sticky='w')
        ttk.Entry(win, textvariable=cost).grid(row=3, column=1, padx=12, pady=9, sticky='ew')

        def load_symbol(*unused):
            row = rows.get(symbol.get().strip(), {})
            ai_qty.set(str(row.get('ai_qty') or 0))
            cost.set('')
            summary.set(f"最近對帳：券商現股 {row.get('broker_cash_qty', '待查詢')} 股，"
                        f"目前 AI 管理 {row.get('ai_qty', '待查詢')} 股。\n儲存時會重新與券商對帳。")
        choice.bind('<<ComboboxSelected>>', load_symbol)
        choice.bind('<FocusOut>', load_symbol)
        selected = self.position_tree.selection()
        symbol.set(str(self.position_tree.item(selected[0], 'values')[0]) if selected
                   else symbols[0] if symbols else '')
        load_symbol()

        def save():
            try:
                target = symbol.get().strip().upper()
                shares = int(ai_qty.get().strip())
                unit = Decimal(cost.get().strip()) if cost.get().strip() else None
                if shares < 0 or (unit is not None and (not unit.is_finite() or unit <= 0)):
                    raise ValueError()
            except (ValueError, InvalidOperation):
                messagebox.showerror('格式錯誤','股數須為非負整數；轉入成本須為正數，沒有增加股數時可留白。',parent=win)
                return
            if not self.scheduler.scan_lock.acquire(blocking=False):
                messagebox.showinfo('持倉設定', '目前分析進行中，完成後再儲存。', parent=win)
                return
            save_button.configure(state='disabled')
            def work():
                try:
                    from execution.position_configuration import configure_ai_holding
                    result = configure_ai_holding(ROOT, self.service.db_path, target, shares,
                                                  unit_cost_twd=unit)
                    self.events.put(('position_saved', (win, result)))
                except Exception as exc:
                    self.events.put(('position_save_error', (win, save_button, redact(exc))))
                finally:
                    self.scheduler.scan_lock.release()
            threading.Thread(target=work, daemon=True).start()
        win.columnconfigure(1, weight=1)
        save_button = ttk.Button(win,text='儲存持倉設定',command=save)
        save_button.grid(row=4,columnspan=2,pady=(12,8),ipady=3)
        ttk.Label(win, wraplength=400, justify='left', text=
            'AI 管理股數設為 0，即禁止 AI 買進與賣出此股票。\n'
            '股數不可超過券商現股；其餘股數歸為人工持有。\n'
            '增加 AI 股數時必須填入轉入成本。此設定會更新持股歸屬，不會送出委託。'
        ).grid(row=5,columnspan=2,padx=12,pady=(4,12),sticky='w')

    def close(self):
        if self.closing:
            return
        self.closing = True
        if self.lifecycle:
            self.lifecycle.event('CLOSE_REQUESTED', status=self.status,
                                 analysis_running=self.scheduler.scan_lock.locked(),
                                 health_running=self.health_running)
        if self.simulation_window and not self.simulation_window.closed:
            self.simulation_window.close()
        self.scheduler.stop_auto()
        self.service.stop.set()
        if self.scheduler.scan_lock.locked():
            self.status = '等待本輪分析完成並儲存資料'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=ROOT/'data'/'analysis')
    parser.add_argument('--test-mode', action='store_true')
    parser.add_argument('--resume-auto', action='store_true',
                        help='resume the previously enabled automatic scan after restart')
    parser.add_argument('--interval', type=int, default=AUTO_INTERVAL_SECONDS)
    args = parser.parse_args()
    if args.interval <= 0 or (args.interval != AUTO_INTERVAL_SECONDS and not args.test_mode):
        parser.error('非 1800 秒間隔需要 --test-mode；間隔須大於 0')
    lifecycle = LifecycleLog(ROOT / 'data' / 'health')
    lifecycle.start()
    root = None
    app = None
    fatal = False
    old_thread_hook = threading.excepthook
    def thread_exception(args):
        lifecycle.exception('THREAD_EXCEPTION', args.exc_type, args.exc_value, args.exc_traceback)
        old_thread_hook(args)
    threading.excepthook = thread_exception
    try:
        # comtypes imports initialize the importing thread; explicit worker STA
        # initialization is paired separately inside AnalysisService.
        import comtypes
        root = tk.Tk()
        def callback_exception(exc_type, exc_value, exc_tb):
            lifecycle.exception('TK_CALLBACK_EXCEPTION', exc_type, exc_value, exc_tb)
            traceback.print_exception(exc_type, exc_value, exc_tb)
        root.report_callback_exception = callback_exception
        app = Dashboard(root, args.data_dir, args.interval, lifecycle=lifecycle)
        if args.resume_auto:
            root.after(1000, app.start_auto)
        lifecycle.event('WINDOW_READY', data_dir=str(args.data_dir.resolve()),
                        interval_seconds=args.interval, test_mode=args.test_mode)
        root.mainloop()
    except BaseException:
        fatal = True
        lifecycle.exception('FATAL_EXCEPTION', *sys.exc_info())
        raise
    finally:
        threading.excepthook = old_thread_hook
        lifecycle.finish('FATAL_EXCEPTION' if fatal else
                         'NORMAL_CLOSE' if app and app.closing else 'MAINLOOP_ENDED')


if __name__ == '__main__':
    main()
