"""Dedicated Paper-only desktop. No import/call to live order dispatchers."""
import argparse
from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
import queue
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk

from config import ROOT, CODE_ROOT, now, redact
from main import save_json, single_instance
from .paper_runtime import PaperRuntime, market_open


class Dashboard:
    def __init__(self, root, *, auto=False):
        self.root = root
        self.root.title('Strategy A/B — 計畫限價 Paper 自動掃描（禁止實盤）')
        self.root.geometry('1160x820')
        self.events = queue.Queue()
        self.enabled, self.stopped, self.prepare_requested = threading.Event(), threading.Event(), threading.Event()
        if auto:
            self.enabled.set()
        self.prepare_requested.set()
        self.last = {}
        self.closing = False
        self.directory = ROOT/'data/planned_paper'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.control_path = self.directory/'dashboard_status.json'
        ttk.Label(root, text='PAPER ONLY｜模擬委託與模擬資金｜不送券商委託｜LLM 呼叫 0',
                  font=('Microsoft JhengHei', 16, 'bold'), foreground='#14663b').pack(anchor='w', padx=16, pady=12)
        ttk.Label(root, text='分支 codex/planned-limit-orders · 即時行情每 30 秒 · 候選重評每 5 分鐘 · A／B 各 NT$100,000 模擬資金').pack(anchor='w', padx=16)
        self.status = tk.StringVar(value='初始化；自動掃描啟用中' if auto else '自動掃描未啟用')
        self.detail = tk.StringVar(value='正在準備日線與獨立 Paper 帳本；休市不連接券商行情。')
        ttk.Label(root, textvariable=self.status, font=('Microsoft JhengHei', 14, 'bold')).pack(anchor='w', padx=16, pady=(12, 4))
        ttk.Label(root, textvariable=self.detail, wraplength=1110).pack(anchor='w', padx=16)
        controls = ttk.Frame(root)
        controls.pack(fill='x', padx=16, pady=12)
        ttk.Button(controls, text='開始 Paper 自動掃描', command=self.start).pack(side='left', padx=(0, 8))
        ttk.Button(controls, text='停止自動掃描', command=self.stop).pack(side='left', padx=8)
        ttk.Button(controls, text='更新日線', command=self.prepare_requested.set).pack(side='left', padx=8)
        ttk.Label(root, text='資金／狀態（以下皆為 Paper；不代表券商帳戶）').pack(anchor='w', padx=16)
        self.accounts = self.table(root, ('策略', '現金', '等待單預留', '可用資金', '持股檔數'), height=2)
        ttk.Label(root, text='等待中委託與成交狀態').pack(anchor='w', padx=16, pady=(8, 0))
        self.orders = self.table(root, ('策略', '股票', '方向', '限價', '委託股數', '已成交', '狀態', '到期（台灣）'), height=7)
        ttk.Label(root, text='已確認 Paper 持倉').pack(anchor='w', padx=16, pady=(8, 0))
        self.positions = self.table(root, ('策略', '股票', '股數', '含費成本', '停利參考', '停損參考'), height=4)
        ttk.Label(root, text='資料品質／候選淘汰原因／錯誤紀錄').pack(anchor='w', padx=16, pady=(8, 0))
        self.notes = tk.Text(root, height=9, wrap='word', font=('Microsoft JhengHei', 10))
        self.notes.pack(fill='both', expand=True, padx=16, pady=(0, 12))
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.worker = threading.Thread(target=self.work, name='PaperMarketSTA', daemon=True)
        self.worker.start()
        self.root.after(300, self.poll)

    @staticmethod
    def table(root, columns, height):
        tree = ttk.Treeview(root, columns=columns, show='headings', height=height)
        for c in columns:
            tree.heading(c, text=c)
            tree.column(c, width=130, stretch=True)
        tree.pack(fill='x', padx=16)
        return tree

    def start(self):
        self.enabled.set()
        self.status.set('Paper 自動掃描已啟用；等待工作執行緒回報')

    def stop(self):
        self.enabled.clear()
        self.status.set('已要求停止；正在進行的唯讀資料請求會完成後停止')

    def work(self):
        runtime = None
        try:
            runtime = PaperRuntime(stop_requested=lambda: not self.enabled.is_set() or self.stopped.is_set())
            runtime.set_auto(self.enabled.is_set())
            self.events.put(runtime.state.copy())
            due = 0
            while not self.stopped.is_set():
                if runtime.auto_enabled != self.enabled.is_set():
                    runtime.set_auto(self.enabled.is_set())
                    due = 0
                    self.events.put(runtime.state.copy())
                if self.prepare_requested.is_set():
                    self.prepare_requested.clear()
                    self.events.put(dict(runtime.state, status='PREPARING_HISTORY'))
                    try:
                        runtime.prepare()
                    except Exception as exc:
                        runtime.state.update(status='HISTORY_ERROR', errors=[redact(str(exc))])
                        runtime.publish()
                    self.events.put(runtime.state.copy())
                if self.enabled.is_set() and time.monotonic() >= due:
                    self.events.put(dict(runtime.state, status='SCANNING' if market_open(now()) else 'WAIT_SESSION'))
                    self.events.put(runtime.step())
                    due = time.monotonic()+(runtime.quote_interval if market_open(now()) else 60)
                self.stopped.wait(1)
        except Exception:
            error = redact(traceback.format_exc())
            self.events.put(dict(status='FATAL_ERROR', errors=[error], live_enabled=False))
            (self.directory/'dashboard_error.log').write_text(error, encoding='utf-8')
        finally:
            if runtime:
                runtime.close()

    def render(self, s):
        labels = {'WAIT_SESSION': '待命：休市／非交易時段', 'RUNNING': '運行中：即時行情掃描',
            'PREPARING_HISTORY': '準備中：更新多日日線', 'WAIT_VALID_QUOTES': '等待有效零股行情',
            'STOPPED': '已停止', 'ERROR_RETRY_PENDING': '資料錯誤；等待下次重試',
            'SCANNING': '掃描中：唯讀券商行情', 'FATAL_ERROR': '啟動失敗；請查看下方錯誤',
            'READY': '準備完成', 'AUTO_ENABLED': '自動掃描已啟用', 'HISTORY_ERROR': '日線更新失敗'}
        self.status.set(('自動掃描 ON｜' if self.enabled.is_set() else '自動掃描 OFF｜')+labels.get(s.get('status'), str(s.get('status'))))
        next_at = s.get('next_market_open_taipei')
        japan = datetime.fromisoformat(next_at).astimezone(timezone(timedelta(hours=9))).strftime('%m/%d %H:%M') if next_at else '待確認'
        self.detail.set(f"下次開市：日本 {japan}／台灣 {next_at or '待確認'}　最近行情掃描：{s.get('last_scan_at') or '尚未開市掃描'}　掃描輪次：{s.get('cycles', 0)}")
        for tree in (self.accounts, self.orders, self.positions):
            tree.delete(*tree.get_children())
        for strategy, account in s.get('accounts', {}).items():
            self.accounts.insert('', 'end', values=(strategy, f"{account['cash']:,.2f}", f"{account['reserved']:,.2f}", f"{account['available']:,.2f}", len(account['positions'])))
            for o in account['orders'][-40:]:
                self.orders.insert('', 'end', values=(strategy, o['symbol'], o['side'], o['limit_price'], o['qty'], o['filled_qty'], o['status'], o['expires_at']))
            for symbol, p in account['positions'].items():
                self.positions.insert('', 'end', values=(strategy, symbol, p['qty'], f"{p['cost']:,.2f}", p.get('target'), p.get('stop')))
        counts = s.get('history_bar_counts', {})
        stale = sum(not h.get('latest_date') or h.get('latest_date', '') < h.get('expected_latest_date', '')
                    for h in s.get('history', {}).values())
        lines = [f"模式：PAPER_ONLY；真實資金下單：禁止；LLM 呼叫：0；股票池 {s.get('symbol_count', 0)} 檔",
                 f"行情狀態：{s.get('market_data_status', '尚未檢查')}；有效報價 {s.get('live_quote_count', 0)} 檔",
                 f"日線：{len(counts)} 檔；筆數範圍 {min(counts.values(), default=0)}～{max(counts.values(), default=0)}",
                 f"日線尚未更新至預期交易日：{stale} 檔；過期資料不能建立新買單，盤中每 15 分鐘重試更新。",
                 '模型保留至少 30 組不重疊歷史樣本門檻；沒有通過訊號時會等待，不保證每日成交。',
                 f"獨立帳本：{s.get('experiment_directory', '')}"]
        for strategy, summary in s.get('last_ranking_summary', s.get('summary', {})).items():
            lines.append(f"{strategy}：{summary}")
        lines.extend(s.get('errors', []))
        self.notes.delete('1.0', 'end')
        self.notes.insert('end', '\n'.join(lines))

    def poll(self):
        while True:
            try:
                self.last = self.events.get_nowait()
                self.render(self.last)
            except queue.Empty:
                break
        save_json(self.control_path, dict(pid=os.getpid(), heartbeat_at=now().isoformat(),
            auto_requested=self.enabled.is_set(), worker_alive=self.worker.is_alive(),
            mode='PAPER_ONLY', live_enabled=False, branch='codex/planned-limit-orders',
            runtime_status=self.last.get('status'), code_root=str(CODE_ROOT),
            experiment_directory=self.last.get('experiment_directory')))
        self.root.after(1000, self.poll)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.enabled.clear()
        self.stopped.set()
        self.status.set('正在停止；等待唯讀請求完成後關閉視窗')
        save_json(self.control_path, dict(pid=os.getpid(), heartbeat_at=now().isoformat(),
            mode='PAPER_ONLY', live_enabled=False, auto_requested=False, runtime_status='CLOSED'))
        self.wait_close()

    def wait_close(self):
        if self.worker.is_alive():
            self.root.after(200, self.wait_close)
        else:
            self.root.destroy()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--auto', action='store_true')
    a = p.parse_args()
    directory = ROOT/'data/planned_paper'
    directory.mkdir(parents=True, exist_ok=True)
    with single_instance(directory/'dashboard.lock'):
        root = tk.Tk()
        Dashboard(root, auto=a.auto)
        root.mainloop()


if __name__ == '__main__':
    main()
