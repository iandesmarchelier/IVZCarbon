"""Annual organization reports: tenant-scoped, immutable calculated snapshots."""
from datetime import datetime, timezone
from html import escape
from collections import defaultdict
from typing import Literal
import uuid
from fastapi import HTTPException
from pydantic import BaseModel, Field, ConfigDict, field_validator
from . import inventory
from .metrics import normalize, compute, pending_match
from .storage import db, decode, event

CATEGORIES = ['Bienes y servicios adquiridos', 'Bienes de capital', 'Combustibles y energía', 'Transporte aguas arriba', 'Residuos de operaciones', 'Viajes de negocios', 'Desplazamiento de empleados', 'Activos arrendados aguas arriba', 'Transporte aguas abajo', 'Procesamiento de productos vendidos', 'Uso de productos vendidos', 'Fin de vida de productos vendidos', 'Activos arrendados aguas abajo', 'Franquicias', 'Inversiones']
ISO = ['Emisiones directas', 'Energía importada', 'Transporte', 'Productos utilizados por la organización', 'Uso de productos de la organización', 'Otras fuentes indirectas']
FIELDS = {
    'activity':'Actividad y descripción de la organización', 'purpose':'Objetivo y destinatarios',
    'boundary':'Límites y entidades incluidas', 'exclusions':'Exclusiones y justificación',
    'significance':'Criterios de significancia de emisiones indirectas', 'basePolicy':'Año base y política de recálculo',
    'methodology':'Metodología y supuestos', 'gwp':'Fuente y versión de PCG utilizados',
    'quality':'Calidad de datos y tratamiento de faltantes', 'energy':'Evidencias y criterios de energía adquirida',
    'gases':'Desagregación por gas y limitaciones', 'removals':'Remociones y método de cuantificación',
    'actions':'Acciones de reducción y responsables', 'verification':'Verificación independiente y referencia al dictamen',
    'responsible':'Responsable del inventario', 'reviewer':'Revisión y aprobación interna',
}

class ReportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    year: int = Field(ge=1900, le=2200)
    site: str | None = Field(default=None, max_length=200)
    consolidation: Literal['Control operacional', 'Control financiero', 'Participación accionaria'] = 'Control operacional'
    notes: dict[str, str] = Field(default_factory=dict, max_length=16)

    @field_validator('notes')
    @classmethod
    def clean_notes(cls, notes):
        if any(k not in FIELDS or len(v) > 6000 for k,v in notes.items()):
            raise ValueError('Campos del reporte inválidos o demasiado extensos.')
        return {k:v.strip() for k,v in notes.items()}

def history(user):
    with db(user) as s:
        rows = s.execute('SELECT id,year,created,body FROM carbon_reports WHERE account=? ORDER BY created DESC LIMIT 100', (user,)).fetchall()
    return [dict(id=r['id'], year=r['year'], created=r['created'], scope=decode(r['body'])['scope'],
                 site=decode(r['body']).get('site'), approvedAt=decode(r['body']).get('approvedAt')) for r in rows]

def get(user, report_id):
    with db(user) as s:
        row = s.execute('SELECT body FROM carbon_reports WHERE account=? AND id=?', (user, report_id)).fetchone()
    if not row:
        raise HTTPException(404, 'Reporte no encontrado.')
    return decode(row['body'])

def approve(user, report_id):
    with db(user['id']) as s:
        row=s.execute('SELECT body FROM carbon_reports WHERE account=? AND id=?'+(' FOR UPDATE' if s.postgres else ''),(user['id'],report_id)).fetchone()
        if not row: raise HTTPException(404,'Reporte no encontrado.')
        report=decode(row['body'])
        if report.get('approvedAt'): return report
        missing=[label for key,label in FIELDS.items() if key!='actions' and not report['notes'].get(key,'').strip()]
        if report['demo'] or report['pending'] or missing:
            raise HTTPException(422,'Antes de aprobar, generá una nueva versión sin datos demo ni asignaciones pendientes y completá los campos descriptivos (podés justificar los que no aplican). Pendientes: '+', '.join(missing))
        report.update(approvedAt=datetime.now(timezone.utc).isoformat(),approvedBy=user['username'])
        s.execute('UPDATE carbon_reports SET body=? WHERE account=? AND id=?',(s.json(report),user['id'],report_id))
        event(s,user['id'],'approve_report',report['revision'],{'report':report_id},user['username'])
    return report

