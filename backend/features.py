"""Per-account switches an administrator sets: which sections a client sees and which integrations it can use.

Stored in carbon_accounts.settings as {"sections": {key: bool}, "integrations": {key: bool}}; a key that was
never set takes its default below. The Panel ejecutivo is always visible and is not listed.
"""
import json

from fastapi import HTTPException

# (key, label, menu group, visible by default)
SECTIONS = [
    ('corporativo', 'Inventario corporativo', 'Análisis', True),
    ('trazabilidad', 'Trazabilidad', 'Análisis', True),
    ('incertidumbre', 'Calidad e incertidumbre', 'Análisis', True),
    ('planta', 'Ubicaciones y facturas', 'Operación', True),
    ('energia', 'Consumo energético por equipo', 'Operación', False),
    ('movilidad', 'Movilidad', 'Operación', True),
    ('asignacion', 'Asignación de factores', 'Operación', True),
    ('transacciones', 'Transacciones y clasificación', 'Operación', True),
    ('integraciones', 'Integraciones ERP', 'Datos', True),
    ('datacenter', 'Carga por Excel', 'Datos', True),
    ('factores', 'Factores de emisión', 'Datos', True),
    ('reportes', 'Reportes', 'Datos', True),
]

# (key, label, native): the connector cards in Integraciones. Only the Hub link is a real integration today.
INTEGRATIONS = [
    ('hub', 'IVZ Sustainability Hub', True),
    ('sap', 'SAP ERP — Compras y Finanzas', False),
    ('tms', 'TMS — gestión de transporte', False),
    ('medidores', 'Medidores de energía', False),
    ('viajes', 'Plataforma de viajes corporativa', False),
    ('residuos', 'Portal del operador de residuos', False),
    ('movilidad', 'Encuesta de movilidad (RRHH)', False),
    ('gmao', 'GMAO / service técnico', False),
    ('excel', 'Excel / CSV', False),
]

DEFAULTS = {'sections': {k: on for k, _, _, on in SECTIONS}, 'integrations': {k: True for k, _, _ in INTEGRATIONS}}


def parse(raw):
    try:
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        data = {}
    return {kind: {k: bool((data.get(kind) or {}).get(k, on)) for k, on in keys.items()} for kind, keys in DEFAULTS.items()}


def of(s, account_id):
    row = s.execute('SELECT settings FROM carbon_accounts WHERE id=?', (account_id,)).fetchone()
    return parse(row['settings'] if row else None)


def update(s, account_id, changes):
    """Apply {sections: {...}, integrations: {...}} over the stored switches; unknown keys are rejected."""
    current = of(s, account_id)
    for kind, values in changes.items():
        if kind not in DEFAULTS or not isinstance(values, dict):
            raise HTTPException(422, 'Configuración inválida.')
        for key, on in values.items():
            if key not in DEFAULTS[kind] or not isinstance(on, bool):
                raise HTTPException(422, f'Opción desconocida: {key}.')
            current[kind][key] = on
    s.execute('UPDATE carbon_accounts SET settings=? WHERE id=?', (json.dumps(current), account_id))
    return current


def catalogue(current):
    return {'sections': [{'key': k, 'label': label, 'group': group, 'default': on, 'on': current['sections'][k]}
                         for k, label, group, on in SECTIONS],
            'integrations': [{'key': k, 'label': label, 'native': native, 'on': current['integrations'][k]}
                             for k, label, native in INTEGRATIONS]}


def require(s, account_id, kind, key, message):
    if not of(s, account_id)[kind][key]:
        raise HTTPException(403, message)
