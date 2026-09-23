"""Automatic emission factor assignment by text similarity.

A purchase line, a trip or any other activity arrives with a description ("Acero aleado",
"Vuelo Buenos Aires → Neuquén"). Its factor is the library factor whose title or search terms
are most similar to that description, among the factors that fit the data (scope, category,
unit, and supplier for supplier-specific factors). The similarity is PostgreSQL's pg_trgm; the
same trigram arithmetic in Python serves SQLite and databases without the extension, so both
give identical results.

Score of a factor = best over its terms of (similarity + strict word similarity) / 2, where the
word similarity looks for the term inside the description or the description inside the term.
The plain similarity alone penalises any extra word; the word similarity alone cannot tell
"Acero aleado — barra" from "Acero aleado — factor del proveedor". Together they rank both cases.

Every automatic assignment waits for a person to approve it. Two factors with the same rounded
percentage are a tie: the screen warns about it and does not let it be approved in bulk.
"""
import re
import unicodedata
from functools import lru_cache
from fastapi import HTTPException
from .storage import db, decode

ALTERNATIVES = 4
MAX_ALTERNATIVES = 12  # a description like no factor at all ties them all at 0%; the record keeps a few

# Search terms of the IVZ reference library, so that the words people use ("vuelo", "hotel",
# "colectivo") find factors whose titles use others ("Avión", "Hotel — noche", "Traslado").
# Terms learnt from approvals and added by the organisation live in each factor's `alias` list.
LIBRARY_TERMS = {
    'FE-001': ['gas natural', 'gas de red', 'caldera', 'horno a gas'],
    'FE-002': ['gasoil movil', 'diesel vehiculos', 'combustible flota', 'tarjeta de combustible'],
    'FE-003': ['glp', 'gas licuado', 'autoelevador', 'garrafa'],
    'FE-004': ['refrigerante', 'r410a', 'recarga de gas', 'fuga de refrigerante'],
    'FE-005': ['biogas'],
    'FE-006': ['gasoil estacionario', 'grupo electrogeno', 'generador diesel'],
    'FE-010': ['electricidad argentina', 'energia electrica argentina', 'luz argentina'],
    'FE-011': ['electricidad uruguay', 'energia electrica uruguay', 'luz uruguay'],
    'FE-012': ['electricidad renovable', 'ppa', 'energia renovable', 'irec'],
    'FE-013': ['vapor', 'calor adquirido', 'agua caliente'],
    'FE-030': ['acero aleado', 'barra de acero', 'acero al carbono', 'acero laminado'],
    'FE-031': ['acero inoxidable', 'inox', 'barra inoxidable'],
    'FE-032': ['insumos', 'repuestos', 'consumibles', 'herramientas', 'rodamientos', 'insumos industriales'],
    'FE-033': ['servicios', 'consultoria', 'honorarios', 'servicios profesionales', 'asesoria'],
    'FE-040': ['camion', 'flete terrestre', 'transporte por carretera', 'transporte terrestre'],
    'FE-041': ['maritimo', 'barco', 'fluvial', 'barcaza', 'contenedor maritimo'],
    'FE-042': ['aereo de carga', 'carga aerea', 'flete aereo'],
    'FE-050': ['raee', 'residuos electronicos', 'chatarra electronica'],
    'FE-051': ['chatarra', 'viruta metalica', 'scrap'],
    'FE-052': ['residuo asimilable', 'residuos generales', 'basura', 'relleno sanitario'],
    'FE-053': ['aceite usado', 'residuo peligroso', 'lubricante usado'],
    'FE-054': ['incineracion', 'valorizacion energetica'],
    'FE-055': ['reciclaje', 'residuos reciclables', 'carton', 'plastico reciclable'],
    'FE-060': ['vuelo', 'pasaje aereo', 'avion', 'vuelo de cabotaje', 'vuelo regional'],
    'FE-061': ['auto de alquiler', 'rent a car', 'alquiler de auto', 'remis', 'taxi'],
    'FE-062': ['hotel', 'hoteleria', 'alojamiento', 'hospedaje', 'noche de hotel'],
    'FE-070': ['auto particular', 'traslado en auto', 'vehiculo propio'],
    'FE-071': ['colectivo', 'omnibus', 'bus', 'transporte publico', 'subte', 'tren'],
    'FE-072': ['home office argentina', 'teletrabajo argentina', 'trabajo remoto argentina'],
    'FE-073': ['home office uruguay', 'teletrabajo uruguay', 'trabajo remoto uruguay'],
}

