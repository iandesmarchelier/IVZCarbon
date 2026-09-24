/* Annual reports use server-calculated immutable snapshots, not the browser totals. */
(() => {
  const fields = {
    activity:'Actividad y descripción de la organización', purpose:'Objetivo y destinatarios',
    boundary:'Límites y entidades incluidas', exclusions:'Exclusiones y justificación',
    significance:'Criterios de significancia de emisiones indirectas', basePolicy:'Año base y política de recálculo',
    methodology:'Metodología y supuestos', gwp:'Fuente y versión de PCG utilizados',
    quality:'Calidad de datos y tratamiento de faltantes', energy:'Evidencias de energía adquirida',
    gases:'Desagregación por gas y limitaciones', removals:'Remociones y método de cuantificación',
    actions:'Acciones de reducción y responsables', verification:'Verificación independiente y referencia al dictamen',
    responsible:'Responsable del inventario', reviewer:'Revisión y aprobación interna'
  };
  const drafts = new Map();
  window.generateCarbonReport = async (year) => {
    const site = S.f.site === 'all' ? null : S.f.site;
    const draftKey=JSON.stringify([year,site]);
    openModal('<div class="modal-h"><h3>Reporte anual ISO 14064-1 · '+year+'</h3></div><div class="modal-b" id="annual-report-loading"><p>Cargando los datos del último reporte…</p></div><div class="modal-f"><button class="btn" onclick="closeModal()">Cerrar</button></div>');
    const loading=document.getElementById('annual-report-loading');
    try {
      const history = await api('/api/reports');
      const matching=history.find(r=>(r.site||null)===site && r.year===year) || history.find(r=>(r.site||null)===site && r.year<year);
      const latest = matching ? await api('/api/reports/'+encodeURIComponent(matching.id)) : null;
      if(document.getElementById('annual-report-loading')!==loading)return;
      let draft = drafts.get(draftKey) || {notes:latest?.notes || {}, consolidation:latest?.consolidation || 'Control operacional'};
      const scope = site ? SBI[site]?.name || site : 'Todos los sitios';
      openModal('<div class="modal-h"><h3>Generar reporte anual '+year+'</h3></div><form id="annual-report-form"><div class="modal-b" style="max-height:68vh;overflow:auto">'+
        '<p><b>'+esc(scope)+'</b> · enero a diciembre. El filtro mensual no limita el reporte anual.</p><p>Se calcularán los resultados y gráficos con los datos guardados. Podés completar la información descriptiva ahora o generar una versión para revisión con los faltantes identificados.</p>'+
        '<label style="display:block;margin:14px 0">Enfoque de consolidación<select name="consolidation" style="display:block;width:100%;margin-top:6px">'+['Control operacional','Control financiero','Participación accionaria'].map(x=>'<option'+(draft.consolidation===x?' selected':'')+'>'+x+'</option>').join('')+'</select></label>'+
        Object.entries(fields).map(([key,label],i)=>'<label style="display:block;margin:14px 0">'+esc(label)+'<textarea name="'+key+'" maxlength="6000" rows="'+(i<2?2:3)+'" style="display:block;width:100%;margin-top:6px;padding:10px;border:1px solid #cad7ce;border-radius:6px;font:inherit" placeholder="No informado">'+esc(draft.notes[key]||'')+'</textarea></label>').join('')+
        '<p class="dim tiny">Se reutilizan textos del mismo perímetro. Revisá su vigencia para este año. La generación no implica certificación ni verificación independiente.</p><p id="annual-report-error" role="alert" style="color:#a63832"></p></div><div class="modal-f"><button type="button" class="btn" onclick="closeModal()">Cancelar</button><button class="btn primary" type="submit">Generar y abrir reporte</button></div></form>');
      const form=document.getElementById('annual-report-form');
      const capture=()=>{const data=new FormData(form);draft={consolidation:data.get('consolidation'),notes:Object.fromEntries(Object.keys(fields).map(key=>[key,data.get(key).trim()]))};drafts.set(draftKey,draft);};
      form.oninput=capture;
      form.onsubmit=async ev=>{
        ev.preventDefault();capture();
        const button=form.querySelector('[type=submit]');button.disabled=true;button.textContent='Calculando y guardando…';
        const target=window.open('about:blank','_blank');
        if(target){target.opener=null;target.document.body.textContent='Generando reporte anual de IVZ Carbon…';}
        try {
          if(!await carbonSave())throw Error('No se pudieron guardar los cambios del inventario. Resolvé el aviso de guardado y volvé a generar.');
          const report=await api('/api/reports',{method:'POST',body:JSON.stringify({year,site,...draft})});
          const url='/reports/'+encodeURIComponent(report.id);
          closeModal();
          if(target)target.location.replace(url);
          else openModal('<div class="modal-h"><h3>Reporte guardado</h3></div><div class="modal-b"><p>El navegador bloqueó la nueva pestaña.</p><a class="btn primary" href="'+url+'" target="_blank" rel="noopener">Abrir reporte</a></div><div class="modal-f"><button class="btn" onclick="closeModal()">Cerrar</button></div>');
          toast('Reporte anual guardado. Disponible en el historial.');
        }catch(error){if(target)target.close();const errorBox=document.getElementById('annual-report-error');if(errorBox)errorBox.textContent=error.message;else toast(error.message);button.disabled=false;button.textContent='Generar y abrir reporte';}
      };
    }catch(error){if(document.getElementById('annual-report-loading')===loading)openModal('<div class="modal-h"><h3>No se pudo preparar el reporte</h3></div><div class="modal-b"><p>'+esc(error.message)+'</p></div><div class="modal-f"><button class="btn" onclick="closeModal()">Cerrar</button></div>');}
  };
  window.carbonReportHistory=async()=>{
    openModal('<div class="modal-h"><h3>Reportes anuales guardados</h3></div><div class="modal-b" id="annual-history">Cargando…</div><div class="modal-f"><button class="btn" onclick="closeModal()">Cerrar</button></div>');
    try{
      const rows=await api('/api/reports');const el=document.getElementById('annual-history');if(!el)return;
      el.innerHTML=rows.length?'<p>Las versiones conservan los resultados y textos del momento de generación. Para actualizar un informe, generá una nueva versión.</p><table><thead><tr><th>Año</th><th>Perímetro</th><th>Generado</th><th>Estado</th><th></th></tr></thead><tbody>'+rows.map(r=>'<tr><td>'+r.year+'</td><td>'+esc(r.scope)+'</td><td>'+esc(new Date(r.created).toLocaleString('es-AR'))+'</td><td>'+(r.approvedAt?'Aprobado internamente':'Para revisión')+'</td><td><a class="btn sm" href="/reports/'+encodeURIComponent(r.id)+'" target="_blank" rel="noopener">Abrir / PDF</a></td></tr>').join('')+'</tbody></table>':'<p>Todavía no hay reportes guardados. Generá el primero desde Reportes.</p>';
    }catch(error){const el=document.getElementById('annual-history');if(el)el.textContent=error.message;}
  };
})();