def create(user, request):
    if any(k not in FIELDS or len(v) > 6000 for k,v in request.notes.items()):
        raise HTTPException(422, 'Campos del reporte inválidos o demasiado extensos.')
    # Lock the same account row as inventory writes/closures for a coherent snapshot.
    with db(user['id']) as s:
        row, catalogue = inventory._catalogue(s, user['id'], lock=True)
        if not row:
            raise HTTPException(404, 'Inicializá el inventario antes de generar el reporte.')
        state = dict(catalogue, **{kind:[item for _,item in inventory._rows(s,user['id'],kind)] for kind in inventory.ROWS})
        closure = s.execute('SELECT * FROM carbon_closures WHERE account=? AND year=?', (user['id'],request.year)).fetchone()
        if closure:
            state.update(FACTORS=decode(closure['factors']), SITES=decode(closure['sites']))
        if request.site and not any(x['id']==request.site for x in state['SITES']):
            raise HTTPException(422, 'El sitio no pertenece al perímetro del año seleccionado.')
        # Only normalize the reporting year, as other years may use new catalogue entries.
        records = [r for r in state['REC'] if r['p'].startswith(str(request.year)) and (not request.site or r['site']==request.site)]
        if not records:
            raise HTTPException(422, 'No hay registros para ese año y perímetro. Cargá datos antes de generar el reporte.')
        ids = {r['rid'] for r in records}
        state['REC'] = [dict(r, pair=r.get('pair') if r.get('pair') in ids else None) for r in records]
        state = normalize(state)
        result = compute(state, request.year, request.site)
        uncertainty_basis = 'Factores seleccionados en el inventario'
        if closure and not request.site:
            # Closing freezes aggregate uncertainty too, including the settings in use then.
            result = decode(closure['results'])
            uncertainty_basis = 'Resultado consolidado conservado al cerrar el año'
        elif closure:
            # Legacy closures store no site-level uncertainty settings. Do not imply this
            # recalculated percentage is the one approved at closing.
            result['uncertainty'] = {'pct':None, 'coverage':None, 'kg':result['kg']}
            uncertainty_basis = 'No disponible por sitio en el cierre histórico'
        months = []
        for month in range(1,13):
            subset = dict(state, REC=[r for r in state['REC'] if r['p']==f'{request.year}-{month:02}'])
            m = compute(subset)
            months.append(dict(month=month, count=m['count'], scopes=m['scopes'], location=m['scope2LocationKg']))
        sites = []
        for site in state['SITES']:
            if not request.site or site['id']==request.site:
                summary=compute(state, request.year, site['id'])
                sites.append(dict(id=site['id'], name=site.get('name',site['id']), country=site.get('cc','No informado'), count=summary['count'], kg=summary['locationKg']))
        used = {r['factor'] for r in state['REC']}
        # Also preserve the grid factors used by the location calculation.
        for r in state['REC']:
            if r['scope']==2 and r.get('sub','elec')=='elec':
                site=next(x for x in state['SITES'] if x['id']==r['site'])
                used.add(site.get('ef') or ('FE-011' if site.get('cc')=='UY' else 'FE-010'))
        pending=sum(pending_match(r) for r in state['REC'])
        demo_ids=set(state.get('demoRecordIds',[]))
        demo=sum(r['rid'] in demo_ids for r in state['REC'])
        iso = defaultdict(float)
        for r in state['REC']:
            if r['scope']==1: category=1
            elif r['scope']==2: continue
            elif r['cat'] in (4,6,7,9): category=3
            elif r['cat'] in (1,2,3,5,8): category=4
            elif r['cat'] in (10,11,12,13): category=5
            else: category=6
            iso[category]+=r['kg']
        if any(r['scope']==2 for r in state['REC']):
            iso[2]=result['scope2LocationKg']
        report = dict(id=str(uuid.uuid4()), year=request.year, created=datetime.now(timezone.utc).isoformat(),
            company=user['company'], scope=sites[0]['name'] if request.site else 'Todos los sitios', site=request.site,
            revision=row['revision'], closedAt=closure['closed_at'] if closure else None,
            consolidation=request.consolidation, notes=request.notes, result=result, months=months, sites=sites,
            factors=[f for f in state['FACTORS'] if f['id'] in used], iso=dict(iso), pending=pending, demo=demo,
            generatedBy=user['username'], version=1, uncertaintyBasis=uncertainty_basis)
        s.execute('INSERT INTO carbon_reports (id,account,year,created,body) VALUES (?,?,?,?,?)',
            (report['id'],user['id'],request.year,report['created'],s.json(report)))
        event(s,user['id'],'generate_report',row['revision'],{'report':report['id'],'year':request.year,'site':request.site},user['username'])
    return report

