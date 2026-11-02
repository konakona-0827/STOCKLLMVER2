"""Exercise client JSON and feedback in a fake DOM, not a real browser or account."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from support_scout.manual_ui import PANEL


@unittest.skipUnless(shutil.which('node'),'Node is optional for frontend contract tests')
class ManualClientTests(unittest.TestCase):
    def test_client_submission_feedback_dates_retries_and_offline_state(self):
        script=re.search(r'<script>(.*)</script>',PANEL,re.S).group(1)
        script=script.replace('__TOKEN__','"fixture"').replace('__ASOF__','null').replace('__UIVERSION__','7')
        harness=r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
class El{
 constructor(){this.children=[];this.dataset={};this.handlers={};this.value='';this.checked=false;this.disabled=false;this.textContent='';}
 append(...x){this.children.push(...x);}replaceChildren(...x){this.children=[...x];}querySelector(){return null;}
 addEventListener(k,v){this.handlers[k]=v;}scrollIntoView(){}reportValidity(){return true;}
}
function environment(){
 const els=new Map(),get=id=>{if(!els.has(id))els.set(id,new El());return els.get(id);};
 const form=get('fill-form'),fields=[];
 for(const name of ['symbol','side','amount_jpy','price','quantity','fees','filled_at','risk_lower','confirmed','amount_reported']){
  const e=new El();e.name=name;e.type=['confirmed','amount_reported'].includes(name)?'checkbox':'text';fields.push(e);fields[name]=e;
 }
 fields.namedItem=k=>fields[k];form.elements=fields;
 Object.assign(fields.symbol,{value:'9001'});Object.assign(fields.side,{value:'BUY'});Object.assign(fields.quantity,{value:'3'});
 fields.price.value='6000';fields.fees.value='0';fields.filled_at.value='2026-11-02T14:10:05';fields.confirmed.checked=true;
 const d={funding:{funding_as_of:'2026-11-02T16:00:00+09:00',research_as_of:null,capital_jpy:200000,available_cash_estimate_jpy:182000,reserved_cash_jpy:0,confirmed_cost_jpy:18000,plans:[]},portfolio:{positions:{'9001.T':{symbol:'9001.T',quantity:3,average_cost:6000,total_cost:18000,opened_at:'2026-11-02T14:10:05+09:00'}}},drafts:[],proposals:[],fill_history:[]};
 const requests=[],store=new Map();const ctx={Node:El,Date,Math,Number,JSON,console,CSS:{escape:x=>x},crypto:{randomUUID:()=> 'fixture-fill-0001'},
  localStorage:{setItem:(k,v)=>store.set(k,v),getItem:k=>store.get(k)||null,removeItem:k=>store.delete(k)},
  document:{getElementById:get,createElement:()=>new El(),createTextNode:x=>String(x),querySelector:()=>null,addEventListener:()=>{}},
  FormData:class{constructor(f){this.f=f;}get(k){return this.f.elements[k].value;}has(k){return this.f.elements[k].checked;}},setInterval:()=>{},
  fetch:async(path,opt)=>{if(opt){requests.push(JSON.parse(opt.body));return{ok:true,json:async()=>({recorded:true,manual_desk:d,message:'成交已成功儲存，持倉與資金已更新',confirmation_id:'confirm-fixture-0001'})};}
   return{ok:true,json:async()=>({manual_desk:d,email:{status:'READY',counts:{}},runtime:{analyze_text:'待命'},analysis:{status:'IDLE'},report_as_of:null})};}
 };
 vm.createContext(ctx);vm.runInContext(SCRIPT,ctx);return{ctx,get,form,fields,requests};
}
(async()=>{
 let e=environment();await e.form.handlers.submit({preventDefault(){}});
 assert.equal(e.requests[0].filled_at,'2026-11-02T14:10:05+09:00');assert.equal(e.requests[0].confirmed,true);
 assert.ok(e.get('fill-status').textContent.includes('成功儲存'));assert.equal(e.get('positions').children.length,1);
 e.fields.confirmed.checked=true;await e.form.handlers.submit({preventDefault(){}});assert.equal(e.requests[0].id,e.requests[1].id);
 e=environment();e.fields.filled_at.value='2026-11-02T14:10';await e.form.handlers.submit({preventDefault(){}});
 assert.equal(e.requests[0].filled_at,'2026-11-02T14:10:00+09:00');
 e=environment();e.fields.amount_jpy.value='20000';await e.form.handlers.submit({preventDefault(){}});
 assert.equal(e.requests.length,0);assert.ok(e.get('fill-status').textContent.includes('不一致'));
 e=environment();e.ctx.fetch=async()=>({ok:false,json:async()=>({error:'BAD_QUANTITY',message:'請核對實際股數'})});
 await e.form.handlers.submit({preventDefault(){}});assert.equal(e.get('fill-status').textContent,'請核對實際股數');
 assert.equal(e.get('fill-save').disabled,false);assert.equal(e.fields.price.value,'6000');
 e=environment();e.ctx.fetch=async()=>{throw new Error('offline');};await vm.runInContext('refreshDesk()',e.ctx);
 assert.ok(e.get('funding-asof').textContent.includes('目前無法更新'));
 await e.form.handlers.submit({preventDefault(){}});assert.ok(e.get('fill-status').textContent.includes('連線中斷'));
 assert.equal(e.fields.price.value,'6000');assert.equal(e.get('fill-save').disabled,false);
 e=environment();vm.runInContext("draftIntoForm({id:'draft-one',fill_id:'fill-one',symbol:'9001.T',amount_jpy:null,actual_price:6000,reported_executed:false},{action:'EXIT_REVIEW',entry_reference:5900})",e.ctx);
 assert.equal(e.fields.side.value,'SELL');assert.equal(e.fields.price.value,6000);assert.equal(e.fields.filled_at.value,'');
 assert.equal(e.fields.confirmed.checked,false);
 e=environment();e.ctx.document.createElement=()=>{throw new Error('render failure');};
 await e.form.handlers.submit({preventDefault(){}});assert.ok(e.get('fill-status').textContent.includes('紀錄已保存'));
 assert.equal(e.get('fill-status').dataset.kind,'success');
 console.log('client workflow contracts passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''.replace('SCRIPT',json.dumps(script,ensure_ascii=False))
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'manual-client.cjs';path.write_text(harness,encoding='utf-8')
            result=subprocess.run([shutil.which('node'),str(path)],capture_output=True,text=True,encoding='utf-8',timeout=15)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
