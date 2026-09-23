/* Persistence adapter for the V3.5 domain objects. UI-only state is excluded. */
(() => {
  'use strict';
  const arrays = {FACTORS,SITES,PROCS,LINES,MACH,REC,MOV,PERIODS,WASTECAT,RULES};
  const indexes = {FACTORS:FBI,SITES:SBI,PROCS:PBI,LINES:LBI,MACH:MBI,WASTECAT:WCBI,RULES:RBI};
  const ruleTests=Object.fromEntries(RULES.map(r=>[r.id,r.test]));
  let revision=0, ready=false, busy=false, blocked=false, base=null, mappingProfiles={};
  let demoRecordIds=REC.map(r=>r.rid), demoMovementIds=MOV.map(m=>m.id);
  const clone=x=>JSON.parse(JSON.stringify(x));
  const gate=document.createElement('div');
  gate.style.cssText='position:fixed;inset:0;z-index:190;background:#f2f5f0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;color:#173d35;font:16px system-ui';
  const gateLogo=document.querySelector('.mark img')?.cloneNode(true);
  if(gateLogo){gateLogo.style.cssText='height:64px;width:auto;animation:boot-pulse 1.4s ease-in-out infinite';gate.append(gateLogo);}
  const gateName=document.createElement('div');gateName.style.cssText='font:600 15px system-ui;letter-spacing:.2px';gateName.textContent='Cargando IVZ Carbon…';gate.append(gateName);
  if(!document.getElementById('boot-pulse-style')){const style=document.createElement('style');style.id='boot-pulse-style';style.textContent='@keyframes boot-pulse{0%,100%{opacity:.4;transform:scale(.92)}50%{opacity:1;transform:scale(1)}}';document.head.append(style);}
  document.body.append(gate);
  let lastSaveError='';
  function showSaveError(text){
    if(text===lastSaveError)return;
    lastSaveError=text;
    const dialog=document.createElement('dialog');
    dialog.style.cssText='max-width:480px;border:1px solid #bdcec2;border-radius:12px;padding:24px;font:14px system-ui;color:#234c3b';
    const title=document.createElement('h3');title.textContent='No se pudieron guardar los cambios';
    const detail=document.createElement('p');detail.textContent=text;
    const backup=document.createElement('button');backup.className='btn';backup.textContent='Descargar respaldo';
    backup.onclick=()=>download({revision,state:snapshot()},'ivz-carbon-respaldo.json');
    const close=document.createElement('button');close.className='btn';close.textContent='Cerrar';close.onclick=()=>dialog.close();
    dialog.append(title,detail,backup,close);dialog.addEventListener('close',()=>dialog.remove());
    document.body.append(dialog);dialog.showModal();
  }
  function snapshot(){return {schemaVersion:1,...arrays,BIZ,PLACES,settings:{uqOverrides:S.uqOverrides,uqAudit:S.uqAudit,recentImports:S.recentImports},counters:{RID,MOVN,MDN,UID},demoRecordIds,demoMovementIds,mappingProfiles};}
  const originalDemo=clone(snapshot());
  /* The server keeps records (REC) and movements (MOV) as rows: the screen loads them in pages and
     saves only what changed, so no request carries the whole inventory (Vercel caps requests at 4.5 MB). */
  const ROWS={REC:'rid',MOV:'id'}, PAGE=2000, PART_BYTES=2500000;
  function catalogue(){const {REC:_r,MOV:_m,...rest}=snapshot();return rest;}
  // Changes since the last save, and the baseline to keep once they are saved. `initial` only builds the baseline.
  function diff(initial=false){
    const next={catalogue:JSON.stringify(catalogue())}, out={changes:{},order:{}};let any=false;
    if(!initial&&next.catalogue!==base.catalogue){out.catalogue=next.catalogue;any=true;}
    for(const [kind,key] of Object.entries(ROWS)){
      const rows=new Map(), upsert=[], ids=arrays[kind].map(item=>item[key]);
      for(const item of arrays[kind]){const json=JSON.stringify(item);rows.set(item[key],json);if(!initial&&base.rows[kind].get(item[key])!==json)upsert.push(json);}
      (next.rows??={})[kind]=rows;(next.ids??={})[kind]=ids;
      if(initial)continue;
      const removed=[...base.rows[kind].keys()].filter(id=>!rows.has(id));
      if(upsert.length||removed.length){out.changes[kind]={upsert,delete:removed};any=true;}
      // New rows go to the end on the server; send the full order only when the screen placed them elsewhere or reordered.
      const expected=base.ids[kind].filter(id=>rows.has(id)).concat(ids.filter(id=>!base.rows[kind].has(id)));
      if(ids.some((id,i)=>id!==expected[i])){out.order[kind]=ids;any=true;}
    }
    return {any,out,next};
  }
  const rowsJson=changes=>'{'+Object.entries(changes).map(([kind,c])=>JSON.stringify(kind)+':{"upsert":['+c.upsert.join(',')+']'+(c.delete?',"delete":'+JSON.stringify(c.delete):'')+'}').join(',')+'}';
  async function send(out){
    // A large change set travels in parts; the final request names the batch and the server applies it all at once.
    let batch=null, parts=0, chunk={}, chunkBytes=0;
    const total=Object.values(out.changes).reduce((n,c)=>n+c.upsert.reduce((m,json)=>m+json.length,0),0);
    if(total>PART_BYTES){
      batch='b'+Date.now().toString(36)+Math.random().toString(36).slice(2,10);
      const flush=async()=>{if(!chunkBytes)return;await api('/api/state/upload',{method:'POST',body:'{"batch":"'+batch+'","part":'+parts+',"changes":'+rowsJson(chunk)+'}'});parts++;chunk={};chunkBytes=0;};
      for(const [kind,c] of Object.entries(out.changes)){
        for(const json of c.upsert){if(chunkBytes+json.length>PART_BYTES)await flush();(chunk[kind]??={upsert:[]}).upsert.push(json);chunkBytes+=json.length;}
        c.upsert=[];
      }
      await flush();
    }
    let body='{"revision":'+revision+',"changes":'+rowsJson(out.changes)+',"order":'+JSON.stringify(out.order);
    if(out.catalogue)body+=',"catalogue":'+out.catalogue;
    if(batch)body+=',"batch":"'+batch+'","parts":'+parts;
    return api('/api/state/changes',{method:'POST',body:body+'}'});
  }
  async function loadInventory(){
    for(let attempt=0;attempt<3;attempt++){
      const cat=await api('/api/state/catalogue');
      if(!cat.state)return cat;
      const state={...cat.state};let consistent=true;
      for(const kind of Object.keys(ROWS)){
        state[kind]=[];
        for(let offset=0;consistent&&offset<cat.counts[kind];offset+=PAGE){
          const page=await api('/api/state/rows?kind='+kind+'&offset='+offset+'&limit='+PAGE);
          if(page.revision!==cat.revision)consistent=false;else for(const item of page.items)state[kind].push(item);
        }
      }
      if(consistent)return {revision:cat.revision,state,updated:cat.updated};
    }
    throw Error('El inventario cambió mientras se cargaba. Recargá la página.');
  }
  let company='IVZ Carbon';
  let username='', impersonating=false;
  /* Closed years keep their results and cannot be edited until an administrator reopens them.
     The server enforces it; the screen undoes such an edit right away instead of failing to save. */
  let closures=[], closedYears=new Set();
  const DERIVED=['kg','bio','scope','cat','unit','sub'];
  const yearOf=item=>+String(item&&item.p||'').slice(0,4);
  const essential=item=>{const copy={...item};for(const k of DERIVED)delete copy[k];return JSON.stringify(copy);};
  async function loadClosures(){closures=await api('/api/closures');closedYears=new Set(closures.map(c=>c.year));}
  function enforceClosed(){
    if(!closedYears.size||!base)return;
    let reverted=0;
    for(const [kind,key] of Object.entries(ROWS)){
      const list=arrays[kind], seen=new Set();
      for(let i=list.length-1;i>=0;i--){
        const item=list[i], json=base.rows[kind].get(item[key]), old=json&&JSON.parse(json);
        seen.add(item[key]);
        if(!closedYears.has(yearOf(item))&&!(old&&closedYears.has(yearOf(old))))continue;
        if(old&&essential(old)===essential(item))continue;
        if(old)list[i]=old;else list.splice(i,1);
        reverted++;
      }
      for(const [id,json] of base.rows[kind])if(!seen.has(id)){const old=JSON.parse(json);if(closedYears.has(yearOf(old))){list.push(old);reverted++;}}
    }
    if(reverted){render();toast('Ese año está cerrado: el cambio no se guardó.','!');}
  }
  function renderYears(){
    const box=document.getElementById('account-years');if(!box)return;
    const years=[...new Set(PERIODS.map(p=>+p.slice(0,4)))].sort();
    const byYear=Object.fromEntries(closures.map(c=>[c.year,c]));
    box.innerHTML='<p style="margin-bottom:4px"><b>Cierre de años</b></p><p style="margin-top:0;color:#65786c;font-size:12.5px">Un año cerrado conserva sus resultados y ya no se puede editar. Solo un administrador de Invenzis puede reabrirlo.</p>'+
      years.map(y=>{const c=byYear[y];return '<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;padding:7px 0;border-top:1px solid #e3ebe5"><span><b>'+y+'</b> · '+(c?'Cerrado el '+esc(new Date(c.closedAt).toLocaleDateString('es'))+' por '+esc(c.closedBy):'Abierto')+'</span>'+
        (c?(impersonating?'<button class="btn" data-reopen="'+y+'">Reabrir</button>':''):'<button class="btn" data-close="'+y+'">Cerrar año</button>')+'</div>';}).join('');
    for(const b of box.querySelectorAll('[data-close]'))b.onclick=()=>closeYear(+b.dataset.close,b);
    for(const b of box.querySelectorAll('[data-reopen]'))b.onclick=()=>reopenYear(+b.dataset.reopen,b);
  }
  async function closeYear(year,button){
    const rows=REC.filter(r=>yearOf(r)===year), pending=rows.filter(r=>r.cls&&r.cls.state!=='auto').length;
    const tonnes=(rows.reduce((a,r)=>a+(r.kg||0),0)/1000).toLocaleString('es',{maximumFractionDigits:1});
    if(!confirm('¿Cerrar '+year+'? '+rows.length.toLocaleString('es')+' registros, '+tonnes+' tCO₂e.'+
      (pending?'\n\nAtención: '+pending+' registros de '+year+' todavía están pendientes de revisar.':'')+
      '\n\nDespués no se van a poder agregar, editar ni borrar registros de ese año, y sus resultados quedan fijos.'))return;
    button.disabled=true;
    try{
      if(!await save())throw Error('No se pudieron guardar los cambios pendientes.');
      await api('/api/closures',{method:'POST',body:JSON.stringify({year})});
      await loadClosures();renderYears();toast(year+' cerrado');
    }catch(e){button.disabled=false;document.getElementById('account-error').textContent=e.message;}
  }
  async function reopenYear(year,button){
    const reason=prompt('Motivo para reabrir '+year+' (queda registrado):');
    if(!reason||!reason.trim())return;
    button.disabled=true;
    try{await api('/api/closures/'+year+'/reopen',{method:'POST',body:JSON.stringify({reason:reason.trim()})});await loadClosures();renderYears();toast(year+' reabierto');}
    catch(e){button.disabled=false;document.getElementById('account-error').textContent=e.message;}
  }
  document.getElementById('btn-user').onclick=()=>{
    openModal('<div class="modal-h"><h3>Configuración de la cuenta</h3></div><div class="modal-b"><p><b>Usuario</b><br>'+esc(username)+'</p><p><b>Organización</b><br>'+esc(company)+'</p><div id="account-years"></div><p id="account-error" role="alert"></p></div><div class="modal-f"><button class="btn" onclick="closeModal()">Volver</button><button class="btn" id="account-logout">Cerrar sesión</button></div>',{narrow:true});
    renderYears();
    document.getElementById('account-logout').onclick=async event=>{
      const button=event.currentTarget;button.disabled=true;
      try{
        if(!await save()){button.disabled=false;return;}
        await api('/api/logout',{method:'POST'});ready=false;location.replace('/');
      }catch(e){button.disabled=false;document.getElementById('account-error').textContent=e.message;}
    };
  };
  const originalRender=render;
  render=function(){
    originalRender();
    const tenant=document.querySelector('.tenant');
    tenant.removeAttribute('onclick');tenant.querySelector('.t1').textContent=company;
    const crumb=document.getElementById('crumb');if(crumb.firstChild)crumb.firstChild.textContent=company+' ';
    const demoIds=new Set(demoRecordIds);
    document.querySelector('.demo-flag').textContent=REC.some(r=>demoIds.has(r.rid))?'Contiene datos demo':'Inventario de la empresa';
  };
  function hydrate(data){
    for(const [name,target] of Object.entries(arrays)){
      target.length=0;for(const item of data[name])target.push(item);
      if(name==='RULES')for(const row of target)row.test=ruleTests[row.id]||(()=>false);
      if(indexes[name]){for(const key of Object.keys(indexes[name]))delete indexes[name][key];for(const row of target)indexes[name][row.id]=row;}
    }
    BIZ=data.BIZ;
    for(const key of Object.keys(PLACES))delete PLACES[key];Object.assign(PLACES,data.PLACES);
    for(const key of ['uqOverrides','uqAudit','recentImports'])if(data.settings[key]!==undefined)S[key]=data.settings[key];
    ({RID,MOVN,MDN,UID}=data.counters);
    // Never reuse record IDs after a reload or an imported snapshot.
    RID=Math.max(RID,...REC.map(r=>Number(r.rid.replace(/^R/,''))||0));
    demoRecordIds=data.demoRecordIds||[];demoMovementIds=data.demoMovementIds||[];
    mappingProfiles=data.mappingProfiles||{};
    S.f={year:PERIODS.length?PERIODS[PERIODS.length-1].slice(0,4):String(new Date().getFullYear()),month:'all',site:'all'};
    S.view='dash';S.node=null;S.invSel=null;S.mv.sel=null;
  }
  async function api(path,options={}){
    const r=await fetch(path,{...options,headers:{'Content-Type':'application/json','X-IVZ-Carbon':'1',...options.headers}});
    if(!r.ok){let body;try{body=await r.json()}catch{body={detail:await r.text().catch(()=>r.statusText)}}const e=Error(typeof body.detail==='string'?body.detail:'La solicitud contiene datos inválidos.');e.status=r.status;throw e;}
    return r.json();
  }
  window.api=api;
  function download(value,name){const a=document.createElement('a');const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  let pendingSave=null;
  async function save(){
    if(busy){await pendingSave;return save();}
    if(!ready||blocked)return false;
    enforceClosed();
    const {any,out,next}=diff();
    if(!any)return true;
    busy=true;
    pendingSave=(async()=>{
      try{const result=await send(out);revision=result.revision;base=next;lastSaveError='';return true;}
      catch(e){
        if(e.status===423){await loadClosures().catch(()=>{});enforceClosed();return false;}  // closed from another tab
        if(e.status===409||e.status===401)blocked=true;showSaveError((e.status===401?'La sesión venció. Descargá un respaldo antes de volver a ingresar. ':e.status===409?'Conflicto. Descargá un respaldo y recargá. ':'No se guardó: ')+e.message);return false;}
      finally{busy=false;}
    })();
    return pendingSave;
  }
  window.addEventListener('beforeunload',e=>{if(ready&&diff().any){e.preventDefault();e.returnValue='';}});
  // Replace positional deletion: after reload, user records must never become demo records.
  clearDemoData=function(){const ids=new Set(demoRecordIds),mids=new Set(demoMovementIds);for(let i=REC.length-1;i>=0;i--)if(ids.has(REC[i].rid))REC.splice(i,1);for(let i=MOV.length-1;i>=0;i--)if(mids.has(MOV[i].id))MOV.splice(i,1);demoRecordIds=[];demoMovementIds=[];S.recentImports=[];closeModal();render();};
  restoreDemoData=function(){hydrate(clone(originalDemo));closeModal();render();};
  // Mapping profiles belong to the account and are included in the server snapshot.
  window.carbonProfiles={get:()=>JSON.stringify(mappingProfiles),set:value=>{mappingProfiles=JSON.parse(value)}};
  function activate(result){revision=result.revision;hydrate(result.state);base=diff(true).next;ready=true;gate.remove();render();setInterval(save,3000);}
  function showImpersonationBar(){
    const bar=document.createElement('div');
    bar.style.cssText='position:fixed;top:0;left:0;right:0;z-index:200;background:#8a5a1a;color:#fff;font:13px system-ui;display:flex;align-items:center;justify-content:center;gap:14px;padding:8px 16px';
    bar.innerHTML='<span>Estás viendo esta cuenta como administrador.</span>';
    const back=document.createElement('button');
    back.textContent='Volver a administración';
    back.style.cssText='font:inherit;cursor:pointer;border-radius:6px;border:1px solid #fff6;background:transparent;color:#fff;padding:4px 10px';
    back.onclick=async()=>{back.disabled=true;try{await api('/api/admin/return',{method:'POST'});location.replace('/admin')}catch(e){back.disabled=false}};
    bar.append(back);document.body.prepend(bar);
    document.body.style.paddingTop='36px';
  }
  async function boot(){
    try{
      const [result,user]=await Promise.all([loadInventory(),api('/api/me'),loadClosures()]);
      company=user.company;
      username=user.username;
      impersonating=!!user.impersonating;
      document.getElementById('profile-name').textContent=username;
      document.getElementById('profile-company').textContent=company;
      document.getElementById('profile-avatar').textContent=username.slice(0,2).toUpperCase();
      if(user.impersonating)showImpersonationBar();
      if(result.state){activate(result);return;}
      gate.innerHTML='<section style="max-width:510px;background:white;border-radius:16px;padding:36px"><h1>Tu inventario de carbono</h1><p>Elegí cómo empezar. La biblioteca de factores de referencia queda disponible en ambas opciones.</p><button class="btn primary" id="carbon-empty">Empezar vacío</button> <button class="btn" id="carbon-demo">Explorar con datos demo</button><p id="carbon-init-error" role="alert"></p></section>';
      for(const mode of ['empty','demo'])document.getElementById('carbon-'+mode).onclick=async()=>{for(const b of gate.querySelectorAll('button'))b.disabled=true;try{await api('/api/initialize',{method:'POST',body:JSON.stringify({mode})});activate(await loadInventory())}catch(e){document.getElementById('carbon-init-error').textContent=e.message;for(const b of gate.querySelectorAll('button'))b.disabled=false;}};
    }catch(e){gate.textContent='No se pudo cargar el inventario: '+e.message;}
  }
  boot();
})();
