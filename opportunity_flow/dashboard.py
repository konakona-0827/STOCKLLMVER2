"""Reuse the complete STOCKLLM Dashboard; replace only discovery and control semantics."""
from datetime import datetime
import json
from pathlib import Path
import time
import tkinter as tk
from tkinter import ttk,messagebox

from dashboard import Dashboard,update_detail_text
from .common import TAIPEI,policy,read
from .dashboard_service import FlowViewService,STATE_LABELS


class FlowDashboard(Dashboard):
    def __init__(self,root,args,*,start=True,service=None):
        self.args=args;self.flow_ready=False;self.flow_tick=0;self.flow_rows={};self._flow_signature=None
        if service is None:
            from .live import resolve_account_root
            service=FlowViewService(resolve_account_root(args.state_root),args)
        self.controller=service.controller
        super().__init__(root,service.directory,service=service)
        root.title('TW Opportunity Flow｜完整帳戶、研究與持倉管理')
        root.geometry(f'{min(1500,root.winfo_screenwidth()-20)}x{min(920,root.winfo_screenheight()-80)}')
        self.run_button.configure(text='立即更新研究')
        self.action_buttons['開始自動掃描'].configure(text='啟動 FLOW',command=self.start_auto)
        self.action_buttons['停止自動掃描'].configure(text='停止 FLOW',command=self.stop_auto)
        self.action_buttons['設定持倉'].configure(text='持股歸屬／轉交 AI')
        self.action_buttons['開啟最近分析資料夾'].configure(text='開啟 FLOW 紀錄')
        self.strategy_button.configure(text='流程／費用',command=self.show_flow_policy)
        self.mode_selector.configure(state='disabled');self.mode_selector.pack_forget()
        self.notebook.tab(self.top_tree.master,text='型態與候選排序')
        self.notebook.tab(self.union_tree.master,text='研究判斷清單')
        self.top_tree.heading('量化分數',text='型態排序分數');self.top_tree.heading('AI 建議',text='研究狀態')
        self.union_tree.heading('建議',text='研究狀態')
        self.flow_page=ttk.Frame(self.notebook);self.notebook.add(self.flow_page,text='FLOW 全母體與研究卡')
        self.flow_summary=tk.StringVar(value='等待第一輪 FLOW 資料。')
        ttk.Label(self.flow_page,textvariable=self.flow_summary,justify='left',wraplength=1350,padding=10).pack(fill='x')
        filters=ttk.Frame(self.flow_page);filters.pack(fill='x',padx=10,pady=(0,8))
        ttk.Label(filters,text='顯示：').pack(side='left')
        self.flow_filter=tk.StringVar(value='具型態')
        selector=ttk.Combobox(filters,textvariable=self.flow_filter,state='readonly',width=18,
            values=('具型態','全部','等待研究','雙方研究通過','費後／風險未過','資料待補','研究錯誤'))
        selector.pack(side='left',padx=5);selector.bind('<<ComboboxSelected>>',lambda e:self.render_flow_rows())
        ttk.Label(filters,text='代號／名稱：').pack(side='left',padx=(12,0))
        self.flow_search=tk.StringVar();ttk.Entry(filters,textvariable=self.flow_search,width=20).pack(side='left',padx=5)
        self.flow_search.trace_add('write',lambda *a:self.render_flow_rows())
        ttk.Label(filters,text='雙擊研究卡查看兩方理由、固定計畫與未通過原因。').pack(side='left',padx=12)
        grid=ttk.Frame(self.flow_page);grid.pack(fill='both',expand=True,padx=10,pady=(0,10))
        columns=('symbol','name','state','rank','entry','stop','target','qty','reason')
        self.flow_tree=ttk.Treeview(grid,columns=columns,show='headings')
        for key,label,width in zip(columns,('代號','名稱','FLOW 狀態','排序分數','固定入場','失效價','目標價','股數','未過原因／等待條件'),(80,130,150,95,90,90,90,65,450)):
            self.flow_tree.heading(key,text=label);self.flow_tree.column(key,width=width,minwidth=60,stretch=key=='reason')
        y=ttk.Scrollbar(grid,orient='vertical',command=self.flow_tree.yview);x=ttk.Scrollbar(grid,orient='horizontal',command=self.flow_tree.xview)
        self.flow_tree.configure(yscrollcommand=y.set,xscrollcommand=x.set)
        grid.columnconfigure(0,weight=1);grid.rowconfigure(0,weight=1)
        self.flow_tree.grid(row=0,column=0,sticky='nsew');y.grid(row=0,column=1,sticky='ns');x.grid(row=1,column=0,sticky='ew')
        self.flow_tree.bind('<Double-1>',self.show_research_card)
        self.notebook.select(self.flow_page)
        if args.read_only:
            self.paper_capital_button.configure(state='disabled');self.daily_limit_button.configure(state='disabled')
            self.action_buttons['設定持倉'].configure(state='disabled')
        self.flow_ready=True;self.refresh()
        if start:root.after(0,self.start_auto)

    def update_live_strategy_label(self):self.live_strategy_text.set('型態＋固定計畫＋雙方研究')
    def select_decision_mode(self,*unused):pass
    def launch(self,*unused):raise RuntimeError('FLOW_UI_LEGACY_SCAN_DISABLED')
    def launch_health(self):pass  # The FLOW coordinator owns broker refreshes.

    def run_strategy_info(self,run_id,symbol):
        if isinstance(run_id,str) and run_id.startswith('tw-flow-'):
            import re
            if re.fullmatch(r'tw-flow-[0-9a-f]{28}',run_id):
                try:
                    payload=read(Path(self.args.state)/'handoffs'/(run_id+'.json'))
                    row=next(r for r in payload['recommendations'] if r['symbol']==symbol)
                    return 'TW Opportunity Flow',row.get('reason','--')
                except (OSError,ValueError,KeyError,StopIteration):pass
            for attempt in (self.last_health or {}).get('execution_attempts',[]):
                if attempt.get('run_id')==run_id and attempt.get('symbol')==symbol:
                    detail=attempt.get('detail') or {}
                    return 'TW Opportunity Flow',detail.get('analysis_reason') or (detail.get('buy_plan') or {}).get('reason','--')
            return 'TW Opportunity Flow','--'
        return super().run_strategy_info(run_id,symbol)

    def refresh(self,sync_broker=False):
        if self.flow_ready:super().refresh(sync_broker)

    def manual(self):
        if not self.closing:
            self.controller.request_update();self.refresh_text.set('已要求更新研究；既有研究快取與風控仍然適用。')

    def start_auto(self):
        if not self.closing:self.controller.start()

    def stop_auto(self):self.controller.stop()

    def configure_ai_holding(self,symbol,quantity,unit_cost):
        return self.controller.configure_position(symbol,quantity,unit_cost)

    def position_configuration_note(self):
        return '\n增加 AI 股數會記錄為使用者交接；原買進計畫未知。\n券商成本不可核實時，請輸入本次轉入股數的成本。'

    def position_settings(self):
        if self.args.read_only:
            messagebox.showinfo('只讀模式','只讀模式不變更持股歸屬。',parent=self.root);return
        super().position_settings()

    def save_paper_capital(self):
        if not self.args.read_only:super().save_paper_capital()

    def save_daily_buy_limit(self):
        if not self.args.read_only:super().save_daily_buy_limit()

    def tick(self):
        if not self.flow_ready:
            self.root.after(100,self.tick);return
        if self.closing:
            if not self.controller.running and not self.refresh_pending and not self.scheduler.scan_lock.locked():
                if self.simulation_window and not self.simulation_window.closed:self.simulation_window.close()
                self.root.destroy()
            else:self.status_text.set('正在停止 FLOW，等待已開始的查詢與模型結束；已送券商委託仍待核對。');self.root.after(100,self.tick)
            return
        self.next_health_at=float('inf')
        super().tick()  # Retain financial/configuration events, never the legacy scan scheduler.
        self.flow_tick+=1
        if self.flow_tick%20==0 and not self.refresh_pending:self.refresh()
        status={'STOPPED':'已停止','STARTING':'正在啟動','RUNNING':'運行中','STOPPING':'正在停止','ERROR':'啟動／執行錯誤'}.get(self.controller.status,self.controller.status)
        health=self.last_health or {};summary=self.service.flow.get('summary',{})
        mode='只讀演練' if self.args.read_only else '正式執行器'
        error=self.controller.error or self.last_error
        self.status_text.set(f'{datetime.now(TAIPEI):%m/%d %H:%M:%S} 台灣時間｜{mode}｜FLOW {status}｜研究資料 {summary.get("as_of") or "待建立"}\n'
            f'券商核對 {health.get("checked_at") or "尚未核對"}｜新買可用額 {health.get("current_buy_budget_twd","待核對")}｜最近錯誤：{error or "無"}')
        self.system_status_text.set('FLOW 主輪詢約30秒；全母體數值約60秒或輸入更新時重算；券商／持倉約60秒。\n'
            '每批最多3檔、兩個獨立角色；持倉各自重審60～3600秒。\n'
            f'狀態：{status}；錯誤：{error or "無"}；UI讀取錯誤：{"；".join(self.service.read_errors) or "無"}\n'
            f'CLI累計用量：{json.dumps(self.service.usage,ensure_ascii=False)}')
        self.run_button.configure(state='disabled' if self.controller.status=='STOPPING' else 'normal')
        self.action_buttons['開始自動掃描'].configure(state='disabled' if self.controller.running else 'normal')
        self.action_buttons['停止自動掃描'].configure(state='normal' if self.controller.running else 'disabled')

    def render(self):
        super().render()
        if not self.flow_ready:return
        candidates=self.service.flow.get('candidates',[]);states={c['symbol']:STATE_LABELS.get(c['state'],c['state']) for c in candidates}
        for tree,index in ((self.top_tree,6),(self.union_tree,7)):
            for item in tree.get_children():
                values=list(tree.item(item,'values'));symbol=values[1 if tree is self.top_tree else 0]
                values[index]=states.get(symbol,'待研究');values[-1]='--';tree.item(item,values=values)
        summary=self.service.flow.get('summary',{});self.flow_rows={c['symbol']:c for c in candidates}
        counts='｜'.join(f'{STATE_LABELS.get(k,k)} {v}' for k,v in summary.get('states',{}).items())
        self.flow_summary.set(f'全母體 {summary.get("symbols_observed",0)} 檔｜資料時間 {summary.get("as_of") or "待建立"}\n{counts or "等待第一輪資料。"}')
        self.market_text.set('完整母體數值篩選；公開參考價分批輪替，券商零股報價約每分鐘更新持股及研究子集。\n'+counts)
        update_detail_text(self.detail,self.flow_detail())
        signature=(summary.get('as_of'),summary.get('policy_sha256'),tuple(sorted(summary.get('states',{}).items())))
        if signature!=self._flow_signature:self._flow_signature=signature;self.render_flow_rows()

    def render_flow_rows(self):
        if not self.flow_ready:return
        selected=self.flow_tree.selection();selected=selected[0] if selected else None
        self.flow_tree.delete(*self.flow_tree.get_children())
        names=self.service.names
        choice=self.flow_filter.get();search=self.flow_search.get().strip().upper()
        for symbol,c in sorted(self.flow_rows.items()):
            state=c['state'];label=STATE_LABELS.get(state,state);name=names.get(symbol,symbol)
            if search and search not in (symbol+' '+name).upper():continue
            if choice=='具型態' and not c.get('packet'):continue
            if choice=='研究錯誤' and not ('ERROR' in state or 'BACKOFF' in state):continue
            if choice not in ('具型態','全部','研究錯誤') and choice!=label:continue
            packet=(c.get('review') or {}).get('packet') or c.get('packet') or {}
            routes=(packet.get('evidence') or [{},{},{}])[2].get('facts',[]) if len(packet.get('evidence',[]))>2 else []
            route=next((r for r in routes if r.get('route_id')==c.get('selected_route_id')),None) or next((r for r in routes if r.get('eligible')),None) or (routes[0] if routes else {})
            reasons=list(c.get('reasons',[]))
            for role in ('researcher','challenger'):reasons.extend(((c.get('review') or {}).get(role) or {}).get('wait_conditions',[]))
            self.flow_tree.insert('','end',iid=symbol,values=(symbol,name,label,c.get('rank_score','--'),route.get('entry','--'),
                (route.get('stop') or {}).get('price','--'),(route.get('target') or {}).get('price','--'),
                (route.get('economics') or {}).get('quantity','--'),'；'.join(reasons)))
        if selected and self.flow_tree.exists(selected):self.flow_tree.selection_set(selected)

    def flow_detail(self):
        return ('TW Opportunity Flow：研究、模型錯誤與正式委託分開記錄。\n\n'
            '研究員／反證員的個別理由：請在「FLOW 全母體與研究卡」雙擊股票。\n\n'
            '最新流程摘要\n'+json.dumps(self.service.flow.get('summary',{}),ensure_ascii=False,indent=2)+
            '\n\n最新執行與阻擋原因\n'+json.dumps(self.service.execution,ensure_ascii=False,indent=2)+
            '\n\nCLI累計用量\n'+json.dumps(self.service.usage,ensure_ascii=False,indent=2)+
            '\n\n最近模型狀態與資料來源錯誤\n'+json.dumps(getattr(self.service,'activity',{}),ensure_ascii=False,indent=2))

    def show_research_card(self,event=None):
        selected=self.flow_tree.selection()
        if not selected:return
        self.show_text('研究卡｜'+selected[0],json.dumps(self.flow_rows[selected[0]],ensure_ascii=False,indent=2))

    def show_text(self,title,content):
        window=tk.Toplevel(self.root);window.title(title);window.geometry('1050x720')
        text=tk.Text(window,wrap='word',font=('Microsoft JhengHei UI',10),padx=12,pady=10)
        scroll=ttk.Scrollbar(window,orient='vertical',command=text.yview);text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y');text.pack(fill='both',expand=True);text.insert('1.0',content);text.configure(state='disabled')
        return window

    def show_flow_policy(self):
        from .fees import fee_status
        p=policy();fee=fee_status(self.service.account_root)
        self.show_text('FLOW 流程與費用設定',
            f'群益電子盤中零股\n標準佣金率：{float(fee["commission_rate"])*100:g}%\n'
            f'每筆最低佣金：NT$ {fee["minimum_commission_twd"]}\n每筆資金預留：至少 NT$ {fee["cash_reserve_twd"]}，另保留既有價金緩衝。\n'
            f'來源：{fee["source_url"] or "既有明確設定"}\n{fee["limitation"]}\n\n'
            f'新買進條件\n單檔配置最多 {p["allocation_fraction"]*100:g}%；'
            f'模型損失風險最多資金 {p["maximum_position_risk_fraction"]*100:g}%。\n'
            f'費後淨收益至少 NT$ {p["minimum_net_profit_twd"]}、淨報酬至少 {p["minimum_net_return"]*100:g}%、'
            f'報酬風險至少 {p["minimum_reward_risk"]}。\n資金上限另以正式帳戶設定為準，研究不能改寫門檻。\n\n'
            '排程（台灣時間）\n08:30 補齊前日資料，18:00 更新當日日線。\n'
            '公開參考價約30秒分批輪替；完整母體數值約60秒或輸入改變時重算。\n'
            '券商與持倉約60秒；每批最多3檔、兩方獨立研究；各持倉指定60～3600秒重審。\n'
            '立即更新不跳過研究快取、價格證據或風控。')

    def close(self):
        if self.closing:return
        self.closing=True;self.controller.stop()


def launch(args):
    from .live import resolve_account_root
    from .fees import correct_legacy_capital_fee,fee_status
    from state_lock import StateLease
    account_root=resolve_account_root(args.state_root)
    from config import ROOT
    if ROOT.resolve()!=account_root:raise RuntimeError('ACCOUNT_STATE_ALREADY_IMPORTED_FOR_ANOTHER_ROOT')
    with StateLease(account_root,'dashboard'):
        if not args.read_only and fee_status(account_root)['basis']=='LEGACY_RESERVE_USED_AS_COMMISSION':
            correct_legacy_capital_fee(account_root,Path(args.state)/'fee_review')
        import comtypes  # Match the original Dashboard's UI-thread COM initialization.
        root=tk.Tk();FlowDashboard(root,args);root.mainloop();return 0
