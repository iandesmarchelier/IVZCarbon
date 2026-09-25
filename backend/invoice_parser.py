"""Lectura real (sin IA) de facturas de electricidad, gas y manifiestos de residuos.

El texto se extrae del PDF (capa de texto embebida si existe; si no, OCR con
Tesseract sobre la página renderizada) o directo de una foto, y los campos se
leen con expresiones regulares específicas por proveedor. No hay ningún
modelo de lenguaje involucrado en este módulo.

Proveedores reconocidos hoy: Edenor y UTE (electricidad), Metrogas (gas).
Para el resto se aplica un parser genérico de menor confianza. El manifiesto
de residuos no tiene todavía un proveedor de referencia: es genérico desde
el inicio y sus campos siempre se marcan de confianza baja.
"""
import re
from functools import lru_cache
from io import BytesIO

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

try:
    import pytesseract
except ImportError:
    pytesseract = None

try:
    from PIL import Image
except ImportError:
    Image = None


def _clean_number(raw):
    """'16.814,84' (formato es-AR/UY: punto de miles, coma decimal) -> 16814.84"""
    if not raw:
        return None
    s = raw.strip().replace('.', '').replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return None


def _ddmmyyyy_to_period(s):
    m = re.match(r'(\d{2})/(\d{2})/(\d{4})', s)
    if not m:
        return None
    return f'{m.group(3)}-{m.group(2)}'


class OcrUnavailable(Exception):
    pass


@lru_cache(maxsize=1)
def ocr_engine():
    """'tesseract-spa' en el contenedor, 'tesseract' sin el idioma español, 'off' donde no hay Tesseract (Vercel)."""
    if pytesseract is None:
        return 'off'
    try:
        languages = pytesseract.get_languages(config='')
    except Exception:
        return 'off'
    return 'tesseract-spa' if 'spa' in languages else 'tesseract'


def _ocr_image(img):
    if pytesseract is None:
        raise OcrUnavailable()
    try:
        return pytesseract.image_to_string(img, lang='spa')
    except pytesseract.TesseractNotFoundError:
        raise OcrUnavailable()
    except Exception:
        try:
            return pytesseract.image_to_string(img)
        except pytesseract.TesseractNotFoundError:
            raise OcrUnavailable()


def extract_text(data, filename):
    """Devuelve (texto, metodo). metodo: 'pdf-text' | 'ocr' | 'ocr-unavailable' | 'vacio'."""
    name = (filename or '').lower()
    if name.endswith('.pdf'):
        if pdfplumber is None:
            return '', 'vacio'
        try:
            with pdfplumber.open(BytesIO(data)) as pdf:
                pages = pdf.pages[:5]
                parts = [t for t in (p.extract_text() or '' for p in pages) if t.strip()]
                if parts:
                    return '\n'.join(parts), 'pdf-text'
                if not pages:
                    return '', 'vacio'
                try:
                    image = pages[0].to_image(resolution=300).original
                except Exception:
                    return '', 'vacio'
        except Exception:
            return '', 'vacio'
        try:
            return _ocr_image(image), 'ocr'
        except OcrUnavailable:
            return '', 'ocr-unavailable'
    if name.endswith(('.jpg', '.jpeg', '.png')):
        if Image is None:
            return '', 'vacio'
        try:
            image = Image.open(BytesIO(data))
        except Exception:
            return '', 'vacio'
        try:
            return _ocr_image(image), 'ocr'
        except OcrUnavailable:
            return '', 'ocr-unavailable'
    return '', 'vacio'


def _search(pattern, text):
    return re.search(pattern, text, re.IGNORECASE)


