/* Persistence adapter for the V3.5 domain objects. UI-only state is excluded. */
(() => {
  'use strict';
  const arrays = {FACTORS,SITES,PROCS,LINES,MACH,REC,MOV,PERIODS,WASTECAT,RULES};
  const indexes = {FACTORS:FBI,SITES:SBI,PROCS:PBI,LINES:LBI,MACH:MBI,WASTECAT:WCBI,RULES:RBI};
  const ruleTests=Object.fromEntries(RULES.map(r=>[r.id,r.test]));
  let revision=0, ready=false, busy=false, blocked=false, baseline='', mappingProfiles={};
  let demoRecordIds=REC.map(r=>r.rid), demoMovementIds=MOV.map(m=>m.id);
  const clone=x=>JSON.parse(JSON.stringify(x));
  const gate=document.createElement('div');
  gate.style.cssText='position:fixed;inset:0;z-index:190;background:#f2f5f0;display:grid;place-items:center;color:#234c3b;font:16px system-ui';
  gate.textContent='Cargando IVZ Carbon…';document.body.append(gate);
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
  let company='IVZ Carbon';
  let username='';
  document.getElementById('btn-user').onclick=()=>{
    openModal('<div class="modal-h"><h3>Configuración de la cuenta</h3></div><div class="modal-b"><p><b>Usuario</b><br>'+esc(username)+'</p><p><b>Organización</b><br>'+esc(company)+'</p><p id="account-error" role="alert"></p></div><div class="modal-f"><button class="btn" onclick="closeModal()">Volver</button><button class="btn" id="account-logout">Cerrar sesión</button></div>',{narrow:true});
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
  function download(value,name){const a=document.createElement('a');const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  let pendingSave=null;
  async function save(){
    if(busy){await pendingSave;return save();}
    if(!ready||blocked)return false;
    const serialized=JSON.stringify(snapshot());
    if(serialized===baseline)return true;
    busy=true;
    pendingSave=(async()=>{
      try{const result=await api('/api/state',{method:'PUT',body:JSON.stringify({revision,state:JSON.parse(serialized)})});revision=result.revision;baseline=serialized;lastSaveError='';return true;}
      catch(e){if(e.status===409||e.status===401)blocked=true;showSaveError((e.status===401?'La sesión venció. Descargá un respaldo antes de volver a ingresar. ':e.status===409?'Conflicto. Descargá un respaldo y recargá. ':'No se guardó: ')+e.message);return false;}
      finally{busy=false;}
    })();
    return pendingSave;
  }
  window.addEventListener('beforeunload',e=>{if(ready&&JSON.stringify(snapshot())!==baseline){e.preventDefault();e.returnValue='';}});
  // Replace positional deletion: after reload, user records must never become demo records.
  clearDemoData=function(){const ids=new Set(demoRecordIds),mids=new Set(demoMovementIds);for(let i=REC.length-1;i>=0;i--)if(ids.has(REC[i].rid))REC.splice(i,1);for(let i=MOV.length-1;i>=0;i--)if(mids.has(MOV[i].id))MOV.splice(i,1);demoRecordIds=[];demoMovementIds=[];S.recentImports=[];closeModal();render();};
  restoreDemoData=function(){hydrate(clone(originalDemo));closeModal();render();};
  // Mapping profiles belong to the account and are included in the server snapshot.
  window.carbonProfiles={get:()=>JSON.stringify(mappingProfiles),set:value=>{mappingProfiles=JSON.parse(value)}};
  function activate(result){revision=result.revision;hydrate(result.state);baseline=JSON.stringify(snapshot());ready=true;gate.remove();render();setInterval(save,3000);}
  async function boot(){
    try{
      const [result,user]=await Promise.all([api('/api/state'),api('/api/me')]);
      company=user.company;
      username=user.username;
      document.getElementById('profile-name').textContent=username;
      document.getElementById('profile-company').textContent=company;
      document.getElementById('profile-avatar').textContent=username.slice(0,2).toUpperCase();
      if(result.state){activate(result);return;}
      gate.innerHTML='<section style="max-width:510px;background:white;border-radius:16px;padding:36px"><h1>Tu inventario de carbono</h1><p>Elegí cómo empezar. La biblioteca de factores de referencia queda disponible en ambas opciones.</p><button class="btn primary" id="carbon-empty">Empezar vacío</button> <button class="btn" id="carbon-demo">Explorar con datos demo</button><p id="carbon-init-error" role="alert"></p></section>';
      for(const mode of ['empty','demo'])document.getElementById('carbon-'+mode).onclick=async()=>{for(const b of gate.querySelectorAll('button'))b.disabled=true;try{activate(await api('/api/initialize',{method:'POST',body:JSON.stringify({mode})}))}catch(e){document.getElementById('carbon-init-error').textContent=e.message;for(const b of gate.querySelectorAll('button'))b.disabled=false;}};
    }catch(e){gate.textContent='No se pudo cargar el inventario: '+e.message;}
  }
  boot();
})();
