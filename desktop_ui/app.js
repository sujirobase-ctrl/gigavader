'use strict';
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const bytes=n=>n==null?'tamanho desconhecido':n<1048576?(n/1024).toFixed(0)+' KB':(n/1048576).toFixed(1)+' MB';
const status={pending:'Pendente',down:'Baixando',ok:'Concluído',skip:'Ignorado',fail:'Falhou'};
const typeName={img:'Imagem',vid:'Vídeo',audio:'Áudio',arq:'Arquivo'};
const S={items:[],queue:[],remaining:0,sel:new Set(),generation:-1,pg:0,fp:0,kind:'*',tab:'novo',pick:false,state:'idle',logs:[],history:[],recover:[],frames:[],frame:0,video:null,videoToken:0,catalogBusy:false,cookieBusy:false,thumbs:new Map(),thumbPending:new Map(),galleryToken:0};
let api, timer, videoTimer;
function toast(message){$('#toast').textContent=message;$('#toast').style.display='block';clearTimeout(timer);timer=setTimeout(()=>$('#toast').style.display='none',5000)}
async function call(method,...args){try{return await api[method](...args)}catch(e){toast(e.message||String(e));return null}}
function tab(name){S.tab=name;['novo','down','hist','diag'].forEach(k=>$('#t-'+k).hidden=k!==name);document.querySelectorAll('.nv').forEach(b=>b.classList.toggle('on',b.dataset.tab===name));$('#v-home').hidden=S.pick;$('#v-pick').hidden=!S.pick;if(name==='down')renderQueue();if(name==='hist')renderHistory()}
$('#nav').innerHTML=[['novo','↓','Novo download'],['down','≡','Downloads'],['hist','◷','Histórico'],['diag','⌁','Diagnóstico']].map(([k,i,n])=>`<button class="nv" data-tab="${k}"><span>${i}</span>${n}</button>`).join('');
$('#nav').onclick=e=>{const b=e.target.closest('[data-tab]');if(b)tab(b.dataset.tab)};
const config=()=>({url:$('#url').value,cookies:$('#ckn').value,destination:$('#dst').value,all_pages:$('#sa').checked,separate_posts:$('#sp').checked,flat:$('#pu').checked,dry_run:$('#so').checked,workers:+$('#c1').value,connections:+$('#c2').value,retries:+$('#c4').value,max_pages:+$('#pl').value,delay:+$('#c3').value,rate:+$('#c5').value,include:$('#inc').value,exclude:$('#exc').value,recovery:$('#rs').checked,theme:$('#th').checked?'light':'dark'});
function fill(c){for(const [id,key] of Object.entries({url:'url',ckn:'cookies',dst:'destination',c1:'workers',c2:'connections',c3:'delay',c4:'retries',c5:'rate',pl:'max_pages',inc:'include',exc:'exclude'})){if(c[key]!=null)$('#'+id).value=c[key]}for(const [id,key] of Object.entries({sa:'all_pages',sp:'separate_posts',pu:'flat',so:'dry_run',rs:'recovery'})){if(c[key]!=null)$('#'+id).checked=c[key]}$('#pl').disabled=$('#sa').checked;document.documentElement.dataset.theme=c.theme||'dark';$('#th').checked=c.theme==='light'}
$('#th').onchange=()=>document.documentElement.dataset.theme=$('#th').checked?'light':'dark';
$('#adv').onclick=e=>{const b=e.target.closest('button');if(!b)return;$('#pp').hidden=b.dataset.a==='1';$('#pa').hidden=b.dataset.a!=='1';document.querySelectorAll('#adv button').forEach(x=>x.classList.toggle('on',x===b))};
$('#sa').onchange=()=>$('#pl').disabled=$('#sa').checked;
$('#sp').onchange=()=>{if($('#sp').checked)$('#pu').checked=false};$('#pu').onchange=()=>{if($('#pu').checked)$('#sp').checked=false};
function cookieButtons(){
 const disabled=busy()||S.cookieBusy;
 $('#ckb').disabled=S.cookieBusy;
 $('#ck-login').disabled=disabled;
 $('#ck-verify').disabled=disabled||!$('#ckn').value;
}
function showCookieStatus(info){
 if(!info)return;
 if(info.path)$('#ckn').value=info.path;
 const date=value=>new Date(value).toLocaleString('pt-BR');
 const details=[];
 if(info.expires){
  if(!('login' in info.expires))details.push('O site não informou uma data de expiração para o login.');
  for(const [key,label] of [['login','Cookie de login'],['protection','Proteção do site']]){
   if(!(key in info.expires))continue;
   const expiry=info.expires[key];
   details.push(expiry
    ? `${label} ${new Date(expiry)<new Date()?'expirou em':'expira em'} ${date(expiry)}.`
    : `${label} sem data fixa de expiração.`);
  }
 }
 const message={
  missing:'Nenhuma sessão selecionada.',
  unchecked:'Sessão carregada. Use Verificar sessão para consultar o site.',
  unreadable:'Sessão indisponível neste perfil. Capture ou importe novamente.',
  valid:'O site confirmou o login',
  signed_out:'O site respondeu como visitante',
  unknown:'Verificação inconclusiva. O app não conseguiu confirmar o login',
 }[info.state];
 if(message)details.push(info.checked_at?`${message} em ${date(info.checked_at)}.`:message);
 $('#ckst').textContent=details.join(' ');
 cookieButtons();
}
async function manageCookies(action){
 if(S.cookieBusy)return;
 S.cookieBusy=true;cookieButtons();
 try{showCookieStatus(await action())}
 finally{S.cookieBusy=false;cookieButtons()}
}
$('#ckb').onclick=()=>manageCookies(async()=>{
 const path=await call('choose','cookies');
 if(path&&await call('update_cookies',path))return call('cookie_status');
});
$('#ck-login').onclick=()=>manageCookies(()=>call('capture_cookies',$('#url').value));
$('#ck-verify').onclick=()=>manageCookies(()=>call('cookie_status',true));
$('#dsb').onclick=async()=>{const path=await call('choose','folder');if(path)$('#dst').value=path};
async function analyze(all){if(await call('analyze',config(),all)){S.afterAnalyze=all?'down':'gallery';S.state='scanning';setBusy();toast('Capturando páginas e resolvendo links…')}}
$('#vz').onclick=()=>analyze(false);$('#an').onclick=()=>analyze(true);$('#up').onclick=()=>analyze(false);
$('#back').onclick=()=>{S.pick=false;tab('novo')};$('#bk').onclick=()=>{S.pick=true;tab('novo');renderGallery()};
function filtered(){const q=$('#sq').value.toLowerCase(),host=$('#fh').value;return S.items.filter(f=>(S.kind==='*'||f.type===S.kind)&&(host==='*'||f.host===host)&&(!q||`${f.name} ${f.post} ${f.host}`.toLowerCase().includes(q)))}
function selection(){ $('#cnt').textContent=S.sel.size+' selecionados';$('#enq').textContent=`Baixar selecionados (${S.sel.size})`;$('#enq').disabled=!S.sel.size||busy();$('#enqa').textContent=`Baixar todos (${S.items.length})`;$('#enqa').disabled=busy()}
function renderGallery(){const list=filtered(),pages=Math.max(1,Math.ceil(list.length/12));S.pg=Math.max(0,Math.min(S.pg,pages-1));const shown=list.slice(S.pg*12,S.pg*12+12),token=++S.galleryToken;
$('#pt1').textContent=S.title;$('#pt2').textContent=`${S.items.length} arquivos · ${S.posts} posts`;$('#res').innerHTML=`<details><summary>Detalhes da análise</summary><p class="mu sm">A seleção é preservada entre páginas. Falhas de prévia não impedem o download.</p><div>${[...new Set(S.items.map(f=>f.post))].map(p=>`<button class="pc" data-post="${esc(p)}">${esc(p)}</button>`).join('')}</div></details>`;
$('#gal').innerHTML=shown.map(f=>`<div><div tabindex="0" role="checkbox" aria-checked="${S.sel.has(f.id)}" aria-label="Selecionar ${esc(f.name)}" class="th ${S.sel.has(f.id)?'s':''}" data-id="${f.id}" style="background:linear-gradient(135deg,#494391,#245d72)"><span class="ck">${S.sel.has(f.id)?'✓':''}</span>${f.type==='img'?`<button class="ex" data-image="${f.id}" aria-label="Ampliar imagem" title="Ampliar no BUIDL">⤢</button>`:f.type==='vid'?`<button class="ex" data-video="${f.id}" aria-label="Ampliar vídeo" title="Ver prévia no BUIDL">⤢</button>`:''}<span class="placeholder">${['img','vid'].includes(f.type)?'Carregando…':typeName[f.type]}</span>${f.type==='vid'?`<b role="button" tabindex="0" data-video="${f.id}">▶ Ver prévia</b>`:''}</div><div class="nm" title="${esc(f.name)}">${esc(f.name)}</div><div class="mu sm">${esc(f.post)} · ${esc(f.host)}</div></div>`).join('')||'<div class="emp">Nenhum resultado.</div>';
$('#pgi').textContent=`Página ${S.pg+1} de ${pages} · ${list.length} resultados`;$('#pv1').disabled=S.pg===0;$('#pv2').disabled=S.pg>=pages-1;selection();if(S.pick && S.tab==='novo' && !$('#v-pick').hidden)loadThumbs(shown,token)}
async function loadThumbs(list,token){
 const tasks=list.filter(f=>['img','vid'].includes(f.type));
 const generation=S.generation;
 async function worker(){while(tasks.length && token===S.galleryToken){
  const f=tasks.shift();let data=S.thumbs.get(f.id);
  if(data===undefined){
   const key=generation+':'+f.id;
   if(!(S.thumbPending instanceof Map))S.thumbPending=new Map();
   if(!S.thumbPending.has(key))S.thumbPending.set(key,call('preview',f.id,false,false));
   try{data=await S.thumbPending.get(key)}finally{S.thumbPending.delete(key)}
   if(generation!==S.generation)return;
   if(data?.frames?.length)S.thumbs.set(f.id,data);
   while(S.thumbs.size>96)S.thumbs.delete(S.thumbs.keys().next().value);
  }
  if(token!==S.galleryToken)return;
  const tile=$(`#gal [data-id="${f.id}"]`);if(!tile)continue;
  if(data?.frames?.length){const img=document.createElement('img');img.src=data.frames[0];img.alt=f.name;tile.querySelector('.placeholder')?.remove();tile.prepend(img)}
  else{const label=tile.querySelector('.placeholder');if(label){label.textContent=f.type==='vid'?'Capa indisponível':'Prévia indisponível';label.title='Isso não confirma que o arquivo esteja quebrado.'}}
 }}
 await Promise.all([worker(),worker(),worker()]);
}
$('#gal').onclick=e=>{const o=e.target.closest('[data-image]'),v=e.target.closest('[data-video]'),tile=e.target.closest('[data-id]');if(o)return openImage(+o.dataset.image);if(v)return openVideo(+v.dataset.video);if(tile){const id=+tile.dataset.id;S.sel.has(id)?S.sel.delete(id):S.sel.add(id);tile.classList.toggle('s',S.sel.has(id));tile.setAttribute('aria-checked',String(S.sel.has(id)));tile.querySelector('.ck').textContent=S.sel.has(id)?'✓':'';selection()}};
$('#gal').onkeydown=e=>{if(e.key===' '||e.key==='Enter'){e.preventDefault();e.target.click()}};
$('#res').onclick=e=>{const b=e.target.closest('[data-post]');if(b){const list=S.items.filter(f=>f.post===b.dataset.post),clear=list.some(f=>S.sel.has(f.id));list.forEach(f=>clear?S.sel.delete(f.id):S.sel.add(f.id));renderGallery()}};
$('#ft').onclick=e=>{const b=e.target.closest('[data-t]');if(b){S.kind=b.dataset.t;S.pg=0;document.querySelectorAll('#ft button').forEach(x=>x.classList.toggle('on',x===b));renderGallery()}};
$('#sq').oninput=$('#fh').onchange=()=>{S.pg=0;renderGallery()};$('#pv1').onclick=()=>{S.pg--;renderGallery()};$('#pv2').onclick=()=>{S.pg++;renderGallery()};
$('#sv').onclick=()=>{filtered().forEach(f=>S.sel.add(f.id));renderGallery()};$('#dv').onclick=()=>{filtered().forEach(f=>S.sel.delete(f.id));renderGallery()};
async function start(ids){if(await call('download',ids)){S.state='downloading';tab('down');setBusy()}}
$('#enq').onclick=()=>start([...S.sel]);$('#enqa').onclick=()=>start(null);
function busy(){return ['scanning','downloading','paused','cancelling'].includes(S.state)}
function setBusy(){const b=busy();$('#an').disabled=$('#vz').disabled=$('#up').disabled=b;$('#an').textContent=S.state==='scanning'?'Analisando…':'Baixar tudo';$('#bk').hidden=!S.items.length;$('#bm').disabled=!['downloading','paused'].includes(S.state);$('#bm').textContent=S.state==='paused'?'Continuar':'Pausar';$('#b4').disabled=!b;$('#b5').disabled=b;cookieButtons();selection()}
function renderQueue(){
 $('#dne').hidden=!!S.items.length||busy();
 $('#dn').hidden=!S.items.length&&!busy();
 const queue=$('#qb');
 if(!S.queue.length){
  queue.innerHTML=S.items.length&&!busy()?'<p class="mu sm">Nenhum arquivo na fila.</p>':'';
  return;
 }
 const hidden=S.remaining-S.queue.length;
 queue.innerHTML='<div class="card" style="padding:6px 22px">'+S.queue.map(f=>`<div class="it" data-row="${f.id}"><div class="ic">${f.type==='vid'?'▶':f.type==='audio'?'♪':'▧'}</div><div><div class="nm" title="${esc(f.name)}">${esc(f.name)}</div><div class="mu sm">${esc(f.host)}</div></div><div class="pr"><div class="pb"><i style="width:${f.progress}%"></i></div></div><span class="sm state">${status[f.status]}</span></div>`).join('')+'</div>'+(hidden>0?`<p class="mu sm">Mais ${hidden.toLocaleString('pt-BR')} na fila</p>`:'');
}
function metrics(p){const count=k=>S.items.filter(f=>f.status===k).length,ok=count('ok'),skip=count('skip'),fail=count('fail'),done=ok+skip+fail,total=done+S.remaining;
$('#bp').style.width=(total?done/total*100:0)+'%';$('#d1').textContent=({idle:'Pronto',scanning:'Analisando thread',gallery:'Arquivos disponíveis',downloading:'Baixando',paused:'Pausado — arquivos ativos podem terminar',cancelling:'Cancelando…',cancelled:'Interrompido — andamento preservado',done:'Concluído',partial:'Concluído com pendências',error:'Não foi possível concluir'})[p.state]||p.state;
$('#d2').textContent=`${ok} concluídos · ${skip} ignorados · ${fail} falhas · ${Math.floor(p.elapsed||0)} s`;
$('#bs').innerHTML=[['Concluídos',ok],['Pendentes',S.remaining],['Ignorados',skip],['Falhas',fail]].map(([l,n])=>`<div><span>${l}</span><b>${n}</b></div>`).join('');
$('#bf').hidden=!fail;$('#b5').hidden=!fail&&!(p.retry_pending&&!busy());$('#b5').textContent=p.retry_pending&&!busy()?'Retomar pendências':'Tentar falhas de novo';$('#bf').textContent=`Ver falhas (${fail})`;$('#sv2').textContent=p.saved?'Fila salva para recuperação; conclusões são registradas em disco.':'';updateLogs();if(!$('#fm').hidden)renderFailures()}
$('#bm').onclick=()=>call('pause_download');$('#b4').onclick=()=>call('cancel_download');$('#bo').onclick=()=>call('open_folder',null);$('#bd').onclick=()=>$('#bs').hidden=!$('#bs').hidden;$('#act').ontoggle=updateLogs;
function renderFailures(){const list=S.items.filter(f=>f.status==='fail'),pages=Math.max(1,Math.ceil(list.length/20));S.fp=Math.min(S.fp,pages-1);$('#ft2').textContent=`Falhas (${list.length})`;$('#fl').innerHTML=list.slice(S.fp*20,S.fp*20+20).map(f=>`<div class="fc"><div><b>${esc(f.name)}</b><p class="er sm">${esc(f.error)}</p></div><button data-check="${f.id}">Abrir no navegador</button><button data-retry="${f.id}" ${busy()?'disabled':''}>Tentar novamente</button></div>`).join('')||'<p>Nenhuma falha.</p>';$('#fa').disabled=busy()||!list.length;$('#fail-page').textContent=`${S.fp+1} / ${pages}`;$('#fail-prev').disabled=!S.fp;$('#fail-next').disabled=S.fp>=pages-1}
$('#bf').onclick=()=>{$('#fm').hidden=false;renderFailures()};$('#fx').onclick=()=>$('#fm').hidden=true;$('#fail-prev').onclick=()=>{S.fp--;renderFailures()};$('#fail-next').onclick=()=>{S.fp++;renderFailures()};
async function retry(ids){if(await call('retry',ids)){S.state='downloading';setBusy();tab('down')}}
$('#fl').onclick=e=>{const b=e.target.closest('[data-retry]');if(b)retry([+b.dataset.retry]);const link=e.target.closest('[data-check]');if(link)call('open_origin',+link.dataset.check)};$('#fa').onclick=$('#b5').onclick=()=>retry(null);
function renderHistory(){$('#hb').innerHTML=S.history.map((h,i)=>`<div class="it" style="grid-template-columns:1fr 130px auto"><div><div class="nm">${esc(h.title)}</div><div class="mu sm">${esc(h.date)} · ${h.completed} de ${h.total}${h.simulation?' · simulação':''}</div></div><span class="sm">${({done:'Concluído',partial:'Parcial',cancelled:'Interrompido'})[h.status]||esc(h.status)}</span><button data-history="${i}">Abrir pasta</button></div>`).join('')||'<div class="emp">Nenhuma tarefa concluída ainda.</div>'}
$('#hb').onclick=e=>{const b=e.target.closest('[data-history]');if(b)call('open_folder',+b.dataset.history)};
function updateLogs(){
 const text=S.logs.join('\n')||'Log vazio.';
 for(const [box,shown] of [[$('#lg2'),$('#act').open],[$('#lg'),!$('#lg').hidden]]){
  if(shown && box!==document.activeElement && box.value!==text)box.value=text;
 }
}
$('#ol').onclick=()=>{$('#cl').hidden=$('#lg').hidden=!$('#lg').hidden;updateLogs()};
$('#xr').onclick=async()=>{const text=await call('report',true);if(text){$('#rp').value=text;$('#rp').hidden=false;$('#cp').hidden=false}};
$('#timing-refresh').onclick=async()=>{
 const report=await call('file_timings',false);
 if(!report)return;
 $('#timing-refresh').textContent='Atualizar tempos';
 $('#timing-summary').textContent=report.total
  ? `Exibindo ${report.items.length} de ${report.total} resultados. O CSV inclui todos os resultados e os nomes dos arquivos.`
  : 'Ainda não há tempos registrados para esta fila.';
 const date=value=>new Date(value).toLocaleString('pt-BR');
 const seconds=value=>Number(value).toLocaleString('pt-BR',{maximumFractionDigits:2})+' s';
 const rows=report.items.map(item=>[
  item.name,item.host,status[item.status]||item.status,date(item.started),date(item.finished),
  seconds(item.duration),seconds(item.transfer),item.size==null?'—':bytes(item.size),
  item.speed?bytes(item.speed)+'/s':'—'
 ]);
 $('#timing-list').innerHTML=rows.length
  ? '<table class="timing-table"><thead><tr><th>Arquivo</th><th>Servidor</th><th>Resultado</th><th>Início</th><th>Fim</th><th>Total</th><th>Download</th><th>Tamanho</th><th>Vazão</th></tr></thead><tbody>'+
    rows.map(cells=>'<tr>'+cells.map(cell=>'<td>'+esc(cell)+'</td>').join('')+'</tr>').join('')+'</tbody></table>'
  : '';
};
$('#timing-export').onclick=async()=>{if(await call('file_timings',true))toast('CSV de tempos salvo.')};
document.querySelectorAll('[data-copy-log]').forEach(button=>button.onclick=async()=>{
 updateLogs();
 const box=document.getElementById(button.dataset.copyLog);
 if(!box.value)return;
 if(await call('copy_text',box.value)){toast('Texto copiado.');return}
 box.focus();box.select();toast('Texto selecionado. Pressione Ctrl+C para copiar.');
});
async function openVideo(id,refresh=false){clearInterval(videoTimer);videoTimer=null;const token=++S.videoToken;S.video=id;S.frame=0;S.frames=[];$('#md').hidden=false;$('#mt').textContent=S.items[id].name;$('#pvs').textContent='Carregando prévia…';$('#pr').disabled=true;$('#video-frame').hidden=true;const data=await call('preview',id,true,refresh);if(token!==S.videoToken||$('#md').hidden)return;S.frames=data?.frames||[];$('#pvs').textContent=data?.message||'Prévia indisponível. O download continua disponível.';$('#pr').disabled=S.frames.length<2;$('#pr').textContent='Reproduzir';if(S.frames.length){$('#video-frame').src=S.frames[0];$('#video-frame').hidden=false;$('#md .pl svg').style.display='none';S.thumbs.set(id,{frames:[S.frames[0]]});if(S.pick)renderGallery();if(S.frames.length>1)$('#pr').click()}}
$('#pr').onclick=()=>{if(videoTimer){clearInterval(videoTimer);videoTimer=null;$('#pr').textContent='Reproduzir';return}$('#pr').textContent='Pausar';videoTimer=setInterval(()=>{$('#video-frame').src=S.frames[S.frame++];$('#pvb').style.width=S.frame/S.frames.length*100+'%';if(S.frame>=S.frames.length){clearInterval(videoTimer);videoTimer=null;S.frame=0;$('#pr').textContent='Reproduzir'}},125)};
$('#pa2').onclick=()=>openVideo(S.video,true);$('#mc').onclick=async()=>{S.videoToken++;clearInterval(videoTimer);videoTimer=null;$('#md').hidden=true;S.frames=[];$('#video-frame').removeAttribute('src');await call('stop_preview');if(S.pick)renderGallery()};
function showRecovery(){const r=S.recover[0];$('#rec').hidden=!r;if(r){$('#rt').textContent=r.title;$('#ri').textContent=`${r.count} arquivos na fila · ${r.destination||''}. Arquivos concluídos serão reaproveitados.`}}
$('#rd').onclick=async()=>{if(await call('recover',S.recover[0].id,true)){S.recover.shift();showRecovery()}};
$('#rc').onclick=async()=>{const r=await call('recover',S.recover[0].id,false);if(r){S.recover.shift();$('#rec').hidden=true;await loadCatalog();S.sel=new Set(r.ids||S.items.map(f=>f.id));S.pick=true;tab('novo');renderGallery();if(r.download)await start(r.ids)}};
async function loadCatalog(){if(S.catalogBusy)return;S.catalogBusy=true;try{let first=await call('catalog',0,500);if(!first)return;const items=first.items;for(let offset=500;offset<first.total;offset+=500){const page=await call('catalog',offset,500);if(!page||page.generation!==first.generation)return;items.push(...page.items)}S.items=items;S.generation=first.generation;S.title=first.title;S.posts=first.posts;S.sel=new Set(items.map(f=>f.id));S.thumbs.clear();S.pg=0;$('#fh').innerHTML='<option value="*">Todos os hosts</option>'+[...new Set(items.map(f=>f.host))].map(h=>`<option value="${esc(h)}">${esc(h)}</option>`).join('');renderQueue();renderGallery()}finally{S.catalogBusy=false}}
async function poll(){try{const p=await api.poll();const previous=S.state;S.state=p.state;S.logs=p.logs;S.history=p.history;if(p.generation!==S.generation)await loadCatalog();for(const row of p.items)S.items[row.id]=row;S.queue=p.queue;S.remaining=p.remaining;if(S.tab==='down')renderQueue();metrics(p);setBusy();if(p.state==='gallery'&&previous==='scanning'){S.pick=true;tab('novo');renderGallery()}if(['downloading','error','partial','done','cancelled'].includes(p.state)&&previous==='scanning')tab('down');if(p.error&&p.error!==S.error){S.error=p.error;toast(p.error);$('#act').open=true}if(S.tab==='hist')renderHistory()}catch(e){toast('Falha de comunicação: '+(e.message||e))}finally{setTimeout(poll,600)}}
window.addEventListener('pywebviewready',async()=>{api=window.pywebview.api;const b=await call('bootstrap');if(!b)return;fill({destination:b.destination,...b.settings});showCookieStatus(b.cookie_status);S.history=b.history;S.recover=b.recovery;showRecovery();tab('novo');poll()});
tab('novo');

let imageToken=0, imageId=null;
async function openImage(id){
 const token=++imageToken;imageId=id;
 $('#image-modal').hidden=false;$('#image-name').textContent=S.items[id].name;
 $('#full-image').hidden=true;$('#full-image').removeAttribute('src');
 $('#image-message').textContent='Carregando imagem…';$('#image-retry').disabled=true;
 $('#image-close').focus();
 const result=await call('image_preview',id);
 if(token!==imageToken)return;
 $('#image-message').textContent=result?.message||'Não foi possível carregar a imagem.';
 $('#image-retry').disabled=false;
 if(result?.image){$('#full-image').src=result.image;$('#full-image').alt=S.items[id].name;$('#full-image').hidden=false}
}
async function closeImage(){imageToken++;$('#image-modal').hidden=true;$('#full-image').removeAttribute('src');await call('stop_preview');if(S.pick)renderGallery();const button=$(`#gal [data-image="${imageId}"]`);button?.focus()}
$('#image-close').onclick=closeImage;
$('#image-retry').onclick=()=>openImage(imageId);
$('#image-modal').onclick=e=>{if(e.target===$('#image-modal'))closeImage()};
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('#image-modal').hidden)closeImage()});

document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('#md').hidden)$('#mc').click()});
