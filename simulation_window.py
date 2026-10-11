"""Embedded Tk page for forward-only parallel strategy experiments."""
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from config import ROOT, now, redact
from scheduler import Scheduler, AUTO_INTERVAL_SECONDS, is_twse_oddlot_market_window
from strategy_experiment import ExperimentService, ExperimentStore
from execution.auto_advice_executor import LiveExecutionConfig
import strategy_catalog


def money(value):
    return '--' if value is None else f'NT$ {value:,.2f}'


CHART_SIZES = (0, 120, 200, 280, 320)


class SimulationWindow:
    def __init__(self, parent):
        self.window = ttk.Frame(parent)
        self.store = ExperimentStore()
        self.service = ExperimentService(self.store)
        self.scheduler = Scheduler(AUTO_INTERVAL_SECONDS,
                                   allowed_slot=is_twse_oddlot_market_window,
                                   phase_offset_seconds=300)
        self.events = queue.Queue()
        self.running = False
        self.closed = False
        self.last_result = None
        self.status = tk.StringVar(value='模擬模式待命；只使用行情資料，不送券商委託。')
        ttk.Label(self.window,textvariable=self.status,padding=(12,8),
                  font=('Microsoft JhengHei UI',10,'bold')).pack(fill='x')
        settings = self.store.settings()
        config = ttk.LabelFrame(self.window,text='下一個策略的初始資金',padding=8)
        config.pack(fill='x',padx=12,pady=5)
        self.capital = tk.StringVar(value=str(settings['initial_cents']//100))
        ttk.Label(config,text='初始資金 TWD').grid(row=0,column=0,sticky='w',padx=(4,2))
        self.capital_entry = ttk.Entry(config,textvariable=self.capital,width=14)
        self.capital_entry.grid(row=0,column=1,padx=(0,12))
        self.settings_button = ttk.Button(config,text='儲存下次預設資金',command=self.save_settings)
        self.settings_button.grid(row=0,column=2,padx=4)
        live = LiveExecutionConfig.load(ROOT/'config'/'execution_live.json')
        order_limit = (f'NT$ {live.max_order_twd:,.0f}'
                       if live.max_order_twd is not None else '未限制')
        self.rules_note = tk.StringVar(value=(
            f'正式版資金規則：保留 NT$ {live.min_available_to_buy_twd:,.0f}；每日 BUY 上限 '
            f'NT$ {live.max_daily_buy_twd:,.0f}；單筆上限 {order_limit}；'
            f'買入費用預留至少 NT$ {live.min_buy_fee_reserve_twd:,.0f} 或 {live.buy_cash_buffer_rate:.1%}。'
            '各策略獨立設定資金上限；資金保留下限與正式版相同。'))
        ttk.Label(config,textvariable=self.rules_note,wraplength=1390).grid(
            row=1,column=0,columnspan=3,sticky='w',pady=(8,0))
        ttk.Label(config, text=(
            '模擬買進採用同輪觀察到的賣價，賣出採用買價；不另加滑價。'
            '忽略微小時間差，實際成交價仍可能不同。API Token 不另設額度或輸入欄位。'
            '修改「下一個策略初始資金」不影響既有策略，既有策略可在自己的頁籤提高上限。'),
            wraplength=1390,foreground='#555555').grid(
                row=2,column=0,columnspan=3,sticky='w',pady=(4,0))

        controls = ttk.Frame(self.window)
        controls.pack(fill='x',padx=12,pady=6)
        ttk.Button(controls,text='加入 AI 策略 MD',command=lambda:self.add_strategy('AI')).pack(side='left',padx=4)
        ttk.Button(controls,text='加入固定規則基準',command=lambda:self.add_strategy('RULE')).pack(side='left',padx=4)
        ttk.Button(controls,text='啟用／停用所選策略',command=self.toggle_selected).pack(side='left',padx=4)
        ttk.Button(controls,text='查看所選策略',command=self.show_selected_strategy).pack(side='left',padx=4)
        self.run_button = ttk.Button(controls,text='立即模擬一輪',command=self.manual)
        self.run_button.pack(side='left',padx=(20,4))
        ttk.Button(controls,text='開始自動模擬',command=self.start_auto).pack(side='left',padx=4)
        ttk.Button(controls,text='停止自動模擬',command=self.stop_auto).pack(side='left',padx=4)
        ttk.Button(controls,text='更新畫面',command=self.render).pack(side='left',padx=4)

        self.views = ttk.Notebook(self.window)
        self.views.pack(fill='both',expand=True,padx=12,pady=(0,10))
        self.overview = ttk.Frame(self.views)
        self.views.add(self.overview,text='績效總覽')
        self.strategy_widgets = {}
        self.views.bind('<<NotebookTabChanged>>',lambda _:self.render_current_strategy())
        table_frame = ttk.Frame(self.overview)
        table_frame.pack(fill='x',padx=12,pady=(3,8))
        table_frame.columnconfigure(0,weight=1)
        self.table = ttk.Treeview(table_frame,show='headings',height=5,
            columns=('策略','狀態','開始時間','版本','資金上限','資產淨值','報酬率','勝率','最大回撤',
                     'Profit Factor','交易次數'))
        widths = {'策略':155,'狀態':65,'開始時間':180,'版本':100,'資金上限':125,
                  '資產淨值':130,'報酬率':90,
                  '勝率':80,'最大回撤':100,'Profit Factor':105,'交易次數':80}
        for col in self.table['columns']:
            self.table.heading(col,text=col)
            self.table.column(col,width=widths[col],minwidth=70,anchor='center')
        table_y = ttk.Scrollbar(table_frame,orient='vertical',command=self.table.yview)
        table_x = ttk.Scrollbar(table_frame,orient='horizontal',command=self.table.xview)
        self.table.configure(yscrollcommand=table_y.set,xscrollcommand=table_x.set)
        self.table.grid(row=0,column=0,sticky='ew')
        table_y.grid(row=0,column=1,sticky='ns')
        table_x.grid(row=1,column=0,sticky='ew')
        self.table.bind('<Double-1>',lambda _:self.show_selected_strategy())
        chart_bar = ttk.Frame(self.overview)
        chart_bar.pack(fill='x',padx=12,pady=(0,3))
        ttk.Label(chart_bar,text='資產淨值趨勢（含中途加額）').pack(side='left')
        self.chart_height = 120
        self.chart_size_text = tk.StringVar()
        ttk.Label(chart_bar,textvariable=self.chart_size_text).pack(side='right',padx=(8,0))
        self.chart_shrink_button = ttk.Button(chart_bar,text='▼ 縮小',command=self.shrink_chart)
        self.chart_shrink_button.pack(side='right',padx=3)
        self.chart_expand_button = ttk.Button(chart_bar,text='▲ 放大',command=self.expand_chart)
        self.chart_expand_button.pack(side='right',padx=3)
        self.chart = tk.Canvas(self.overview,height=self.chart_height,bg='white',highlightthickness=1,
                               highlightbackground='#dddddd')
        self.chart.pack(fill='x',padx=12,pady=(0,8))
        self.chart.bind('<Configure>',lambda _:self.draw_chart())
        self.update_chart_size()
        self.render()
        self.window.after(250,self.tick)

    def update_chart_size(self):
        if self.chart_height:
            self.chart.configure(height=self.chart_height)
            if not self.chart.winfo_manager():
                self.chart.pack(fill='x',padx=12,pady=(0,8))
            self.chart_size_text.set(f'{self.chart_height} px')
            self.draw_chart()
        else:
            self.chart.pack_forget()
            self.chart_size_text.set('已收合')
        self.chart_expand_button.configure(state='normal' if self.chart_height<320 else 'disabled')
        self.chart_shrink_button.configure(state='normal' if self.chart_height>0 else 'disabled')

    def expand_chart(self):
        self.chart_height = CHART_SIZES[min(len(CHART_SIZES)-1,CHART_SIZES.index(self.chart_height)+1)]
        self.update_chart_size()

    def shrink_chart(self):
        self.chart_height = CHART_SIZES[max(0,CHART_SIZES.index(self.chart_height)-1)]
        self.update_chart_size()

    def save_settings(self):
        try:
            self.store.configure(self.capital.get())
        except Exception as exc:
            messagebox.showerror('模擬設定',redact(exc),parent=self.window)
        self.render()

    def add_strategy(self,kind):
        if self.running:
            return
        path = (ROOT/'strategies'/'fixed_rules.md') if kind=='RULE' else filedialog.askopenfilename(
            parent=self.window,title='選擇 AI 策略 Markdown',initialdir=str(ROOT/'strategies'),
            filetypes=[('Markdown 策略','*.md')])
        if not path:
            return
        try:
            source = strategy_catalog.read_strategy(path)
            existing = next((s for s in self.store.strategies()
                             if s['source_path'] == source['path']
                             and s['sha256'] == source['sha256']
                             and s['kind'] == kind), None)
            if existing:
                messagebox.showinfo(
                    '策略已在模擬中',
                    f"「{existing['name']}」的相同檔案版本已加入模擬"
                    f"（目前{'啟用' if existing['active'] else '停用'}）。\n"
                    '既有模擬帳戶與紀錄會保留。',
                    parent=self.window)
                if existing['id'] in self.strategy_widgets:
                    self.views.select(self.strategy_widgets[existing['id']]['frame'])
                return
            self.store.configure(self.capital.get())
            sid = self.store.add_strategy(path,kind,initial_twd=self.capital.get())
        except Exception as exc:
            messagebox.showerror('加入策略失敗',redact(exc),parent=self.window)
            return
        self.render()
        self.views.select(self.strategy_widgets[sid]['frame'])

    def selected_strategy_id(self):
        current = self.views.select()
        for sid,widgets in self.strategy_widgets.items():
            if str(widgets['frame']) == current:
                return sid
        selected = self.table.selection()
        return selected[0] if selected else None

    def show_selected_strategy(self):
        selected = self.table.selection()
        if selected and selected[0] in self.strategy_widgets:
            self.views.select(self.strategy_widgets[selected[0]]['frame'])

    def toggle_selected(self):
        sid = self.selected_strategy_id()
        if not sid or self.running:
            return
        strategy = next((s for s in self.store.strategies() if s['id']==sid),None)
        if strategy:
            self.store.set_active(sid,not strategy['active'])
        self.render()

    def increase_capital(self, sid):
        if self.running:
            return
        strategy = next((s for s in self.store.strategies() if s['id']==sid),None)
        if strategy is None:
            return
        current = strategy['capital_limit_cents']//100
        amount = simpledialog.askinteger(
            '提高模擬資金上限',
            f"{strategy['name']} 目前上限：NT$ {current:,}\n"
            '輸入新的總上限（新台幣整數）；只能維持或增加。',
            parent=self.window,initialvalue=current,minvalue=current)
        if amount is None or amount == current:
            return
        if not messagebox.askyesno('確認模擬加額',
                f"將 {strategy['name']} 的模擬資金上限提高至 NT$ {amount:,}。\n"
                f'增加 NT$ {amount-current:,}，會記為投入資金，不計入策略獲利。',
                parent=self.window):
            return
        try:
            self.store.increase_capital(sid,amount)
        except Exception as exc:
            messagebox.showerror('模擬加額失敗',redact(exc),parent=self.window)
        self.render()

    def delete_strategy(self, sid):
        if self.running:
            messagebox.showinfo('刪除策略','本輪模擬完成後才能刪除策略。',parent=self.window)
            return
        strategy = next((s for s in self.store.strategies() if s['id']==sid),None)
        if strategy is None:
            return
        if not messagebox.askyesno('刪除模擬策略與紀錄',
                f"刪除「{strategy['name']}」及其模擬帳戶、持倉、決策、訂單、成交與資金紀錄？\n"
                '此操作無法從模擬畫面復原；其他策略的紀錄會保留。',
                parent=self.window):
            return
        try:
            self.store.delete_strategy(sid)
        except Exception as exc:
            messagebox.showerror('刪除策略失敗',redact(exc),parent=self.window)
        self.views.select(self.overview)
        self.render()

    def manual(self):
        if not self.running and self.scheduler.manual(time.time(),time.monotonic()):
            self.launch('MANUAL')

    def start_auto(self):
        if not self.store.strategies(active_only=True):
            messagebox.showinfo('自動模擬','請先加入並啟用策略。',parent=self.window)
            return
        self.scheduler.start_auto(time.time())
        self.status.set('自動模擬已啟用；盤中每 30 分鐘共用一輪行情。')

    def stop_auto(self):
        self.scheduler.stop_auto()
        self.status.set('自動模擬已停止；既有策略帳戶與前向紀錄保留。')

    def launch(self,trigger):
        self.running = True
        self.run_button.configure(state='disabled')
        def work():
            try:
                result = self.service.run(trigger,lambda phase:self.events.put(('phase',phase)))
                self.events.put(('result',result))
            except Exception as exc:
                self.events.put(('error',redact(f'{type(exc).__name__}: {exc}')))
            finally:
                self.events.put(('finished',None))
        threading.Thread(target=work,name='simulation-worker',daemon=True).start()

    def tick(self):
        if self.closed:
            return
        try:
            while True:
                kind,payload = self.events.get_nowait()
                if kind=='phase':
                    self.status.set('模擬中：'+payload)
                elif kind=='result':
                    self.last_result = payload
                    self.status.set(f"最近模擬：{payload['manifest']['finished_at']}；"
                                    '共用行情、獨立帳戶，未送券商委託。')
                    self.render()
                elif kind=='error':
                    self.status.set('模擬失敗：'+payload)
                elif kind=='finished':
                    self.running = False
                    self.scheduler.finish()
                    self.run_button.configure(state='normal')
        except queue.Empty:
            pass
        if not self.running and self.scheduler.tick(time.time()):
            self.launch('AUTO')
        self.window.after(250,self.tick)

    def create_strategy_tab(self,s):
        sid = s['id']
        frame = ttk.Frame(self.views)
        self.views.add(frame,text=s['name'][:18])
        heading = ttk.Frame(frame)
        heading.pack(fill='x',padx=10,pady=(8,5))
        capital_text = tk.StringVar()
        ttk.Label(heading,textvariable=capital_text,
                  font=('Microsoft JhengHei UI',10,'bold')).pack(side='left',fill='x',expand=True)
        ttk.Button(heading,text='提高資金上限',command=lambda:self.increase_capital(sid)).pack(side='right',padx=4)
        ttk.Button(heading,text='刪除此策略與紀錄',command=lambda:self.delete_strategy(sid)).pack(side='right',padx=4)
        ttk.Button(heading,text='啟用／停用',command=lambda:self.toggle_strategy(sid)).pack(side='right',padx=4)
        pages = ttk.Notebook(frame)
        pages.pack(fill='both',expand=True,padx=10,pady=(0,8))
        detail_frame = ttk.Frame(pages)
        pages.add(detail_frame,text='策略持倉與決策')
        detail = tk.Text(detail_frame,wrap='word',font=('Microsoft JhengHei UI',10))
        detail_y = ttk.Scrollbar(detail_frame,orient='vertical',command=detail.yview)
        detail.configure(yscrollcommand=detail_y.set)
        detail.pack(side='left',fill='both',expand=True)
        detail_y.pack(side='right',fill='y')
        trade_frame = ttk.Frame(pages)
        pages.add(trade_frame,text='模擬成交紀錄')
        trade_frame.columnconfigure(0,weight=1)
        trade_frame.rowconfigure(0,weight=1)
        trades = ttk.Treeview(trade_frame,show='headings',
                              columns=('時間','標的','方向','股數','價格','手續費','已實現損益','策略版本','理由'))
        for col in trades['columns']:
            trades.heading(col,text=col)
            trades.column(col,width=140 if col not in ('理由','時間') else 460 if col=='理由' else 190)
        trades_y = ttk.Scrollbar(trade_frame,orient='vertical',command=trades.yview)
        trades_x = ttk.Scrollbar(trade_frame,orient='horizontal',command=trades.xview)
        trades.configure(yscrollcommand=trades_y.set,xscrollcommand=trades_x.set)
        trades.grid(row=0,column=0,sticky='nsew')
        trades_y.grid(row=0,column=1,sticky='ns')
        trades_x.grid(row=1,column=0,sticky='ew')
        order_frame = ttk.Frame(pages)
        pages.add(order_frame,text='模擬訂單紀錄')
        order_frame.columnconfigure(0,weight=1)
        order_frame.rowconfigure(0,weight=1)
        orders = ttk.Treeview(order_frame,show='headings',
                               columns=('時間','標的','方向','建議股數','成交股數','狀態','AI 理由'))
        for col in orders['columns']:
            orders.heading(col,text=col)
            orders.column(col,width=430 if col=='AI 理由' else 150)
        orders_y = ttk.Scrollbar(order_frame,orient='vertical',command=orders.yview)
        orders_x = ttk.Scrollbar(order_frame,orient='horizontal',command=orders.xview)
        orders.configure(yscrollcommand=orders_y.set,xscrollcommand=orders_x.set)
        orders.grid(row=0,column=0,sticky='nsew')
        orders_y.grid(row=0,column=1,sticky='ns')
        orders_x.grid(row=1,column=0,sticky='ew')
        self.strategy_widgets[sid] = dict(frame=frame,capital_text=capital_text,
                                          detail=detail,trades=trades,orders=orders)

    def toggle_strategy(self,sid):
        if self.running:
            return
        strategy = next((s for s in self.store.strategies() if s['id']==sid),None)
        if strategy:
            self.store.set_active(sid,not strategy['active'])
            self.render()

    def render_current_strategy(self):
        sid = self.selected_strategy_id()
        if sid in self.strategy_widgets and self.views.select() != str(self.overview):
            self.render_strategy_detail(sid)

    def render(self):
        selected = self.table.selection()
        prior = selected[0] if selected else None
        self.table.delete(*self.table.get_children())
        strategies = self.store.strategies()
        current_ids = {s['id'] for s in strategies}
        for sid,widgets in list(self.strategy_widgets.items()):
            if sid not in current_ids:
                self.views.forget(widgets['frame'])
                widgets['frame'].destroy()
                del self.strategy_widgets[sid]
        for s in strategies:
            if s['id'] not in self.strategy_widgets:
                self.create_strategy_tab(s)
            m = self.store.summary(s['id'])
            pf = '∞' if m['profit_factor_infinite'] else '--' if m['profit_factor'] is None else f"{m['profit_factor']:.2f}"
            self.table.insert('', 'end',iid=s['id'],values=(
                m['name']+' ('+m['kind']+')','啟用' if m['active'] else '停用',m['started_at'],m['version'],
                money(m['capital_limit_twd']),money(m['equity_twd']),
                '--' if m['return_pct'] is None else f"{m['return_pct']:+.2f}%",
                '--' if m['win_rate'] is None else f"{m['win_rate']:.1%}",
                '--' if m['max_drawdown'] is None else f"{m['max_drawdown']:.1%}",pf,m['trade_count']))
            self.strategy_widgets[s['id']]['capital_text'].set(
                f"{m['name']}　{'啟用' if m['active'] else '停用'}　"
                f"初始 {money(m['initial_capital_twd'])}　目前上限 {money(m['capital_limit_twd'])}　"
                f"錢包現金 {money(m['cash_twd'])}")
        if prior and self.table.exists(prior):
            self.table.selection_set(prior)
        elif strategies:
            self.table.selection_set(strategies[0]['id'])
        self.render_current_strategy()
        self.draw_chart()

    def render_strategy_detail(self,sid):
        widgets = self.strategy_widgets[sid]
        detail,trades,orders = widgets['detail'],widgets['trades'],widgets['orders']
        detail.delete('1.0','end')
        trades.delete(*trades.get_children())
        orders.delete(*orders.get_children())
        s = next((s for s in self.store.strategies() if s['id']==sid),None)
        if not s:
            return
        m = self.store.summary(sid)
        detail.insert('end',f"策略：{s['name']}　版本：{s['version']}　前向開始：{s['started_at']}\n")
        detail.insert('end',f"檔案：{s['source_path']}\n模型：{s['model'] or os.getenv('OPENAI_MODEL','gpt-5.6-luna') if s['kind']=='AI' else '固定規則／不呼叫 AI'}\n")
        detail.insert('end',f"現金：{money(m['cash_twd'])}　資產淨值：{money(m['equity_twd'])}\n")
        detail.insert('end',f"初始資金：{money(m['initial_capital_twd'])}　目前資金上限：{money(m['capital_limit_twd'])}\n")
        if m['unpriced']:
            detail.insert('end','以下持倉缺少可用估價，暫不計算資產淨值：'+', '.join(m['unpriced'])+'\n')
        detail.insert('end','\n資金投入紀錄\n')
        for event in self.store.capital_history(sid):
            detail.insert('end',f"{event['at']}　{event['event_type']}　"
                          f"增加 {money(event['delta_cents']/100)}；上限 {money(event['capital_limit_cents']/100)}\n")
        detail.insert('end','\n目前持倉\n')
        for p in m['positions']:
            detail.insert('end',f"{p['symbol']}：{p['qty']} 股；含費成本 NT$ {p['cost_cents']/100:,.2f}\n")
        if not m['positions']:
            detail.insert('end','無\n')
        d = self.store.latest_decision(sid)
        if d:
            detail.insert('end','\n最近決策\n'+d.get('market_view','')+'\n')
            for item in d.get('decisions',[]):
                detail.insert('end',f"{item['symbol']} {item.get('decision','--')}：{item.get('reason','')}\n")
        for t in self.store.recent_trades(sid):
            trades.insert('', 'end',values=(t['at'],t['symbol'],t['side'],t['qty'],
                money(t['price_cents']/100),money(t['fee_cents']/100),
                money(t['realized_cents']/100) if t['side']=='SELL' else '--',
                t['strategy_version'],t['reason']))
        for order in self.store.recent_orders(sid):
            orders.insert('', 'end',values=(order['at'],order['symbol'],order['side'],
                order['proposed_qty'],order['filled_qty'],order['status'],order['reason']))

    def draw_chart(self):
        if self.chart_height == 0:
            return
        canvas = self.chart
        canvas.delete('all')
        width = max(400,canvas.winfo_width())
        height = max(120,canvas.winfo_height())
        series = self.store.equity_series()
        strategies = self.store.strategies()
        if not series:
            canvas.create_text(width/2,height/2,text='開始模擬後顯示前向資產曲線',fill='#666666')
            return
        runs = list(dict.fromkeys(row['run_id'] for row in series))
        values = [r['equity_cents']/100 for r in series if r['equity_cents'] is not None]
        values.extend(s['initial_cents']/100 for s in strategies)
        lo,hi = min(values),max(values)
        if hi==lo:
            lo*=.99; hi*=1.01
        colors = ('#1976D2','#D32F2F','#388E3C','#7B1FA2','#EF6C00','#00838F')
        x0,y0,x1,y1 = 70,25,width-20,height-35
        canvas.create_line(x0,y1,x1,y1,fill='#777777')
        canvas.create_line(x0,y0,x0,y1,fill='#777777')
        canvas.create_text(38,y0,text=f'{hi:,.0f}',fill='#555555')
        canvas.create_text(38,y1,text=f'{lo:,.0f}',fill='#555555')
        for idx,s in enumerate(strategies):
            rows = {r['run_id']:r['equity_cents'] for r in series if r['strategy_id']==s['id']}
            points = []
            for i,run_id in enumerate(runs):
                v = rows.get(run_id)
                if v is None:
                    if len(points)>=4:
                        canvas.create_line(*points,fill=colors[idx%len(colors)],width=2)
                    points = []
                    continue
                x = x0+(x1-x0)*(i/max(1,len(runs)-1))
                y = y1-(v/100-lo)/(hi-lo)*(y1-y0)
                points.extend((x,y))
            if len(points)>=4:
                canvas.create_line(*points,fill=colors[idx%len(colors)],width=2)
            elif len(points)==2:
                canvas.create_oval(points[0]-3,points[1]-3,points[0]+3,points[1]+3,
                                   fill=colors[idx%len(colors)],outline='')
            canvas.create_text(x0+idx*190,height-12,text=s['name']+' '+s['version'][:6],
                               fill=colors[idx%len(colors)],anchor='w')

    def close(self):
        self.scheduler.stop_auto()
        self.closed = True
        self.window.destroy()
