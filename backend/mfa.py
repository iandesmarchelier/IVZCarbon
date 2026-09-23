"""Second login step: a one-time code sent by email through Resend."""
import os
import re
import secrets
import httpx
from fastapi import HTTPException
from .security import token_hash

PRODUCT = 'IVZ Carbon'
CODE_TTL = 600
MAX_ATTEMPTS = 5
MAX_SENDS = 3
RESEND_AFTER = 60
EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def new_code():
    return f'{secrets.randbelow(1_000_000):06d}'


def code_digest(challenge, code):
    # Keyed by the challenge cookie, which is stored only hashed: a leaked row can't be brute-forced.
    return token_hash(challenge + ':' + code)


def clean_code(code):
    return ''.join(c for c in code if c.isdigit())


def clean_email(value):
    email = value.strip()
    if len(email) > 254 or not EMAIL.match(email):
        raise HTTPException(400, 'Ingresá un correo válido.')
    return email


def mask(email):
    name, _, domain = email.partition('@')
    return name[:2] + '•••@' + domain


def send_code(email, code):
    key, sender = os.getenv('RESEND_API_KEY'), os.getenv('MAIL_FROM')
    if not key or not sender:
        if os.getenv('VERCEL') or os.getenv('CARBON_ENV') == 'production':
            raise HTTPException(503, 'El envío del código por correo no está configurado. Avisá al administrador.')
        print(f'[{PRODUCT}] Código de verificación para {email}: {code}', flush=True)
        return
    minutes = CODE_TTL // 60
    text = (f'Tu código para ingresar a {PRODUCT} es {code}.\n\nVence en {minutes} minutos. '
            'Si no intentaste ingresar, cambiá tu contraseña y avisá al administrador.')
    html = (f'<p>Tu código para ingresar a <b>{PRODUCT}</b> es:</p>'
            f'<p style="font:600 28px ui-monospace,monospace;letter-spacing:6px">{code}</p>'
            f'<p>Vence en {minutes} minutos. Si no intentaste ingresar, cambiá tu contraseña y avisá al administrador.</p>')
    try:
        r = httpx.post('https://api.resend.com/emails', timeout=10, headers={'Authorization': 'Bearer ' + key},
                       json={'from': sender, 'to': [email], 'subject': f'{code} es tu código de acceso a {PRODUCT}',
                             'text': text, 'html': html})
        r.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(502, 'No pudimos enviar el código por correo. Probá de nuevo en unos minutos.')