UNIT_KEYS = {
    'kg': 'kg', 'kgs': 'kg', 'kilo': 'kg', 'kilos': 'kg', 'kilogramo': 'kg', 'kilogramos': 'kg',
    'l': 'l', 'lt': 'l', 'lts': 'l', 'litro': 'l', 'litros': 'l',
    'm3': 'm3', 'm³': 'm3', 'metro cubico': 'm3', 'metros cubicos': 'm3',
    'kwh': 'kwh', 'usd': 'usd', 'us': 'usd', 'u s': 'usd', 'dolares': 'usd', 'dolar': 'usd',
    'tkm': 'tkm', 't km': 'tkm', 'ton km': 'tkm', 'tonelada km': 'tkm',
    'km': 'km', 'kms': 'km', 'pkm': 'km', 'pasajero km': 'km',
    'noche': 'noche', 'noches': 'noche', 'jornada': 'jornada', 'jornadas': 'jornada', 'dias': 'jornada',
}


def text_key(value):
    """Lowercase ASCII words, as pg_trgm sees them: accents dropped, anything else a separator."""
    value = unicodedata.normalize('NFD', str(value or '').lower()).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]+', ' ', value).strip()


def unit_key(unit):
    raw = str(unit or '').lower().replace('³', '3').replace('·', ' ')
    key = text_key(raw)
    return UNIT_KEYS.get(key, key.replace(' ', ''))


# pg_trgm arithmetic: each word is padded with two leading blanks and one trailing blank.
@lru_cache(maxsize=20000)
def _word_trigrams(word):
    padded = '  ' + word + ' '
    return frozenset(padded[i:i + 3] for i in range(len(padded) - 2))


@lru_cache(maxsize=20000)
def _trigrams(text):
    return frozenset().union(*map(_word_trigrams, text.split()))


@lru_cache(maxsize=20000)
def _runs(text):
    """Trigram sets of every run of consecutive whole words."""
    words, result = text.split(), []
    for i in range(len(words)):
        run = frozenset()
        for word in words[i:]:
            run = run | _word_trigrams(word)
            result.append(run)
    return tuple(result)


def similarity(a, b):
    ta, tb = _trigrams(a), _trigrams(b)
    union = len(ta | tb)
    return len(ta & tb) / union if union else 0.0


def strict_word_similarity(a, b):
    """Greatest similarity between the trigrams of a and those of any run of whole words of b."""
    ta, best = _trigrams(a), 0.0
    for run in _runs(b):
        union = len(ta | run)
        best = max(best, len(ta & run) / union if union else 0.0)
    return best


def score(query, term):
    return (similarity(query, term) + max(strict_word_similarity(query, term), strict_word_similarity(term, query))) / 2


def percent(value):
    """Whole percentage, rounded half up from four decimals: PostgreSQL computes in float4 and Python in
    float64, and rounding at four decimals first makes both engines give the same number."""
    return (int(round(value * 10000)) + 50) // 100


SQL_SCORE = ('(similarity(q.x, t.x) + greatest(strict_word_similarity(q.x, t.x), strict_word_similarity(t.x, q.x))) / 2')

_engine = {}


def engine(s):
    """'pg_trgm' when PostgreSQL has the extension, 'python' otherwise."""
    if not s.postgres:
        return 'python'
    if 'pg' not in _engine:
        _engine['pg'] = bool(s.execute("SELECT 1 AS ok FROM pg_extension WHERE extname='pg_trgm'").fetchone())
    return 'pg_trgm' if _engine['pg'] else 'python'


def enable(s):
    """Install pg_trgm if the database allows it (it is a trusted extension since PostgreSQL 13)."""
    if not s.postgres:
        return
    s.execute('SAVEPOINT carbon_trgm')
    try:
        s.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
        s.execute('RELEASE SAVEPOINT carbon_trgm')
    except Exception:
        s.execute('ROLLBACK TO SAVEPOINT carbon_trgm')
    _engine.pop('pg', None)


