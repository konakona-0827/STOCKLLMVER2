"""Small native live panel; transfer changes ownership, never fabricates a BUY."""
from decimal import Decimal
from pathlib import Path
from queue import Queue,Empty
import threading

from .common import policy,read


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
        self.coordinator=None;self.worker=None;self.stop=threading.Event()
        self.events=Queue();self.rows={};self.ready=False;self.closing=False
        self.trading_error=None
        read_only=getattr(args,'read_only',False)
        self.pause_file=Path(args.state)/('monitoring_paused' if read_only else 'trading_paused')
        enable_label='啟用唯讀監看' if read_only else '啟用自動交易'
        self.stopped_status=f'已停止，可按「{enable_label}」。'
        root.title('TW Opportunity Flow — 自動交易與持股交接')
        root.geometry('1060x640')
        self.status=tk.StringVar(value=self.stopped_status)
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
        self.enable_button=ttk.Button(controls,text=enable_label,command=self.enable_trading)
        self.enable_button.pack(side='left',padx=5)
        self.stop_button=ttk.Button(controls,text='停止唯讀監看' if read_only else '停止自動交易',
                                   command=self.stop_trading,state='disabled')
        self.stop_button.pack(side='left',padx=5)
        ttk.Label(controls,text='停止後不再送新委託；券商未結委託仍須依對帳與撤單狀態處理。').pack(side='left',padx=12)
        self.log=tk.Text(root,height=7,wrap='word');self.log.pack(fill='x',padx=12,pady=8)
        root.protocol('WM_DELETE_WINDOW',self.close)
        root.after(500,self.poll)
        if start and not self.pause_file.exists():
            self.enable_trading()

    def enable_trading(self):
        if self.closing or self.worker is not None:
            return
        try:
            self.pause_file.unlink(missing_ok=True)
        except OSError as exc:
            self.status.set(f'無法解除停止設定，可再試一次。{exc}')
            return
        self.stop=threading.Event()
        self.trading_error=None
        self.worker=threading.Thread(target=self.run_trading,args=(self.stop,),daemon=True)
        self.enable_button.state(['disabled'])
        self.stop_button.state(['!disabled'])
        self.status.set('正在連線並重新核對券商帳戶…')
        self.worker.start()

    def run_trading(self,stop):
        coordinator=None
        try:
            from .live import LiveCoordinator,resolve_account_root
            from .__main__ import main
            p=policy()
            if self.args.model:p['model']=self.args.model
            if stop.is_set():return
            coordinator=LiveCoordinator(resolve_account_root(self.args.state_root),self.args.state,p,stop,read_only=self.args.read_only)
            self.coordinator=coordinator
            if stop.is_set():return
            coordinator.refresh_account()
            self.events.put(('ready',None))
            args=['watch','--dataset',self.args.dataset,'--state',self.args.state,'--live','--llm','--public-quotes','--refresh-daily']
            if self.args.model:args+=['--model',self.args.model]
            if self.args.read_only:args+=['--read-only']
            main(args,coordinator=coordinator,external_stop=stop)
        except Exception as exc:
            self.events.put(('trading_error',type(exc).__name__+': '+str(exc)))
        finally:
            stop.set()
            if coordinator is not None and not coordinator.closed:
                try:coordinator.close()
                except Exception as exc:self.events.put(('trading_error',str(exc)))

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
            if not self.ready or self.stop.is_set():raise ValueError('交易程式尚未就緒或已停止')
        except (ValueError,ArithmeticError) as exc:
            messagebox.showerror('無法轉交',str(exc),parent=self.root);return
        self.run_account_action('handoff',self.coordinator.transfer,*values)
        self.status.set('正在重新核對券商與交接股數…')

    def refresh(self):
        if self.ready and not self.stop.is_set():
            self.run_account_action(None,self.coordinator.refresh_account)

    def run_account_action(self,kind,action,*args):
        coordinator,stop=self.coordinator,self.stop
        def run():
            try:
                with coordinator.lock:
                    if stop.is_set():return
                    result=action(*args)
                if kind:self.events.put((kind,result))
            except Exception as exc:self.events.put(('error',str(exc)))
        threading.Thread(target=run,daemon=True).start()

    def stop_trading(self):
        self.stop.set()
        self.stop_button.state(['disabled'])
        self.status.set('正在完成目前的紀錄與關閉券商連線…')
        try:
            self.pause_file.parent.mkdir(parents=True,exist_ok=True)
            self.pause_file.touch()
        except OSError as exc:
            self.events.put(('trading_error',f'本次已停止，但無法儲存停止設定。{exc}'))

    def poll(self):
        try:
            while True:
                kind,value=self.events.get_nowait()
                if kind=='ready':
                    self.ready=not self.stop.is_set()
                    continue
                if kind=='trading_error':
                    self.trading_error=value
                self.log.insert('end',f'{kind}: {value}\n');self.log.see('end')
        except Empty:pass
        if self.worker is not None and not self.worker.is_alive():
            self.worker=None
            self.coordinator=None
            self.ready=False
            if self.closing:
                self.root.destroy()
                return
            self.enable_button.state(['!disabled'])
            self.stop_button.state(['disabled'])
            self.status.set(self.trading_error or self.stopped_status)
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
            if self.ready and not self.stop.is_set():
                mode='只讀演練，不送單' if status['read_only'] else '正式執行器已接上'
                self.status.set(f"{mode}｜BUY 可用額 {status.get('buy_budget_twd')}｜券商核對 {status.get('checked_at')}｜{'; '.join(status.get('errors',[]))}")
        except (OSError,ValueError,KeyError):pass
        if self.root.winfo_exists():self.root.after(1000,self.poll)

    def close(self):
        self.closing=True
        if self.worker is None:
            self.root.destroy()
            return
        self.stop.set()
        self.stop_button.state(['disabled'])
        self.status.set('正在關閉券商連線…')


def launch(args):
    import tkinter as tk
    root=tk.Tk();ControlPanel(root,args);root.mainloop();return 0
