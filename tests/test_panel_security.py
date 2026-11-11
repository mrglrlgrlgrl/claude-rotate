"""Render the actual panel JavaScript with a tiny DOM sink harness (Node optional)."""
import ast
import json
import shutil
import subprocess
from pathlib import Path


def main():
    node = shutil.which("node")
    if not node:
        print("SKIP panel JavaScript execution: Node.js is not installed")
        return
    tree = ast.parse((Path(__file__).resolve().parent.parent / "rotator.py").read_text())
    panel = next(ast.literal_eval(n.value) for n in tree.body
                 if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PANEL_HTML" for t in n.targets))
    split_end = panel.index("</section>", panel.index('<section class="split">'))
    assert 'id="events"' not in panel[panel.index('<section class="split">'):split_end]
    assert panel[split_end:].replace("</section>\n", "", 1).startswith(
        '<section class="card"><h2>Recent switches</h2><div class="timeline" id="events">')
    script = panel.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const script = JSON.parse(process.argv[1]);
const value = '<mark data-audit="marker">&\'"</mark>';
const elements = {}, urls = [], replaced = [];
const usage = {requests:1,input_tokens:1,output_tokens:1,cache_read:0,cache_write:0,cost_usd:1};
const stats = {devices:{[value]:{last_model:value,last_account:value,key_suffix:value}},alerts:[{level:'warn',text:value}],by_device:{[value]:usage},by_model:{[value]:usage},by_account:{[value]:usage},periods:{since:value,'7d':{device:{[value]:usage},model:{[value]:usage}},'30d':{device:{[value]:usage},model:{[value]:usage}},all:{device:{[value]:usage,[value+'-gone']:usage},model:{[value]:usage}},trend:{[value]:[0,1,2]}}};
const status = {accounts:{[value]:{priority:100,private_to:[value,value+"-2"],reset_5h:1793883600,reset_7d:1793883600,reset_5h_in_min:30,reset_7d_in_h:50}},active_account:value,strategy:value,switch_threshold:.8,switch_threshold_7d:.98,recent_events:[{ts:value,from:value,to:value,reason:value}],weekly:{series:[[0,0,100],[3600,10,100]],projection:[[3600,10,100],[7200,100,100]],rate_per_day:5,eta:7200,back:9000,back_account:value,capacity:200}};
const makeStorage=(init)=>{const m=new Map(Object.entries(init||{}));return {m,getItem:k=>m.has(k)?m.get(k):null,setItem:(k,v)=>{m.set(k,String(v))},removeItem:k=>{m.delete(k)}}};
const makeContext=(search,storage,code)=>{
  const els={}, calls=[], repl=[];
  const ctx=vm.createContext({URL,URLSearchParams,location:{search,href:'http://proxy/rotate/panel'+search},history:{replaceState:(_a,_b,url)=>repl.push(url)},sessionStorage:storage,document:{getElementById:id=>els[id]??=( {innerHTML:'',textContent:''} )},setInterval:()=>{},fetch:async(url,opts)=>{calls.push({url,opts});return {status:code,json:async()=>url.includes('/status')?status:stats}}});
  return {ctx,els,calls,repl};
};
const boot=async(search,storage,code=200)=>{const c=makeContext(search,storage,code);vm.runInContext(script,c.ctx,{timeout:1000});await vm.runInContext('refresh()',c.ctx,{timeout:1000});return c};
const bearers=c=>c.calls.map(({opts})=>opts.headers.Authorization);
const context = vm.createContext({URL,URLSearchParams,location:{search:'?key=fake-viewer-key',href:'http://proxy/rotate/panel?key=fake-viewer-key'},history:{replaceState:(_a,_b,url)=>replaced.push(url)},sessionStorage:makeStorage(),document:{getElementById:id=>elements[id]??=( {innerHTML:'',textContent:''} )},setInterval:()=>{},fetch:async(url,opts)=>{urls.push({url,opts});return {json:async()=>url.includes('/status')?status:stats}}});
(async()=>{
  vm.runInContext(script,context,{timeout:1000});
  await vm.runInContext('refresh()',context,{timeout:1000});
  assert.equal(context.sessionStorage.getItem('claude-rotate-key'),'fake-viewer-key','first load must store the key');
  assert(replaced.every(url=>!url.includes('key=')));
  assert(urls.every(({url,opts})=>!url.includes('key=')&&opts.headers.Authorization==='Bearer fake-viewer-key'));
  assert(elements.weekly.innerHTML.includes('class="hit"'),'shortfall band not drawn');
  assert(elements.devices.innerHTML.includes('removed'),'removed device not listed');
  for(const id of ['pills','accounts','weekly','alerts','devices','models','by-account','events','model-pie','account-pie']){
    assert(!elements[id].innerHTML.includes('<mark'),id+' contains raw markup');
    assert(elements[id].innerHTML.includes('&lt;mark'),id+' lost escaped text');
  }
  assert(elements.accounts.innerHTML.includes('private to &lt;mark') && elements.accounts.innerHTML.includes('&lt;/mark&gt;-2'),'private badge missing or unescaped');
  const slices=id=>(elements[id].innerHTML.match(/class="slice"/g)||[]).length;
  assert(slices('model-pie')===1 && slices('account-pie')===1 && elements['model-pie'].innerHTML.includes('<svg'),'one row must draw one slice');
  assert(elements['model-pie'].innerHTML.includes('100%'),'single slice legend must read 100%');
  const keepStats={by_model:stats.by_model,by_account:stats.by_account,m7:stats.periods['7d'].model};
  const many={}; for(let i=0;i<8;i++) many['m'+i]={...usage,cost_usd:10-i};
  stats.periods['7d'].model=many; stats.by_model={only:usage};
  await vm.runInContext('refresh()',context,{timeout:1000});
  assert.equal(slices('model-pie'),7,'8 models must draw 6 named slices plus other');
  assert(elements['model-pie'].innerHTML.includes('<span>other</span>') && elements['model-pie'].innerHTML.includes('<span>m5</span>') && !elements['model-pie'].innerHTML.includes('<span>m6</span>'),'other legend entry wrong');
  elements.period.onclick({target:{dataset:{p:'24 h'}}}); await vm.runInContext('refresh()',context,{timeout:1000});
  assert.equal(slices('model-pie'),1,'the 24 h period must redraw the model pie');
  elements.period.onclick({target:{dataset:{p:'7 d'}}}); await vm.runInContext('refresh()',context,{timeout:1000});
  assert.equal(slices('model-pie'),7,'the 7 d period must redraw the model pie');
  stats.by_model={}; stats.periods['7d'].model={}; stats.by_account={x:{...usage,cost_usd:0}};
  await vm.runInContext('refresh()',context,{timeout:1000});
  for(const id of ['model-pie','account-pie']) assert(elements[id].innerHTML.includes('no usage') && !elements[id].innerHTML.includes('<svg'),id+' must show no usage');
  stats.by_model=keepStats.by_model; stats.by_account=keepStats.by_account; stats.periods['7d'].model=keepStats.m7;
  await vm.runInContext('refresh()',context,{timeout:1000});
  const titles=elements.accounts.innerHTML.match(/title="[^"]*2026-11-05 13:00 UTC"/g)||[];
  assert.equal(titles.length,2,'5h and 7d reset spans need a UTC title');
  const acct=(extra)=>{status.accounts={flat:Object.assign({priority:100},extra)};return vm.runInContext('refresh()',context,{timeout:1000}).then(()=>elements.accounts.innerHTML)};
  const noBadge=(h,m)=>assert(!h.includes('class="badge">weekly cap') && !h.includes('class="badge">paced'),m+': header badge still drawn');
  const capLine=(p)=>`<span class="cap" style="left:${p}%" title="weekly cap ${p}%: the hatched part is not available"></span>`;
  let html=await acct({weekly_cap:.5,weekly_pacing:false,weekly_allowance:.5,effective_util_7d:.2});
  assert(html.includes('<span class="hatch" style="left:50%"></span>') && html.includes(capLine(50)) && html.includes('aria-label="7d usage 20%, weekly cap 50%"'),'flat cap marks wrong');
  assert(html.includes('<span class="hatch" style="left:50%"></span><i style='),'hatch must precede the fill');
  assert(!html.includes('class="pace') && !html.includes('tagged') && !html.includes('reached') && !html.includes('paced'),'flat cap shows pacing or reached');
  noBadge(html,'flat');
  html=await acct({weekly_cap:.5,weekly_pacing:true,weekly_allowance:.1071,effective_util_7d:.05});
  assert(html.includes('<span class="pace" style="left:10.71%" title="paced allowance 11% now of 50%"><b>11%</b></span>') && html.includes('class="meter tagged"') && html.includes(capLine(50)) && !html.includes('reached'),'paced cap marks wrong');
  assert(html.includes('<span class="hatch" style="left:50%"></span><i style='),'hatch must precede the fill (paced)');
  noBadge(html,'paced');
  html=await acct({weekly_cap:null,weekly_pacing:true,weekly_allowance:.1071,effective_util_7d:.05});
  assert(html.includes('title="paced allowance 11% now of 100%"><b>11%</b></span>') && !html.includes('class="cap"') && !html.includes('class="hatch"'),'paced without a cap wrong');
  noBadge(html,'paced no cap');
  for(const [al,cls] of [[.03,'pace lo'],[.97,'pace hi'],[.5,'pace']]){
    html=await acct({weekly_cap:null,weekly_pacing:true,weekly_allowance:al,effective_util_7d:0});
    assert(html.includes(`class="${cls}" style=`),'tag placement wrong for '+al);
  }
  html=await acct({weekly_cap:.5,weekly_pacing:true,weekly_allowance:.1071,effective_util_7d:.2});
  assert(html.includes('paced allowance reached') && html.includes(', reached"'),'paced reached missing');
  noBadge(html,'paced reached');
  html=await acct({weekly_cap:.5,weekly_pacing:false,weekly_allowance:.5,effective_util_7d:.6});
  assert(html.includes('weekly cap reached'),'flat reached missing');
  html=await acct({weekly_cap:.5,weekly_pacing:false,weekly_allowance:.5,effective_util_7d:1});
  assert(html.includes('limit reached') && !html.includes('weekly cap reached'),'full use must say limit reached');
  html=await acct({weekly_cap:0,weekly_pacing:false,weekly_allowance:0,effective_util_7d:0});
  assert(html.includes('<span class="hatch" style="left:0%"></span>'),'zero cap must hatch everything');
  html=await acct({weekly_cap:null,weekly_pacing:false,weekly_allowance:null,effective_util_7d:.2});
  for(const s of ['class="hatch"','class="cap"','class="pace"','tagged','role="img"','weekly cap','paced'])assert(!html.includes(s),'unexpected '+s+' without cap or pacing');
  noBadge(html,'no limits');
  // reload: a fresh context, the stub carried over, no ?key= in the URL
  let reload=await boot('',context.sessionStorage);
  assert(bearers(reload).length>=2 && bearers(reload).every(b=>b==='Bearer fake-viewer-key'),'reload must send the stored key');
  // a pre-filled stub with no ?key=
  let pre=await boot('',makeStorage({'claude-rotate-key':'stored-key'}));
  assert(bearers(pre).every(b=>b==='Bearer stored-key'),'stored key not sent');
  // the URL wins over the stored key, and replaces it
  const over=makeStorage({'claude-rotate-key':'stored-key'});
  let win=await boot('?key=new-key',over);
  assert(bearers(win).every(b=>b==='Bearer new-key') && over.getItem('claude-rotate-key')==='new-key','URL key must win and be stored');
  // no key anywhere and a 401: one banner, the stored key forgotten, nothing thrown
  const none=makeStorage({'claude-rotate-key':'stale-key'});
  let bad=await boot('',none,401);
  assert(bad.els.alerts.innerHTML.includes('open the panel link with ?key='),'401 banner missing');
  assert.equal(none.getItem('claude-rotate-key'),null,'401 must forget the stored key');
  assert(bad.els.pills===undefined || bad.els.pills.innerHTML==='','401 must render nothing else');
  // blocked storage: the URL key still works
  const blocked={getItem(){throw new Error('blocked')},setItem(){throw new Error('blocked')},removeItem(){throw new Error('blocked')}};
  let blk=await boot('?key=url-key',blocked);
  assert(bearers(blk).every(b=>b==='Bearer url-key'),'blocked storage must fall back to the URL key');
  console.log('panel JavaScript security checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    subprocess.run([node, "-e", harness, json.dumps(script)], check=True)


if __name__ == "__main__":
    main()