def parse_electricity(text):
    flat = re.sub(r'\s+', ' ', text)
    fields = {'provider': '', 'period': None, 'qty': None, 'doc': ''}
    confidence = {}

    if _search(r'\bUTE\b', flat) or 'clearing de informes' in flat.lower():
        fields['provider'] = 'UTE'
        m = _search(r'Activa\s+[\d.,]+\s+[\d.,]+\s+([\d.,]+)\s+Regular', flat)
        if m:
            fields['qty'] = _clean_number(m.group(1)); confidence['qty'] = 'high'
        m = _search(r'(\d{2}/\d{2}/\d{4})\s+a\s+(\d{2}/\d{2}/\d{4})', flat)
        if m:
            fields['period'] = _ddmmyyyy_to_period(m.group(2)); confidence['period'] = 'high'
        m = _search(r'\b([A-Z]\s?\d{6,8})\s+\d{2}/\d{2}/\d{4}\s+\d{2}/\d{2}/\d{4}', flat)
        if m:
            fields['doc'] = re.sub(r'\s+', ' ', m.group(1)).strip(); confidence['doc'] = 'high'
    elif _search(r'edenor', flat):
        fields['provider'] = 'Edenor'
        m = _search(r'Total\s*Consumo\s*([\d.,]+)\s*kWh', flat)
        if m:
            fields['qty'] = _clean_number(m.group(1)); confidence['qty'] = 'high'
        m = _search(r'Per[ií]odo de consumo:?\s*\d{2}/\d{2}/\d{4}\s*AL\s*(\d{2}/\d{2}/\d{4})', flat)
        if m:
            fields['period'] = _ddmmyyyy_to_period(m.group(1)); confidence['period'] = 'high'
        m = _search(r'Liquidaci[oó]n de Servicio P[uú]blico N[°ºo]\.?\s*([\d-]+)', flat)
        if m:
            fields['doc'] = m.group(1); confidence['doc'] = 'high'
    else:
        m = _search(r'([\d.,]+)\s*kWh', flat)
        if m:
            fields['qty'] = _clean_number(m.group(1)); confidence['qty'] = 'low'
        m = _search(r'(\d{2}/\d{2}/\d{4})\D{1,15}(\d{2}/\d{2}/\d{4})', flat)
        if m:
            fields['period'] = _ddmmyyyy_to_period(m.group(2)); confidence['period'] = 'low'

    return fields, confidence


def parse_gas(text):
    flat = re.sub(r'\s+', ' ', text)
    fields = {'provider': '', 'period': None, 'qty': None, 'doc': ''}
    confidence = {}

    if _search(r'metrogas', flat):
        fields['provider'] = 'Metrogas'
        m = _search(r'Consumo total en m3:?\s*([\d.,]+)', flat)
        if not m:
            m = _search(r'Consumo a 9300\s*Kcal/m3:?\s*([\d.,]+)', flat)
        if m:
            fields['qty'] = _clean_number(m.group(1)); confidence['qty'] = 'high'
        m = _search(r'PER[IÍ]ODO DE LIQUIDACI[OÓ]N:?\s*\d{2}/\d{2}/\d{4}\s*A\s*(\d{2}/\d{2}/\d{4})', flat)
        if m:
            fields['period'] = _ddmmyyyy_to_period(m.group(1)); confidence['period'] = 'high'
        m = _search(r'([A-Z]-\d{4}-\d{7,9})', flat)
        if m:
            fields['doc'] = m.group(1); confidence['doc'] = 'high'
    else:
        m = _search(r'([\d.,]+)\s*m3', flat)
        if m:
            fields['qty'] = _clean_number(m.group(1)); confidence['qty'] = 'low'
        m = _search(r'(\d{2}/\d{2}/\d{4})\D{1,15}(\d{2}/\d{2}/\d{4})', flat)
        if m:
            fields['period'] = _ddmmyyyy_to_period(m.group(2)); confidence['period'] = 'low'

    return fields, confidence


def parse_waste_manifest(text):
    """Sin proveedor de referencia todavía: heurística genérica, siempre confianza baja."""
    flat = re.sub(r'\s+', ' ', text)
    fields = {'date': '', 'qty': None, 'operator': '', 'doc': '', 'treatment': ''}
    confidence = {}

    m = _search(r'(?:fecha de retiro|fecha de generaci[oó]n)\D{0,10}(\d{2}/\d{2}/\d{4})', flat)
    if m:
        d, mo, y = m.group(1).split('/')
        fields['date'] = f'{y}-{mo}-{d}'; confidence['date'] = 'low'

    m = _search(r'(?:peso neto|cantidad)\D{0,15}([\d.,]+)\s*kg', flat)
    if m:
        fields['qty'] = _clean_number(m.group(1)); confidence['qty'] = 'low'
    else:
        m = _search(r'([\d.,]+)\s*(?:ton(?:eladas)?|t\b)', flat)
        if m:
            v = _clean_number(m.group(1))
            fields['qty'] = v * 1000 if v is not None else None
            confidence['qty'] = 'low'

    m = _search(r'(?:transportista|operador)\b[:\s]{1,3}([^\n]{3,60})', text)
    if m:
        fields['operator'] = re.sub(r'\s+', ' ', m.group(1)).strip(' .,-')[:60]; confidence['operator'] = 'low'

    m = _search(r'\bmanifiesto\b[^\d\n]{0,45}(\d{3,10})', flat)
    if m:
        fields['doc'] = m.group(1); confidence['doc'] = 'low'

    treat_kw = {'reciclaje': ['recicl'], 'relleno': ['relleno sanitario', 'disposici[oó]n final', 'relleno'],
                'incineracion': ['incinera'], 'especial': ['tratamiento especial', 'residuo peligroso', 'peligros']}
    low = flat.lower()
    for key, kws in treat_kw.items():
        if any(re.search(kw, low) for kw in kws):
            fields['treatment'] = key; confidence['treatment'] = 'low'
            break

    return fields, confidence