def terms(factor):
    """Normalised search texts of a factor: its title, its library terms and its own terms."""
    found = [factor.get('act')] + LIBRARY_TERMS.get(factor.get('id'), []) + list(factor.get('alias') or [])
    result = []
    for t in map(text_key, found):
        if t and t not in result:
            result.append(t)
    return result


def eligible(factor, item):
    if item.get('scope') and factor.get('scope') != item['scope']:
        return False
    if item.get('cat') and factor.get('cat') != item['cat']:
        return False
    if item.get('sub') and (factor.get('sub') or ('elec' if factor.get('scope') == 2 else 'stat')) != item['sub']:
        return False
    if item.get('unit') and unit_key(factor.get('unit')) != unit_key(item['unit']):
        return False
    # A factor declared by one supplier only applies to that supplier's purchases.
    if factor.get('supplier') and factor['supplier'] != item.get('supplier'):
        return False
    return True


def _describe(item):
    parts = ['Alcance ' + str(item['scope'])] if item.get('scope') else []
    if item.get('cat'):
        parts.append('Cat. ' + str(item['cat']))
    if item.get('unit'):
        parts.append('unidad ' + str(item['unit']))
    return ', '.join(parts) or 'estos datos'


def _scores(s, queries, candidates):
    """{(query, factor id): score} for every query against every candidate factor."""
    pairs = [(f['id'], t) for f in candidates for t in terms(f)]
    if not pairs or not queries:
        return {}
    if engine(s) == 'pg_trgm':
        rows = s.execute(f'SELECT q.x AS q, t.f AS f, max({SQL_SCORE}) AS score '
                         'FROM unnest(?::text[]) AS q(x) CROSS JOIN unnest(?::text[], ?::text[]) AS t(f, x) GROUP BY q.x, t.f',
                         (list(queries), [f for f, _ in pairs], [t for _, t in pairs])).fetchall()
        return {(r['q'], r['f']): float(r['score']) for r in rows}
    result = {}
    for q in queries:
        for f, t in pairs:
            result[(q, f)] = max(result.get((q, f), 0.0), score(q, t))
    return result


def rank(s, factors, items):
    """Best factor for each item, with its percentage, the runners-up and whether it is a tie."""
    groups = {}
    for i, item in enumerate(items):
        key = (item.get('scope'), item.get('cat'), item.get('sub'), unit_key(item.get('unit')) if item.get('unit') else None, item.get('supplier'))
        groups.setdefault(key, []).append(i)
    results = [None] * len(items)
    for indexes in groups.values():
        sample = items[indexes[0]]
        candidates = [f for f in factors if eligible(f, sample)]
        if not candidates:
            for i in indexes:
                results[i] = {'factor': None, 'error': f'La biblioteca no tiene factores para {_describe(sample)}.'}
            continue
        queries = {text_key(items[i]['text']) for i in indexes} - {''}
        scores = _scores(s, queries, candidates)
        for i in indexes:
            q = text_key(items[i]['text'])
            ranked = sorted(((percent(scores.get((q, f['id']), 0.0)), f['id']) for f in candidates), key=lambda x: (-x[0], x[1]))
            best_pct, best = ranked[0]
            tied = [f for p, f in ranked[1:] if p == best_pct]
            alts = [{'factor': f, 'pct': p} for p, f in ranked[1:1 + min(max(ALTERNATIVES, len(tied)), MAX_ALTERNATIVES)] if p or f in tied]
            results[i] = {'factor': best, 'pct': best_pct, 'tie': bool(tied), 'tiedWith': tied[:MAX_ALTERNATIVES], 'alts': alts, 'text': items[i]['text']}
    return results


def match(user, items):
    with db() as s:
        row = s.execute('SELECT body FROM carbon_states WHERE account=?', (user,)).fetchone()
        if not row:
            raise HTTPException(404, 'Inicializá el inventario.')
        factors = decode(row['body']).get('FACTORS') or []
        results = rank(s, factors, items)
        used = engine(s)
    return {'engine': used, 'results': results}
