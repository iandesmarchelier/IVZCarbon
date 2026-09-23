"""Server-side calculation using the factor catalogue supplied by IVZ Carbon V3.5.

The catalogue is a prototype reference, not independently verified emission data.
"""
import copy
import math
import re

COLLECTIONS = ('FACTORS', 'SITES', 'PROCS', 'LINES', 'MACH', 'BIZ', 'REC', 'MOV', 'PERIODS', 'WASTECAT', 'RULES')


def number(value, name, maximum=1e18):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValueError(f'{name}: debe ser un número finito no negativo (máximo {maximum:g}).')
    return value


def unique(rows, key, label):
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get(key), str) or not row[key] or len(row[key]) > 200:
            raise ValueError(f'{label}: identificador inválido.')
        if row[key] in result:
            raise ValueError(f'{label}: identificador duplicado {row[key]}.')
        result[row[key]] = row
    return result


def normalize(raw):
    state = copy.deepcopy(raw)
    if state.get('schemaVersion') != 1:
        raise ValueError('Versión de datos no compatible.')
    for name in COLLECTIONS:
        if not isinstance(state.get(name), list) or len(state[name]) > 50000:
            raise ValueError(f'{name}: lista inválida o demasiado grande.')
    factors = unique(state['FACTORS'], 'id', 'Factores')
    sites = unique(state['SITES'], 'id', 'Sitios')
    for name in ('PROCS', 'LINES', 'MACH', 'BIZ', 'MOV', 'WASTECAT', 'RULES'):
        unique(state[name], 'id', name)
    for f in factors.values():
        number(f.get('v'), 'Factor')
        if f.get('scope') not in (1, 2, 3) or isinstance(f.get('scope'), bool):
            raise ValueError('Alcance de factor inválido.')
        if f['scope'] == 3 and (type(f.get('cat')) is not int or not 1 <= f['cat'] <= 15):
            raise ValueError('Categoría de alcance 3 inválida.')
        if not isinstance(f.get('unit'), str) or not f['unit']:
            raise ValueError('Unidad de factor requerida.')
        for k in ('bio', 'unc'):
            if f.get(k) is not None:
                number(f[k], k)
        alias = f.get('alias', [])
        if not isinstance(alias, list) or len(alias) > 500 or any(not isinstance(a, str) or len(a) > 500 for a in alias):
            raise ValueError(f"{f['id']}: términos de búsqueda inválidos.")
    for p in state['PERIODS']:
        if not isinstance(p, str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', p):
            raise ValueError('Período inválido: usá AAAA-MM.')
    for site in sites.values():
        if site.get('ef') and site['ef'] not in factors:
            raise ValueError('Factor eléctrico del sitio inexistente.')
    records = unique(state['REC'], 'rid', 'Registros')
    for r in records.values():
        f = factors.get(r.get('factor'))
        if not f:
            raise ValueError(f"{r['rid']}: factor inexistente.")
        if r.get('site') not in sites or r.get('p') not in state['PERIODS']:
            raise ValueError(f"{r['rid']}: sitio o período inexistente.")
        qty = number(r.get('qty'), 'Cantidad')
        if r.get('unit', f['unit']) != f['unit']:
            raise ValueError(f"{r['rid']}: la unidad no coincide con el factor.")
        r.update(kg=number(qty * f['v'], 'Emisiones', 1e30), scope=f['scope'], cat=f.get('cat'), unit=f['unit'])
        r['bio'] = number(qty * f.get('bio', 0), 'CO2 biogénico', 1e30)
        if f['scope'] != 3:
            r['sub'] = f.get('sub') or ('elec' if f['scope'] == 2 else 'stat')
        if r.get('pair') and r['pair'] not in records:
            raise ValueError(f"{r['rid']}: registro vinculado inexistente.")
        if r.get('fm') is not None:
            check_match(r)
    settings = state.setdefault('settings', {})
    if not isinstance(settings, dict) or not isinstance(settings.get('uqOverrides', {}), dict):
        raise ValueError('Configuración de incertidumbre inválida.')
    for override in settings.get('uqOverrides', {}).values():
        if not isinstance(override, dict):
            raise ValueError('Incertidumbre inválida.')
        number(override.get('pct'), 'Incertidumbre', 1000)
    if not isinstance(state.get('PLACES'), dict) or not isinstance(state.get('counters'), dict):
        raise ValueError('Ubicaciones o contadores inválidos.')
    for k in ('RID', 'MOVN', 'MDN', 'UID'):
        if type(state['counters'].get(k)) is not int or not 0 <= state['counters'][k] <= 10**12:
            raise ValueError('Contadores inválidos.')
    for key in ('demoRecordIds', 'demoMovementIds'):
        if not isinstance(state.get(key), list) or any(not isinstance(x, str) for x in state[key]):
            raise ValueError('Referencias demo inválidas.')
    return state


def check_match(r):
    """Automatic factor assignment kept on a record: what was searched, how similar, and its approval."""
    fm = r['fm']
    if not isinstance(fm, dict) or not isinstance(fm.get('ok', False), bool) or not isinstance(fm.get('tie', False), bool):
        raise ValueError(f"{r['rid']}: asignación automática de factor inválida.")
    if fm.get('pct') is not None:
        number(fm['pct'], 'Similitud', 100)
    if not isinstance(fm.get('cands', []), list) or len(fm.get('cands', [])) > 30:
        raise ValueError(f"{r['rid']}: alternativas de factor inválidas.")


def pending_match(r):
    return isinstance(r.get('fm'), dict) and not r['fm'].get('ok')


def activity_uncertainty(r):
    typ, src = r.get('type'), r.get('src', 'manual')
    if typ == 'elec':
        return 2 if src == 'iot' else 3 if src == 'invoice' else 15
    if typ == 'td':
        return 15
    if typ == 'purchase' and r.get('po'):
        return {'spend': 35, 'proveedor': 10}.get(r['po'].get('method'), 5)
    if typ == 'transport':
        return 8
    if typ in ('waste', 'travel', 'fugitive'):
        return 10
    if typ == 'commute':
        return 25
    if typ == 'fuel':
        return 3 if src == 'invoice' else 5
    return 20


def compute(state, year=None, site=None):
    factors = {f['id']: f for f in state['FACTORS']}
    sites = {s['id']: s for s in state['SITES']}
    records = [r for r in state['REC'] if (not year or r['p'][:4] == str(year)) and (not site or r['site'] == site)]
    scopes, cats = {1: 0., 2: 0., 3: 0.}, {}
    total = bio = location = sum_sq = covered = 0.
    overrides = state.get('settings', {}).get('uqOverrides', {})
    for r in records:
        f = factors[r['factor']]
        kg = r['qty'] * f['v']
        scopes[f['scope']] += kg
        total += kg
        bio += r['qty'] * f.get('bio', 0)
        if f['scope'] == 3:
            cats[f['cat']] = cats.get(f['cat'], 0) + kg
        if f['scope'] == 2 and f.get('sub', 'elec') == 'elec':
            st = sites[r['site']]
            lf = factors.get(st.get('ef')) or factors.get('FE-011' if st.get('cc') == 'UY' else 'FE-010')
            if not lf:
                raise ValueError('Falta factor location-based.')
            location += r['qty'] * lf['v']
        else:
            location += kg
        if f.get('unc') is not None:
            act = overrides.get(r.get('source', '').split(' · ')[0], {}).get('pct', activity_uncertainty(r))
            sum_sq += kg**2 * (act**2 + f['unc']**2) / 10000
            covered += kg
    return {'count': len(records), 'kg': total, 'tCO2e': total / 1000, 'scopes': scopes, 'categories': cats,
            'biogenicKg': bio, 'locationKg': location, 'scope2LocationKg': location - scopes[1] - scopes[3],
            'uncertainty': {'pct': math.sqrt(sum_sq) / total * 100 if total else None,
                            'coverage': covered / total * 100 if total else 0, 'kg': total},
            'method': 'Cantidad × factor; CO2 biogénico separado. Incertidumbre: propagación independiente por registro.',
            'catalogueNotice': 'Factores heredados del prototipo V3.5; requieren validación antes de uso oficial.'}