PARSERS = {'elec': parse_electricity, 'gas': parse_gas, 'waste': parse_waste_manifest}

# Words that point to each kind of document, for the bulk upload (kind 'auto').
KIND_WORDS = {
    'elec': [r'\bkwh\b', r'energ[ií]a el[eé]ctrica', r'edenor', r'edesur', r'\bute\b', r'\bepec\b', r'edelap', r'\bedea\b',
             r'\bedes\b', r'\bedet\b', r'\beden\b', r'\bepe\b', r'potencia contratada', r'energ[ií]a activa'],
    'gas': [r'\bm3\b', r'm³', r'metrogas', r'naturgy', r'camuzzi', r'litoral gas', r'ecogas', r'gasnor', r'gasnea',
            r'montevideo gas', r'gas natural', r'kcal'],
    'waste': [r'manifiesto', r'residuos?', r'generador', r'transportista', r'operador', r'tratamiento', r'disposici[oó]n final'],
}


def detect_kind(text):
    """'elec', 'gas' or 'waste' for the document's text, or None when nothing points to any of them."""
    low = re.sub(r'\s+', ' ', text).lower()
    scores = {kind: sum(len(re.findall(w, low)) for w in words) for kind, words in KIND_WORDS.items()}
    # A waste manifest names itself; an energy bill does not mention "manifiesto".
    if re.search(r'manifiesto', low):
        scores['waste'] += 10
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else None


ACCOUNT_LABEL = (r'(?:n[°ºo]\.?\s*(?:de\s+)?)?(?:cliente|cuenta(?:\s+contrato)?|suministro|nis|medidor|contrato|'
                 r'instalaci[oó]n|punto de suministro|generador)')


def location_hints(text):
    """What can tell which site a document belongs to: supply/account numbers, addresses and the text itself."""
    flat = re.sub(r'\s+', ' ', text)
    numbers = []
    for m in re.finditer(ACCOUNT_LABEL + r'\s*(?:n[°ºo]\.?)?\s*[:#]?\s*([A-Z]{0,3}[\s-]?\d[\d\s./-]{3,22}\d)', flat, re.IGNORECASE):
        value = re.sub(r'\s+', '', m.group(1)).strip('.-/')
        if len(re.sub(r'\D', '', value)) >= 4 and value not in numbers:
            numbers.append(value)
    addresses = []
    for m in re.finditer(r'(?:domicilio|direcci[oó]n)(?:\s+(?:de\s+suministro|del\s+suministro|postal|del\s+servicio|de\s+retiro|del\s+generador))?\s*:?\s*([^\n]{6,90})', text, re.IGNORECASE):
        value = re.sub(r'\s+', ' ', m.group(1)).strip(' .,:-')
        if value and value not in addresses:
            addresses.append(value)
    return {'accounts': numbers[:8], 'addresses': addresses[:4], 'text': flat[:6000]}


def parse_document(data, filename, kind):
    if kind not in PARSERS and kind != 'auto':
        raise ValueError('kind inválido')

    text, method = extract_text(data, filename)
    detected = kind if kind != 'auto' else None

    if method == 'ocr-unavailable':
        return {'ok': False, 'method': method, 'kind': detected,
                'message': 'El documento no tiene texto (es una imagen o un escaneo) y este servidor no tiene OCR: completá los datos a mano.',
                'fields': {}, 'confidence': {}, 'hints': {}}
    if not text.strip():
        return {'ok': False, 'method': method or 'vacio', 'kind': detected,
                'message': 'No se pudo extraer texto del archivo. Probá con otra foto o completá los datos a mano.',
                'fields': {}, 'confidence': {}, 'hints': {}}

    hints = location_hints(text)
    if kind == 'auto':
        detected = detect_kind(text)
        if not detected:
            return {'ok': False, 'method': method, 'kind': None, 'hints': hints, 'fields': {}, 'confidence': {},
                    'message': 'No se reconoció si es una factura de electricidad, de gas o un manifiesto de residuos. Elegí el tipo a mano.'}

    fields, confidence = PARSERS[detected](text)
    found_any = any(v not in (None, '') for v in fields.values())
    source = 'el texto del PDF' if method == 'pdf-text' else 'OCR'
    if found_any:
        message = f'Datos leídos automáticamente con {source}. Revisalos antes de confirmar.'
    else:
        message = f'Se leyó el documento con {source} pero no se reconocieron los campos esperados. Completá los datos a mano.'
    return {'ok': found_any, 'method': method, 'kind': detected, 'message': message, 'fields': fields,
            'confidence': confidence, 'hints': hints}