def e(value): return escape(str(value), quote=True)
def num(value): return f'{value:,.2f}'.replace(',', '_').replace('.', ',').replace('_','.')
def tonnes(value): return num(value/1000)
def table(headers, rows):
    return '<table><thead><tr>'+''.join('<th>'+e(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+e(v)+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table>'
def note(r,key):
    return '<h3>'+e(FIELDS[key])+'</h3><p>'+e(r['notes'].get(key) or 'No informado. Completar en una nueva versión antes de declarar conformidad.').replace('\n','<br>')+'</p>'

def bars(labels, values, title, colors=None):
    maximum=max((v for v in values if v is not None),default=0) or 1
    rows=''
    for i,(label,value) in enumerate(zip(labels,values)):
        y=24+i*34
        color = colors[i] if colors else '#4B7F55'
        bar = '' if value is None else f'<rect x="265" y="{y}" width="{max(0,value/maximum*340):.2f}" height="19" rx="3" fill="{e(color)}"/>'
        rows+=f'<text x="0" y="{y+13}" font-size="12">{e(label[:36])}</text>{bar}<text x="620" y="{y+14}" font-size="12">{tonnes(value) if value is not None else "Sin datos"}</text>'
    return f'<figure><figcaption>{e(title)} · tCO₂e</figcaption><svg role="img" aria-label="{e(title)}" viewBox="0 0 720 {len(labels)*34+35}" xmlns="http://www.w3.org/2000/svg">{rows}</svg></figure>'

def render(r):
    res=r['result']; scopes={int(k):v for k,v in res['scopes'].items()}; cats={int(k):v for k,v in res['categories'].items()}
    total=res['locationKg']; s2=res['scope2LocationKg']; notes=r['notes']
    sections=[]
    def section(title,body): sections.append('<section><h2>'+title+'</h2>'+body+'</section>')
    warnings=[]
    if r['demo']: warnings.append(f"Contiene {r['demo']} registros de demostración.")
    if r['pending']: warnings.append(f"{r['pending']} asignaciones automáticas de factores pendientes de aprobación.")
    missing=[FIELDS[k] for k in FIELDS if k!='actions' and not notes.get(k,'').strip()]
    warnings.append('Los factores de emisión y su aplicabilidad deben validarse con sus fuentes antes de uso oficial.')
    section('01 Resumen ejecutivo',f'<p>Durante {r["year"]}, <strong>{e(r["company"])}</strong> registró <strong>{tonnes(total)} tCO₂e</strong> en {e(r["scope"])}, utilizando el método basado en ubicación para la energía adquirida. El cálculo comprende {res["count"]} registros de actividad.</p>'+
        '<div class="metrics">'+''.join(f'<div><small>{label}</small><strong>{tonnes(value)}</strong><span>tCO₂e</span></div>' for label,value in [('Total registrado',total),('Directas',scopes[1]),('Energía importada',s2),('Otras indirectas',scopes[3])])+'</div>'+
        '<p>El inventario representa las fuentes cargadas en el perímetro seleccionado. La ausencia de registros no demuestra ausencia de emisiones. No se descuentan créditos, emisiones evitadas ni remociones.</p>'+
        bars(['Directas','Energía importada','Otras indirectas'],[scopes[1],s2,scopes[3]],'Distribución del inventario', ['#2E573C','#0F6A6B','#4B7F55'])+
        '<h3>Estado de preparación</h3><ul>'+''.join('<li>'+e(w)+'</li>' for w in warnings)+'</ul>'+f'<p>{len(missing)} campos descriptivos sin completar. Documento generado para revisión interna; no acredita por sí mismo conformidad ni verificación independiente.</p>')
    section('02 Organización y límites',note(r,'activity')+note(r,'purpose')+f'<p>Período: 1 de enero a 31 de diciembre de {r["year"]}. Enfoque declarado: {e(r["consolidation"])}. El cálculo suma los registros incluidos; cualquier ponderación por participación debe estar aplicada y documentada en el inventario.</p>'+note(r,'boundary')+
        table(['Sitio','País','Registros','tCO₂e por ubicación'],[(s['name'],s['country'],s['count'],tonnes(s['kg'])) for s in r['sites']])+note(r,'exclusions')+note(r,'significance'))
    monthly=[m['scopes'].get(1,m['scopes'].get('1',0))+m['location']+m['scopes'].get(3,m['scopes'].get('3',0)) for m in r['months']]
    labels=['Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre']
    section('03 Resultados anuales y evolución',bars(labels,[value if r['months'][i]['count'] else None for i,value in enumerate(monthly)],'Evolución mensual del inventario registrado')+
        table(['Mes','Registros','tCO₂e','Cobertura'],[(labels[i],m['count'],tonnes(monthly[i]) if m['count'] else 'Sin datos','Con registros' if m['count'] else 'Sin registros') for i,m in enumerate(r['months'])])+
        '<p>Las barras sin actividad registrada no representan emisiones verificadas de cero. Los períodos con datos parciales requieren conciliación con la actividad de la empresa.</p>'+note(r,'basePolicy'))
    section('04 Emisiones por categorías ISO 14064-1',table(['Categoría','Emisiones registradas tCO₂e'],[(f'{i+1}. {name}',tonnes(r['iso'].get(i+1,r['iso'].get(str(i+1),0))) if i+1 in r['iso'] or str(i+1) in r['iso'] else 'Sin registros') for i,name in enumerate(ISO)])+
        '<p>Clasificación orientativa por fuente: transporte comprende categorías GHG 4, 6, 7 y 9; productos utilizados, 1, 2, 3, 5 y 8; productos de la organización, 10 a 13; otras indirectas, 14 y 15. Revisar los límites de arrendamientos, franquicias e inversiones y el análisis de significancia antes de declarar conformidad. Los totales de esta tabla reclasifican el inventario y no se suman a los alcances.</p>'+note(r,'gases')+
        f'<p>CO₂ biogénico registrado por separado: {tonnes(res["biogenicKg"])} tCO₂. Los factores agregados en CO₂e no permiten reconstruir emisiones por gas sin información adicional.</p>'+note(r,'removals'))
    section('05 Energía adquirida y cadena de valor',table(['Energía adquirida','tCO₂e'],[('Basada en ubicación',tonnes(s2)),('Con factores seleccionados en el inventario',tonnes(scopes[2]))])+
        '<p>El segundo resultado solo podrá identificarse como basado en mercado una vez comprobados los instrumentos, factores y criterios de calidad aplicables. Ambas cifras son alternativas y no se suman.</p>'+note(r,'energy')+
        table(['Categoría de alcance 3','tCO₂e','Estado'],[(f'{i+1}. {name}',tonnes(cats[i+1]) if i+1 in cats else 'No estimado','Con registros' if i+1 in cats else 'Evaluar aplicabilidad') for i,name in enumerate(CATEGORIES)]))
    section('06 Metodología y factores de emisión','<p>Las emisiones se recalculan en el servidor a partir de cantidad × factor en kgCO₂e por unidad y se convierten a toneladas dividiendo por 1.000. No se aplica nuevamente PCG a factores ya expresados en CO₂e. Los resultados y textos de esta versión quedan guardados al generar el reporte.</p>'+note(r,'methodology')+note(r,'gwp')+
        table(['Factor','Valor kgCO₂e por unidad','Fuente declarada'],[(f.get('name') or f.get('act') or f['id'],format(f['v'],'.10g')+' / '+f['unit'],str(f.get('source') or f.get('src') or 'No informada')+' · '+str(f.get('year') or f.get('rev') or f.get('ver') or 'Versión no informada')+' · '+f['id']) for f in r['factors']]))
    uq=res['uncertainty']
    coverage=num(uq['coverage'])+' %' if uq.get('coverage') is not None else 'No disponible'
    section('07 Calidad e incertidumbre',note(r,'quality')+f'<p>Base de evaluación: {e(r.get("uncertaintyBasis","Factores seleccionados en el inventario"))}. Incertidumbre por propagación independiente por registro: {num(uq["pct"])+" %" if uq["pct"] is not None else "No calculable"}. Cobertura de emisiones evaluadas: {coverage}. No se atribuye un nivel de confianza estadístico a este porcentaje. Revisar correlaciones entre factores compartidos y fuentes no cuantificadas.</p>'+f'<p>Asignaciones automáticas pendientes: {r["pending"]}. Datos de demostración: {r["demo"]} registros.</p>'+('<h3>Información pendiente</h3><ul>'+''.join('<li>'+e(x)+'</li>' for x in missing)+'</ul>' if missing else '<p>Los campos descriptivos fueron completados; su contenido requiere revisión técnica.</p>'))
    section('08 Gestión y verificación',note(r,'actions')+note(r,'responsible')+note(r,'reviewer')+note(r,'verification')+'<p>La revisión interna, el cierre del inventario y la generación del documento no equivalen a verificación independiente. Adjuntar el dictamen, alcance, criterios, materialidad y nivel de aseguramiento cuando exista. Las acciones propuestas no se descuentan del inventario bruto.</p>')
    section('09 Trazabilidad y referencias',table(['Identificación','Valor'],[('Reporte',r['id']),('Generado',r['created']),('Revisión de inventario',r['revision']),('Estado del año','Cerrado '+r['closedAt'] if r['closedAt'] else 'Abierto al generar'),('Preparado por',r['generatedBy'])])+
        '<p>Conservar el inventario detallado, comprobantes, biblioteca de factores, análisis de significancia y documentación de recálculo junto con este reporte. Las versiones anteriores permanecen disponibles en el historial.</p><p>Referencias: <a href="https://www.iso.org/standard/66453.html">ISO 14064-1:2018</a>; <a href="https://ghgprotocol.org/corporate-standard">GHG Protocol Corporate Standard</a>; <a href="https://ghgprotocol.org/scope-2-guidance">Scope 2 Guidance</a>; <a href="https://ghgprotocol.org/corporate-value-chain-scope-3-standard">Scope 3 Standard</a>. Evaluar guías sectoriales y Land Sector and Removals Standard para actividades y períodos aplicables desde 2027.</p>')
    style='''*{box-sizing:border-box}body{margin:0;background:#eaf0ed;color:#23392f;font:15px/1.6 Arial,sans-serif}.toolbar{position:sticky;top:0;padding:14px 5%;background:#173d30;color:white;display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;z-index:2}.toolbar button{background:white;color:#173d30;border:0;border-radius:7px;padding:12px 20px;font-weight:bold;cursor:pointer}main{max-width:1000px;margin:30px auto;background:white;padding:55px 65px;box-shadow:0 8px 40px #15362715}.cover{padding:30px 0 50px;border-bottom:2px solid #e2eae5}.brand{font-weight:bold;letter-spacing:2px;color:#427d63}.cover h1{font-size:46px;line-height:1.1;max-width:700px;color:#153e2d}.cover .year{font-size:68px;color:#427d63;font-weight:bold}.subtitle{font-size:20px}.tag{font-size:12px;letter-spacing:1px;text-transform:uppercase;color:#64766b}h2{font-size:25px;line-height:1.25;color:#173e2e;margin-top:0}h3{font-size:16px;margin-bottom:5px}section{padding:36px 0;border-bottom:1px solid #e2eae5}p,td{overflow-wrap:anywhere}table{border-collapse:collapse;width:100%;font-size:12px;margin:18px 0}th{background:#e9f1ec;text-align:left;color:#214433}td,th{padding:10px;border-bottom:1px solid #dce5df;vertical-align:top}tr:nth-child(even){background:#f7f9f7}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:24px 0}.metrics div{padding:18px;background:#f0f5f1;border-radius:8px}.metrics small,.metrics span{display:block;font-size:12px}.metrics strong{display:block;font-size:27px}.metrics span{color:#65786c}figure{margin:24px 0;break-inside:avoid}figcaption{font-weight:bold;margin:10px 0}svg{width:100%;height:auto;font-family:Arial;fill:#23392f}footer{padding-top:25px;font-size:12px;color:#65786c}a{color:#316b50}@page{size:A4;margin:16mm}@media print{body{background:white;font-size:10pt}.toolbar,#approval-error{display:none}main{margin:0;padding:0;max-width:none;box-shadow:none}.cover{min-height:220mm;break-after:page;border:0}.cover h1{font-size:40pt}.cover .year{font-size:65pt}section{padding:18px 0;break-before:page;border:0}h2,h3{break-after:avoid}thead{display:table-header-group}tr{break-inside:avoid}table{font-size:9pt}td,th{padding:7px}.metrics strong{font-size:20pt}*{-webkit-print-color-adjust:exact;print-color-adjust:exact}}@media(max-width:700px){main{margin:0;padding:25px}.cover h1{font-size:34px}.metrics{grid-template-columns:1fr 1fr}.toolbar{gap:10px;font-size:12px}table{font-size:11px}}'''
    status='Aprobado internamente' if r.get('approvedAt') else 'Versión para revisión'
    approval='' if r.get('approvedAt') else '<button id="approve">Aprobar versión</button>'
    script='''<script>const button=document.getElementById('approve');if(button)button.onclick=async()=>{button.disabled=true;try{const response=await fetch('/api/reports/'+location.pathname.split('/').pop()+'/approve',{method:'POST',headers:{'X-IVZ-Carbon':'1'}});if(!response.ok){const data=await response.json();throw Error(data.detail||'No se pudo aprobar');}location.reload();}catch(error){document.getElementById('approval-error').textContent=error.message;button.disabled=false;}};</script>'''
    return '<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+e(r['company'])+' · Reporte GEI '+str(r['year'])+'</title><style>'+style+'</style></head><body><div class="toolbar"><span>IVZ Carbon · Reporte anual guardado</span>'+approval+'<button onclick="window.print()">Imprimir / Guardar PDF</button></div><p id="approval-error" role="alert" style="max-width:900px;margin:auto;color:#a63832"></p><main><header class="cover"><div class="brand">IVZ CARBON</div><p class="tag">Inventario organizacional de gases de efecto invernadero</p><h1>Reporte anual de huella de carbono</h1><div class="year">'+str(r['year'])+'</div><p class="subtitle">'+e(r['company'])+'</p><p>'+e(r['scope'])+' · Estructura ISO 14064-1:2018</p><p class="tag">'+status+' · '+e(r['created'][:10])+'</p>'+('<p>Aprobación interna: '+e(r['approvedBy'])+' · '+e(r['approvedAt'][:10])+'</p>' if r.get('approvedAt') else '')+'</header>'+''.join(sections)+'<footer>IVZ Carbon · '+e(r['company'])+' · '+str(r['year'])+' · '+e(r['id'])+'</footer></main>'+script+'</body></html>'
