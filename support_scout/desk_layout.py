"""Accessible client-side desk tabs. Changing tabs never calls a model."""
from html import escape

STYLE = '''
[hidden]{display:none!important}
.desk-tabs,.stock-tabs{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0}
.desk-tabs{position:sticky;top:0;z-index:5;background:#f4f6f8;padding:10px 0;border-bottom:1px solid #dbe3ea}
.desk-tabs button,.stock-tabs button{font:inherit;border:1px solid #b5c9d9;background:white;padding:9px 16px;border-radius:6px;cursor:pointer}
[role=tab][aria-selected=true]{background:#245f88;color:white;border-color:#245f88}
.stock-picker{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:12px 0}
.stock-picker select{font:inherit;padding:10px;max-width:100%;min-width:min(100%,340px)}
.scroll{max-height:480px;overflow:auto}.scroll th{position:sticky;top:0;z-index:1}
.stock-card pre{white-space:pre-wrap;overflow-wrap:anywhere}.stock-card{overflow-wrap:anywhere}
.stock-facts{display:flex;gap:12px;flex-wrap:wrap}.stock-facts>div{padding:12px;border:1px solid #dbe3ea;border-radius:6px;min-width:150px}
.stock-facts b{display:block;font-size:19px}.field-row{display:grid;grid-template-columns:minmax(160px,1fr) minmax(0,2fr);gap:12px;padding:8px;border-bottom:1px solid #e4e9ed}
.field-row small{display:block;color:#536579}.field-tree .field-tree{border-left:2px solid #dbe3ea;padding-left:8px}
.stock-card details{margin:10px 0}.stock-card summary{cursor:pointer;font-weight:600}
.report-notes{margin:12px 0}.report-notes>summary{cursor:pointer;color:#536579}
footer{overflow-wrap:anywhere}.stock-picker select{box-sizing:border-box}
@media(max-width:800px){main{padding:14px}.field-row{grid-template-columns:1fr}.desk-tabs button{padding:8px}.stock-facts>div{min-width:120px}}
'''


def stock_tabs(symbol, sections):
    key=escape(symbol,quote=True)
    labels=(('summary','決策與價位'),('technical','走勢與型態'),('research','研究／反證'),('fields','JSON中文說明'))
    buttons=''.join(f'<button type="button" role="tab" id="tab-{key}-{tab}" aria-controls="panel-{key}-{tab}" aria-selected="{str(i==0).lower()}" tabindex="{0 if i==0 else -1}" data-stock-tab="{tab}">{label}</button>' for i,(tab,label) in enumerate(labels))
    panels=''.join(f'<section role="tabpanel" id="panel-{key}-{tab}" aria-labelledby="tab-{key}-{tab}" data-stock-panel="{tab}" {"hidden" if i else ""}>{sections[tab]}</section>' for i,(tab,_) in enumerate(labels))
    return f'<div class="stock-tabs" role="tablist" aria-label="個股資料分類">{buttons}</div>{panels}'


def desk_view(report, overview, cards, operations, audit):
    labels=(('overview','總覽比較'),('stocks','個股研究'),('portfolio','持倉與通知'),('operations','掃描與稽核'))
    buttons=''.join(f'<button type="button" role="tab" id="desk-tab-{key}" aria-controls="desk-{key}" aria-selected="{str(i==0).lower()}" tabindex="{0 if i==0 else -1}" data-desk-tab="{key}">{label}</button>' for i,(key,label) in enumerate(labels))
    options=''.join(f'<option value="{escape(s["symbol"],quote=True)}">{escape(s["symbol"])} · {escape(s.get("name",""))}</option>' for s in report['securities'])
    return f'''<nav class="desk-tabs" role="tablist" aria-label="日股工作台分類">{buttons}</nav>
<section role="tabpanel" aria-labelledby="desk-tab-overview" id="desk-overview" data-desk-panel="overview">{overview}</section>
<section role="tabpanel" aria-labelledby="desk-tab-stocks" id="desk-stocks" data-desk-panel="stocks" hidden>
<h2>個股資訊彙整</h2><p>一次查看一檔；切換分頁只讀取已保存的報告，不重新呼叫API。</p>
<div class="stock-picker"><label for="stock-picker">選擇股票</label><select id="stock-picker">{options}</select><span id="stock-position"></span></div>{''.join(cards)}</section>
<section role="tabpanel" aria-labelledby="desk-tab-portfolio" id="desk-portfolio" data-desk-panel="portfolio" hidden><!--MANUAL_DESK--><p class="static-manual-note">成交回報與通知請開啟本機Dashboard服務。</p></section>
<section role="tabpanel" aria-labelledby="desk-tab-operations" id="desk-operations" data-desk-panel="operations" hidden><!--SCAN_CONTROL-->{operations}{audit}</section>'''


