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

    m = _search(r'\bmanifiesto\b[^\d\n]{0,20}(\d{3,10})', flat)
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


def parse_document(data, filename, kind):
    if kind not in PARSERS:
        raise ValueError('kind inválido')

    text, method = extract_text(data, filename)

    if method == 'ocr-unavailable':
        return {'ok': False, 'method': method,
                'message': 'Este servidor no tiene instalado el motor de OCR (Tesseract), así que no se pudo leer automáticamente. Completá los datos a mano.',
                'fields': {}, 'confidence': {}}
    if not text.strip():
        return {'ok': False, 'method': method or 'vacio',
                'message': 'No se pudo extraer texto del archivo. Probá con otra foto o completá los datos a mano.',
                'fields': {}, 'confidence': {}}

    fields, confidence = PARSERS[kind](text)
    found_any = any(v not in (None, '') for v in fields.values())
    source = 'el texto del PDF' if method == 'pdf-text' else 'OCR'
    if found_any:
        message = f'Datos leídos automáticamente con {source}. Revisalos antes de confirmar.'
    else:
        message = f'Se leyó el documento con {source} pero no se reconocieron los campos esperados. Completá los datos a mano.'
    return {'ok': found_any, 'method': method, 'message': message, 'fields': fields, 'confidence': confidence}
