const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const money = value => Number.isFinite(value) ? new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2}).format(value) : '—';
const decimal = (value,digits=2) => Number.isFinite(value) ? value.toFixed(digits) : '—';
const percent = value => Number.isFinite(value) ? (value*100).toFixed(1)+'%' : '—';
const names = {momentum_breakout:'Momentum breakout',failed_break_reclaim:'Failed break & reclaim',balance_rotation:'Balance rotation',combined:'All three strategies'};
let result=null, sample=null, csv=null, filename=null, worker=null, timer=null, page=0, selected=null, busy=false, serial=0;
const PAGE_SIZE=20;
const color=value=>value<0?'negative':value>0?'positive':'';
const utc=value=>value ? String(value).replace('T',' ').replace(/\+00:00|Z$/,'').slice(0,19) : '—';
function signed(id,value){$(id).textContent=money(value);$(id).className=color(value);}
function showError(message){$('error-banner').textContent=message;$('error-banner').hidden=!message;}
function setView(view){
  if(!['overview','trades','method'].includes(view))throw new Error('Unknown research view.');
  document.querySelectorAll('[data-panel]').forEach(el=>el.hidden=el.dataset.panel!==view);
  document.querySelectorAll('[data-view]').forEach(el=>{const active=el.dataset.view===view;el.classList.toggle('active',active);if(active)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');});
  return {view};
}
function settings(){const commission=Number($('commission').value),slippage=Number($('slippage').value);if(!Number.isFinite(commission)||commission<0||commission>50||!Number.isInteger(slippage)||slippage<0||slippage>20)throw new Error('Use commission between $0 and $50, and 0–20 whole slippage ticks.');return {commission_per_contract:commission,slippage_ticks:slippage,strategy:$('strategy').value};}
function markChanged(){if(result&&!busy)$('run-state').textContent='Settings changed. Results below still show the last completed run. Select Run experiment to apply.';}
function setBusy(value){
  busy=value;
  for(const id of ['run-button','csv-file','strategy','commission','slippage','sample-button'])$(id).disabled=value;
  $('cancel-button').hidden=!value;
  document.body.classList.toggle('loading',value);
}
function validateResult(value){
  if(!value||!Array.isArray(value.bars)||!Array.isArray(value.trades)||!Array.isArray(value.equity)||!value.metrics||!value.dataset||!value.settings)throw new Error('The engine returned an incomplete experiment. Previous results have been preserved.');
  for(const key of ['net_pnl','gross_pnl','costs','max_drawdown','trade_count'])if(!Number.isFinite(value.metrics[key]))throw new Error('The engine returned an invalid metric: '+key);
  return value;
}
function render(value,description){
  result=validateResult(value);page=0;selected=result.trades[0]?.id??null;
  const m=result.metrics; signed('net-pnl',m.net_pnl);$('max-dd').textContent=money(m.max_drawdown);$('profit-factor').textContent=decimal(m.profit_factor);$('trade-count').textContent=String(m.trade_count);$('win-rate').textContent='Win rate '+percent(m.win_rate);
  signed('gross-pnl',m.gross_pnl);$('total-cost').textContent=money(m.costs);signed('cost-net',m.net_pnl);
  $('trade-tab-count').textContent=String(m.trade_count);
  $('run-fingerprint').textContent='RUN '+String(result.run_id).slice(0,12);
  $('full-run-id').textContent=result.run_id;
  $('full-data-hash').textContent=result.dataset.sha256;
  $('engine-version').textContent=result.engine_version;
  $('settings-readback').textContent=JSON.stringify(result.settings,null,2);
  $('range-label').textContent=utc(result.dataset.start)+' → '+utc(result.dataset.end)+' UTC';
  const synthetic=result.dataset.kind==='synthetic';
  $('data-note').textContent=synthetic?'Synthetic data · These generated prices demonstrate reproducibility, not historical trading performance.':'Imported data · Results depend on this file and the displayed execution assumptions. They do not establish future performance.';
  $('dataset-name').textContent=filename||result.dataset.label||'Reproducibility sample';
  $('dataset-detail').textContent=result.dataset.bar_count.toLocaleString()+' one-minute bars · '+(synthetic?'synthetic':'imported');
  $('run-diagnostics').textContent=(result.settings.strategy==='combined'?'Independent strategy aggregate · ':'Single strategy · ')+(result.diagnostics?.gap_count??0)+' data gaps · '+(result.diagnostics?.unresolved_open_positions??0)+' unfinished positions excluded from results.';
  const splits=result.split_metrics||{};
  $('split-rows').innerHTML=['design','validation','lockbox'].map((key,i)=>{const m=splits[key]||{},dates=splits.session_boundaries?.[key]||[];return '<tr><td>'+['First 60%','Next 20%','Final 20%'][i]+'</td><td>'+escape(dates.length?dates[0]+' → '+dates.at(-1):'No sessions')+'</td><td>'+escape(m.trade_count??0)+'</td><td class="'+color(m.net_pnl)+'">'+money(m.net_pnl)+'</td><td>'+decimal(m.expectancy_r)+'R</td></tr>';}).join('');
  $('cost-assumptions').textContent=money(result.settings.commission_per_contract)+' round-trip commission + '+result.settings.slippage_ticks+' slippage tick(s) per side, per contract.';
  const summaries=result.strategy_summary||[];
  $('strategy-rows').innerHTML=summaries.length?summaries.map(row=>'<tr><td>'+escape(names[row.strategy]||row.strategy)+'</td><td>'+escape(row.trade_count)+'</td><td class="'+color(row.net_pnl)+'">'+money(row.net_pnl)+'</td><td>'+percent(row.win_rate)+'</td></tr>').join(''):'<tr><td colspan="4" class="empty-table">No completed trades in this run.</td></tr>';
  $('method-notes').innerHTML=(result.methodology||[]).map(note=>'<p>'+escape(note)+'</p>').join('');
  $('export-report').disabled=false;$('export-trades').disabled=false;
  $('run-state').textContent=description;
  drawEquity();renderLedger();inspect(selected);renderRecent();
}
function svgFrame(width,height,body,label){return '<svg viewBox="0 0 '+width+' '+height+'" role="img" aria-label="'+escape(label)+'">'+body+'</svg>';}
function drawEquity(){
  const rows=result.equity;if(!rows.length){$('equity-chart').innerHTML='<p class="chart-empty">No balance observations.</p>';return;}
  const W=1000,H=280,left=76,right=20,top=20,bottom=34;
  const values=rows.map(r=>r.balance), low=Math.min(...values),high=Math.max(...values),pad=Math.max(10,(high-low)*.12),lo=low-pad,hi=high+pad;
  const x=i=>left+i/Math.max(1,rows.length-1)*(W-left-right), y=v=>top+(hi-v)/(hi-lo)*(H-top-bottom-65);
  let body='';
  for(let i=0;i<4;i++){const v=lo+(hi-lo)*i/3,yy=y(v);body+='<line x1="'+left+'" x2="'+(W-right)+'" y1="'+yy+'" y2="'+yy+'" stroke="#233144"/><text x="'+(left-9)+'" y="'+(yy+4)+'" text-anchor="end">'+Math.round(v).toLocaleString('en-US')+'</text>';}
  const line=rows.map((r,i)=>(i?'L':'M')+x(i).toFixed(2)+' '+y(r.balance).toFixed(2)).join(' ');
  body+='<path d="'+line+'" fill="none" stroke="#59c9b2" stroke-width="2.2"/>';
  const ddMax=Math.max(1,...rows.map(r=>Math.abs(r.drawdown)));
  const base=H-bottom-45;
  const dd=rows.map((r,i)=>(i?'L':'M')+x(i).toFixed(2)+' '+(base+Math.abs(r.drawdown)/ddMax*36).toFixed(2)).join(' ');
  body+='<line x1="'+left+'" x2="'+(W-right)+'" y1="'+base+'" y2="'+base+'" stroke="#344252"/><path d="'+dd+'" fill="none" stroke="#f09598" stroke-width="1.6"/><text x="'+(left-8)+'" y="'+(base+19)+'" text-anchor="end">DD</text>';
  [0,Math.floor((rows.length-1)/2),rows.length-1].forEach(i=>{body+='<text x="'+x(i)+'" y="'+(H-9)+'" text-anchor="'+(i===0?'start':i===rows.length-1?'end':'middle')+'">'+escape(String(rows[i].timestamp).slice(0,10))+'</text>';});
  $('equity-chart').innerHTML=svgFrame(W,H,body,'Closed-trade balance and drawdown, '+rows.length+' observations.');
}
function inspect(id){
  const trade=result?.trades.find(t=>t.id===id);
  if(!trade){$('trade-evidence').innerHTML='';$('price-chart').innerHTML='<p class="chart-empty">No completed trades to inspect. A no-trade run is a valid result.</p>';$('trade-detail').innerHTML='<p class="muted">The strategies did not complete a trade under these inputs.</p>';$('price-caption').textContent='No trade was manufactured to fill this view.';return;}
  selected=id;
  $('trade-detail').innerHTML='<h3>'+escape(names[trade.strategy]||trade.strategy)+'</h3><span class="trade-id">'+escape(trade.id)+'</span><dl><dt>Direction</dt><dd>'+escape(trade.direction)+'</dd><dt>Contracts</dt><dd>'+escape(trade.quantity)+'</dd><dt>Entry</dt><dd>'+decimal(trade.entry)+'</dd><dt>Exit</dt><dd>'+decimal(trade.exit)+'</dd><dt>Stop</dt><dd>'+decimal(trade.stop)+'</dd><dt>Target</dt><dd>'+decimal(trade.target)+'</dd><dt>Net result</dt><dd class="'+color(trade.pnl)+'">'+money(trade.pnl)+'</dd><dt>Modeled costs</dt><dd>'+money(trade.costs)+'</dd></dl><p class="trade-reason">'+escape(trade.exit_reason)+' · '+escape(trade.bars_held)+' bars held.</p>';
  const from=Math.max(0,trade.entry_index-20),to=Math.min(result.bars.length,trade.exit_index+16);
  let bars=result.bars.slice(from,to);
  const stride=Math.max(1,Math.ceil(bars.length/180));
  if(stride>1){const grouped=[];for(let i=0;i<bars.length;i+=stride){const group=bars.slice(i,i+stride);grouped.push({timestamp:group[0].timestamp,open:group[0].open,close:group.at(-1).close,high:Math.max(...group.map(b=>b.high)),low:Math.min(...group.map(b=>b.low))});}bars=grouped;}
  drawPrice(bars,from,trade,stride);
  $('price-caption').textContent=utc(trade.entry_time)+' → '+utc(trade.exit_time)+' UTC · Markers identify modeled fills. Stop/target lines show the original ticket.'+(stride>1?' Chart groups '+stride+' bars per candle to show the full trade.':'');
  $('trade-evidence').innerHTML='<h3>Decision evidence</h3><ul>'+(trade.evidence?.conditions||[]).map(c=>'<li><span class="'+(c.satisfied?'positive':'negative')+'">'+(c.satisfied?'✓':'×')+'</span> '+escape(c.label)+(c.value!=null?' <small>'+escape(typeof c.value==='number'?decimal(c.value):c.value)+'</small>':'')+'</li>').join('')+'</ul>';
  renderRecent();
}
function drawPrice(bars,offset,trade,stride=1){
  if(!bars.length)return;
  const W=800,H=270,L=12,R=64,T=20,B=30, n=bars.length;
  const max=Math.max(...bars.map(b=>b.high),trade.entry,trade.exit,trade.stop,trade.target),min=Math.min(...bars.map(b=>b.low),trade.entry,trade.exit,trade.stop,trade.target),pad=Math.max(1,(max-min)*.1);
  const lo=min-pad,hi=max+pad,x=i=>L+(i+.5)/n*(W-L-R),y=v=>T+(hi-v)/(hi-lo)*(H-T-B);
  let body='';
  for(let i=0;i<4;i++){const val=lo+(hi-lo)*i/3, yy=y(val);body+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+yy+'" y2="'+yy+'" stroke="#223145"/><text x="'+(W-R+9)+'" y="'+(yy+4)+'">'+val.toFixed(1)+'</text>';}
  const cw=Math.max(1,Math.min(7,(W-L-R)/n*.65));
  bars.forEach((b,i)=>{const c=b.close>=b.open?'#5abeaa':'#db8d98';body+='<line x1="'+x(i)+'" x2="'+x(i)+'" y1="'+y(b.high)+'" y2="'+y(b.low)+'" stroke="'+c+'"/><rect x="'+(x(i)-cw/2)+'" y="'+Math.min(y(b.open),y(b.close))+'" width="'+cw+'" height="'+Math.max(1,Math.abs(y(b.open)-y(b.close)))+'" fill="'+c+'"/>';});
  [[trade.stop,'#d49194','STOP'],[trade.target,'#749ee0','TARGET']].forEach(([p,c,label])=>{body+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+y(p)+'" y2="'+y(p)+'" stroke="'+c+'" opacity=".65" stroke-dasharray="4 5"/><text x="'+(L+4)+'" y="'+(y(p)-5)+'" style="fill:'+c+'">'+label+'</text>';});
  [[trade.entry_index,trade.entry,'ENTRY','#f0d18c'],[trade.exit_index,trade.exit,'EXIT','#e5eaf2']].forEach(([i,p,label,c])=>{if(i<offset||i>=offset+n*stride)return;body+='<circle cx="'+x((i-offset)/stride)+'" cy="'+y(p)+'" r="5" fill="'+c+'" stroke="#0e1823" stroke-width="2"/><text x="'+x((i-offset)/stride)+'" y="'+(y(p)-12)+'" text-anchor="middle" style="fill:'+c+'">'+label+'</text>';});
  body+='<text x="'+L+'" y="'+(H-6)+'">'+escape(utc(bars[0].timestamp))+'</text><text x="'+(W-R)+'" y="'+(H-6)+'" text-anchor="end">'+escape(utc(bars[n-1].timestamp))+'</text>';
  $('price-chart').innerHTML=svgFrame(W,H,body,'OHLC prices and actual simulated entry/exit for '+trade.id);
}
function renderRecent(){
  if(!result)return;
  $('recent-trades').innerHTML=result.trades.slice(0,12).map((t,i)=>'<button data-trade="'+escape(t.id)+'" class="'+(t.id===selected?'selected':'')+'">#'+(i+1)+' · '+escape(t.direction)+' · '+money(t.pnl)+'</button>').join('');
}
function filtered(){const key=$('ledger-filter').value;return result?.trades.filter(t=>key==='all'||t.strategy===key)||[];}
function renderLedger(){
  const trades=filtered(),pages=Math.max(1,Math.ceil(trades.length/PAGE_SIZE));page=Math.min(page,pages-1);
  $('ledger-rows').innerHTML=trades.slice(page*PAGE_SIZE,(page+1)*PAGE_SIZE).map(t=>'<tr tabindex="0" data-trade="'+escape(t.id)+'" aria-label="Inspect '+escape(t.id)+'"><td>'+escape(utc(t.entry_time))+'</td><td>'+escape(names[t.strategy]||t.strategy)+'</td><td><span class="side-label '+(String(t.direction).toLowerCase()==='short'?'short':'')+'">'+escape(t.direction)+'</span></td><td>'+escape(t.quantity)+'</td><td>'+decimal(t.entry)+'</td><td>'+decimal(t.exit)+'</td><td class="'+color(t.pnl)+'">'+money(t.pnl)+'</td><td>'+decimal(t.result_r)+'R</td><td>'+escape(t.exit_reason)+'</td></tr>').join('')||'<tr><td colspan="9" class="empty-table">No completed trades match this filter.</td></tr>';
  $('ledger-page-label').textContent=trades.length+' trades · Page '+(page+1)+' of '+pages;
  $('previous-page').disabled=page===0;$('next-page').disabled=page>=pages-1;
}
function download(name,content,type){
  const url=URL.createObjectURL(new Blob([content],{type}));const link=document.createElement('a');link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function csvCell(value){let text=String(value??'');if(typeof value==='string'&&/^[=+\-@\t\r]/.test(text))text="'"+text;return '"'+text.replace(/"/g,'""')+'"';}
function exportTrades(){
  if(!result)return;
  const keys=['id','strategy','direction','entry_time','exit_time','quantity','entry','exit','stop','target','pnl','gross_pnl','costs','result_r','bars_held','exit_reason'];
  download('seb-trades-'+String(result.run_id).slice(0,10)+'.csv',[keys.join(','),...result.trades.map(t=>keys.map(k=>csvCell(t[k])).join(','))].join('\r\n'),'text/csv');
}
function exportReport(){if(result)download('seb-experiment-'+String(result.run_id).slice(0,10)+'.json',JSON.stringify(result,null,2),'application/json');}
function sampleDownload(){if(!sample){showError('The sample is still loading. Please try again.');return;}const keys=['timestamp','open','high','low','close','volume'];download('seb-synthetic-sample.csv',[keys.join(','),...sample.bars.map(b=>keys.map(k=>b[k]).join(','))].join('\r\n'),'text/csv');}
async function run(){
  if(busy)throw new Error('An experiment is already running.');
  let applied;try{applied=settings();}catch(error){showError(error.message);return;}
  if(!csv&&!sample){showError('Load a valid CSV or wait for the sample dataset.');return;}
  showError('');setBusy(true);$('run-state').textContent='Preparing the Python research engine. First run downloads its runtime.';
  const id=++serial;worker=new Worker(new URL('./research-worker.mjs',import.meta.url),{type:'module'});
  const payload=csv?{csv,settings:applied,label:filename}:{bars:sample.bars,settings:applied,label:'Reproducibility sample',kind:'synthetic'};
  timer=setTimeout(()=>{cancel();showError('This run took too long for this browser. Try fewer bars or use the downloadable local runner.');},180000);
  worker.onmessage=event=>{
    if(event.data.id!==id)return;
    if(event.data.type==='progress'){$('run-state').textContent=event.data.message;return;}
    clearTimeout(timer);worker.terminate();worker=null;setBusy(false);
    if(event.data.type==='error'){showError(event.data.message);$('run-state').textContent='Run failed. Last completed results remain unchanged.';return;}
    try{render(event.data.result,'Completed in this browser. Exports contain this run’s data, assumptions, and results.');}catch(error){showError(error.message);}
  };
  worker.onerror=event=>{clearTimeout(timer);worker?.terminate();worker=null;setBusy(false);showError('The research engine could not start. Check your connection and try again. '+(event.message||''));$('run-state').textContent='Last completed results remain unchanged.';};
  worker.postMessage({id,payload});
}
function cancel(){serial++;clearTimeout(timer);worker?.terminate();worker=null;setBusy(false);$('run-state').textContent='Run cancelled. Last completed results remain unchanged.';}
document.querySelectorAll('[data-view],[data-nav]').forEach(el=>el.addEventListener('click',event=>{event.preventDefault();setView(el.dataset.view||el.dataset.nav);}));
$('run-button').addEventListener('click',run);$('cancel-button').addEventListener('click',cancel);
for(const id of ['strategy','commission','slippage'])$(id).addEventListener('change',markChanged);
$('csv-file').addEventListener('change',async event=>{const file=event.target.files[0];if(!file)return;if(file.size>5_000_000){showError('Choose a CSV smaller than 5 MB, with at most 20,000 bars.');event.target.value='';return;}try{const text=await file.text();csv=text;filename=file.name;$('dataset-name').textContent=file.name;$('dataset-detail').textContent='Imported file · awaiting validation';markChanged();showError('');}catch(error){showError('The file could not be read.');}});
$('sample-button').addEventListener('click',()=>{csv=null;filename=null;$('csv-file').value='';$('dataset-name').textContent='Reproducibility sample';$('dataset-detail').textContent='Synthetic 1-minute bars';markChanged();showError('');});
$('export-trades').addEventListener('click',exportTrades);$('export-report').addEventListener('click',exportReport);
for(const id of ['sample-download','method-sample-download'])$(id).addEventListener('click',sampleDownload);
$('ledger-filter').addEventListener('change',()=>{page=0;renderLedger();});
$('previous-page').addEventListener('click',()=>{page--;renderLedger();});$('next-page').addEventListener('click',()=>{page++;renderLedger();});
document.addEventListener('click',event=>{const el=event.target.closest('[data-trade]');if(el){inspect(el.dataset.trade);setView('overview');}});
document.addEventListener('keydown',event=>{if((event.key==='Enter'||event.key===' ')&&event.target.matches('tr[data-trade]')){event.preventDefault();inspect(event.target.dataset.trade);setView('overview');}});
try{const response=await fetch('./sample-result.json');if(!response.ok)throw new Error('Sample unavailable.');sample=validateResult(await response.json());render(sample,'Verified sample result. Select Run experiment to reproduce it in this browser.');}catch(error){showError('The saved sample could not be loaded. Import a CSV and run the engine to begin.');$('run-state').textContent='Ready for a data file.';}
if(document.modelContext?.registerTool){
  const controller=new AbortController();
  const tools=[
    {name:'run_research_experiment',description:'Run the current sample or locally imported data with the displayed strategy and execution settings. Research simulation only; does not send data or orders.',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:false},execute:async()=>{await run();return {status:busy?'running':'not_started',message:$('run-state').textContent};}},
    {name:'read_research_result',description:'Read the completed research experiment summary and its identity. Does not start a run.',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:true},execute:()=>result?{run_id:result.run_id,dataset:result.dataset,settings:result.settings,metrics:result.metrics}: {status:'no_completed_run'}},
    {name:'inspect_research_trade',description:'Select an existing completed trade and show its price chart and execution details.',inputSchema:{type:'object',properties:{trade_id:{type:'string'}},required:['trade_id'],additionalProperties:false},annotations:{readOnlyHint:false},execute:input=>{if(!input||typeof input.trade_id!=='string'||!result?.trades.some(t=>t.id===input.trade_id))throw new Error('Unknown trade.');inspect(input.trade_id);setView('overview');return result.trades.find(t=>t.id===input.trade_id);}}
  ];
  for(const tool of tools)try{Promise.resolve(document.modelContext.registerTool(tool,{signal:controller.signal})).catch(()=>{});}catch{}
  window.addEventListener('pagehide',()=>controller.abort(),{once:true});
}