SCRIPT = '''
(()=>{
const tabs=Array.from(document.querySelectorAll('[data-desk-tab]'));
const cards=Array.from(document.querySelectorAll('[data-stock]'));
const picker=document.getElementById('stock-picker');
function desk(name){tabs.forEach(b=>{const active=b.dataset.deskTab===name;b.setAttribute('aria-selected',active);b.tabIndex=active?0:-1;});document.querySelectorAll('[data-desk-panel]').forEach(p=>p.hidden=p.dataset.deskPanel!==name);}
function stock(symbol,part='summary'){const chosen=cards.find(c=>c.dataset.stock===symbol);if(!chosen)return;cards.forEach(c=>c.hidden=c!==chosen);picker.value=symbol;document.getElementById('stock-position').textContent=(cards.indexOf(chosen)+1)+' / '+cards.length+' 檔';chosen.querySelectorAll('[data-stock-tab]').forEach(b=>{const active=b.dataset.stockTab===part;b.setAttribute('aria-selected',active);b.tabIndex=active?0:-1;});chosen.querySelectorAll('[data-stock-panel]').forEach(p=>p.hidden=p.dataset.stockPanel!==part);}
function route(){let hash;try{hash=decodeURIComponent(location.hash.slice(1));}catch(e){return;}
const symbol=hash.startsWith('json-')?hash.slice(5):hash;
if(cards.some(c=>c.dataset.stock===symbol)){desk('stocks');stock(symbol,hash.startsWith('json-')?'fields':'summary');return;}
if(hash==='field-guide'){desk('stocks');stock(picker.value,'fields');return;}
if(['scan-control','desk-operations'].includes(hash)){desk('operations');return;}
if(['manual-desk','desk-portfolio'].includes(hash)){desk('portfolio');return;}
if(hash==='desk-stocks'){desk('stocks');return;}
if(['decision-brief','desk-overview','observation-table',''].includes(hash))desk('overview');
}
tabs.forEach(b=>b.onclick=()=>{desk(b.dataset.deskTab);history.replaceState(null,'','#desk-'+b.dataset.deskTab);});
if(picker)picker.onchange=()=>{stock(picker.value);history.replaceState(null,'','#'+encodeURIComponent(picker.value));};
cards.forEach(c=>c.querySelectorAll('[data-stock-tab]').forEach(b=>b.onclick=()=>{stock(c.dataset.stock,b.dataset.stockTab);history.replaceState(null,'','#'+(b.dataset.stockTab==='fields'?'json-':'')+encodeURIComponent(c.dataset.stock));}));
document.querySelectorAll('[role=tablist]').forEach(list=>list.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;const buttons=Array.from(list.querySelectorAll('[role=tab]'));let i=buttons.indexOf(document.activeElement);if(i<0)return;e.preventDefault();i=e.key==='Home'?0:e.key==='End'?buttons.length-1:(i+(e.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length;buttons[i].focus();buttons[i].click();}));
if(cards.length)stock(cards[0].dataset.stock);window.addEventListener('hashchange',route);route();
const filter=document.getElementById('filter');if(filter)filter.addEventListener('input',()=>{const q=filter.value.toLowerCase();document.querySelectorAll('#decision-brief tbody tr,#observation-table tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q));});
})();
'''
