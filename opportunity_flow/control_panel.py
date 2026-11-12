"""Small native live panel; transfer changes ownership, never fabricates a BUY."""
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from queue import Queue,Empty
import threading

from .common import ROOT,policy,read


def transfer_values(symbol,quantity,cost,broker_cost=None):
    if not symbol:raise ValueError('請先選取券商持倉')
    qty=int(quantity)
    if qty<=0:raise ValueError('轉交股數須為正整數')
    unit=Decimal(cost.strip()) if cost.strip() else Decimal(str(broker_cost)) if broker_cost is not None else None
    if unit is None:raise ValueError('未取得可核實券商成本，請輸入轉交股數的每股成本價')
    if not unit.is_finite() or unit<=0:raise ValueError('成本價須為有限正數')
    return symbol,qty,str(unit)


class ControlPanel:
    def __init__(self,root,args,*,start=True):
        import tkinter as tk
        from tkinter import ttk
        self.root,self.args=root,args
        self.coordinator=None;self.stop=threading.Event();self.events=Queue();self.rows={}
        root.title('TW Opportunity Flow — 自動交易與持股交接')
        root.geometry('1060x640')
        self.status=tk.StringVar(value='正在啟動既有券商執行器…' if start else '介面測試')
        ttk.Label(root,textvariable=self.status,wraplength=1020).pack(fill='x',padx=12,pady=10)
        columns=('symbol','broker','manual','ai','broker_cost','handoff')
        self.table=ttk.Treeview(root,columns=columns,show='headings',height=12)
        for key,title in zip(columns,('股票代號','券商總股數','人工股數','AI 股數','券商成本參考','使用者轉交')):
            self.table.heading(key,text=title);self.table.column(key,width=150)
        self.table.pack(fill='both',expand=True,padx=12);self.table.bind('<<TreeviewSelect>>',self.select)
        form=ttk.LabelFrame(root,text='人工持股轉交 AI 管理（更新歸屬，不產生買單）')
        form.pack(fill='x',padx=12,pady=10)
        self.symbol=tk.StringVar();self.quantity=tk.StringVar();self.cost=tk.StringVar()
        for i,(label,var) in enumerate((('股票代號',self.symbol),('本次轉交股數',self.quantity),('轉交股數的每股成本價',self.cost))):
            ttk.Label(form,text=label).grid(row=0,column=i*2,padx=7,pady=10)
            entry=ttk.Entry(form,textvariable=var,width=15,state='readonly' if i==0 else 'normal')
            entry.grid(row=0,column=i*2+1,padx=7,pady=10)
            if i==2:self.cost_entry=entry
        ttk.Button(form,text='轉交並啟用 AI 管理',command=self.transfer).grid(row=1,column=0,columnspan=2,padx=8,pady=8)
        ttk.Label(form,text='券商成本無法核實時請填寫；原買進計畫會標示未知，不會補造。',wraplength=740).grid(row=1,column=2,columnspan=4,sticky='w')
        controls=ttk.Frame(root);controls.pack(fill='x',padx=12,pady=5)
        ttk.Button(controls,text='重新核對券商持倉',command=self.refresh).pack(side='left',padx=5)
        ttk.Button(controls,text='停止自動交易',command=self.stop_trading).pack(side='left',padx=5)
        ttk.Label(controls,text='停止後不再送新委託；券商未結委託仍須依對帳與撤單狀態處理。').pack(side='left',padx=12)
        self.log=tk.Text(root,height=7,wrap='word');self.log.pack(fill='x',padx=12,pady=8)
        root.protocol('WM_DELETE_WINDOW',self.close)
        root.after(500,self.poll)
        if start:threading.Thread(target=self.start,daemon=True).start()

    def start(self):
        try:
            from .live import LiveCoordinator,resolve_account_root
            from .__main__ import main
            p=policy()
            if self.args.model:p['model']=self.args.model
            self.coordinator=LiveCoordinator(resolve_account_root(self.args.state_root),self.args.state,p,self.stop,read_only=self.args.read_only)
            self.coordinator.refresh_account()
            args=['watch','--dataset',self.args.dataset,'--state',self.args.state,'--live','--llm','--public-quotes','--refresh-daily']
            if self.args.model:args+=['--model',self.args.model]
            if self.args.read_only:args+=['--read-only']
            main(args,coordinator=self.coordinator,external_stop=self.stop)
        except Exception as exc:
            self.events.put(('error',type(exc).__name__+': '+str(exc)))
            self.stop.set()
            if self.coordinator is not None:self.coordinator.close()

    def select(self,_=None):
        chosen=self.table.selection()
        if not chosen:return
        row=self.rows[chosen[0]];self.symbol.set(row['symbol']);self.quantity.set(str(row['manual_qty']))
        self.cost.set(str(row['broker_cost']) if row.get('broker_cost') is not None and not row.get('ai_qty') else '')

    def transfer(self):
        from tkinter import messagebox
        try:
            row=next((r for r in self.rows.values() if r['symbol']==self.symbol.get()),{})
            values=transfer_values(self.symbol.get(),self.quantity.get(),self.cost.get(),row.get('broker_cost') if not row.get('ai_qty') else None)
            if values[1]>int(row.get('manual_qty') or 0):raise ValueError('轉交股數超過目前人工持股')
            if self.coordinator is None or self.stop.is_set():raise ValueError('交易程式尚未就緒或已停止')
        except (ValueError,ArithmeticError) as exc:
            messagebox.showerror('無法轉交',str(exc),parent=self.root);return
        def run():
            try:self.events.put(('handoff',self.coordinator.transfer(*values)))
            except Exception as exc:self.events.put(('error',str(exc)))
        threading.Thread(target=run,daemon=True).start()
        self.status.set('正在重新核對券商與交接股數…')

    def refresh(self):
        if self.coordinator is None:return
        def run():
            try:self.coordinator.refresh_account()
            except Exception as exc:self.events.put(('error',str(exc)))
        threading.Thread(target=run,daemon=True).start()

    def stop_trading(self):
        self.stop.set();self.status.set('已停止新交易；正在完成目前的紀錄與關閉連線。')

    def poll(self):
        try:
            while True:
                kind,value=self.events.get_nowait();self.log.insert('end',f'{kind}: {value}\n');self.log.see('end')
        except Empty:pass
        try:
            status=read(Path(self.args.state)/'live_status.json')
            selected=self.table.selection();selected=selected[0] if selected else None
            self.rows={r['symbol']:r for r in status['positions']}
            for key in self.table.get_children():
                if key not in self.rows:self.table.delete(key)
            for key,r in self.rows.items():
                values=(r['symbol'],r['broker_qty'],r['manual_qty'],r['ai_qty'],r.get('broker_cost') or '未取得，請輸入',
                        '使用者交接／原計畫未知' if r.get('user_handoff') else '')
                if self.table.exists(key):self.table.item(key,values=values)
                else:self.table.insert('', 'end',iid=key,values=values)
            if not self.stop.is_set():
                mode='只讀演練，不送單' if status['read_only'] else '正式執行器已接上'
                self.status.set(f"{mode}｜BUY 可用額 {status.get('buy_budget_twd')}｜券商核對 {status.get('checked_at')}｜{'; '.join(status.get('errors',[]))}")
        except (OSError,ValueError,KeyError):pass
        if self.root.winfo_exists():self.root.after(1000,self.poll)

    def close(self):
        self.stop_trading()
        if self.coordinator is None or self.coordinator.closed:self.root.destroy()
        else:self.root.after(300,self.wait_close)

    def wait_close(self):
        if self.coordinator.closed:self.root.destroy()
        else:self.root.after(300,self.wait_close)


def launch(args):
    import tkinter as tk
    root=tk.Tk();ControlPanel(root,args);root.mainloop();return 0
