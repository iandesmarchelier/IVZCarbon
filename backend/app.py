import csv
import hmac
import io
import json
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import mfa
from .invoice_parser import parse_document
from .metrics import normalize, compute
from .security import hash_password, verify_password, token_hash
from .storage import db, initialize, decode, database_url

ROOT = Path(__file__).resolve().parent.parent
DUMMY = hash_password('not-a-real-account')
# How long a login (password + email code) lasts. 30 days for now; set SESSION_HOURS=8 once licenses are sold.
SESSION_TTL = int(float(os.getenv('SESSION_HOURS', '720')) * 3600)


@asynccontextmanager
async def lifespan(app):
    initialize()
    bootstrap_account()
    yield


app = FastAPI(title='IVZ Carbon', version='1.0.0', lifespan=lifespan)


@app.middleware('http')
async def guard(request: Request, call_next):
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        if request.headers.get('x-ivz-carbon') != '1':
            return Response('Solicitud no autorizada', status_code=403)
        body = await request.body()
        limit = 4_000_000 if os.getenv('VERCEL') else 32_000_000
        if len(body) > limit:
            return Response(f'Máximo {limit // 1_000_000} MB por guardado', status_code=413)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Cache-Control'] = 'no-store'
    return response


def account(request):
    with db() as s:
        row = s.execute('SELECT a.id,a.username,a.company,a.role,a.active,t.impersonated_by FROM carbon_accounts a JOIN carbon_sessions t ON t.account=a.id WHERE t.token=? AND t.expires>?',
                        (token_hash(request.cookies.get('ivz_carbon_session', '')), time.time())).fetchone()
    if not row or not row['active']:
        raise HTTPException(401, 'Ingresá a tu cuenta de IVZ Carbon.')
    return dict(row)


def require_admin(request):
    user = account(request)
    if user['role'] != 'admin':
        raise HTTPException(403, 'Necesitás permisos de administrador.')
    return user


def account_by_token(request):
    """Server-to-server auth for read-only cross-app endpoints (e.g. IVZ Sustainability Hub)."""
    header = request.headers.get('authorization', '')
    token = header[7:] if header.lower().startswith('bearer ') else ''
    if not token:
        return None
    with db() as s:
        row = s.execute('SELECT a.id,a.username,a.company FROM carbon_accounts a JOIN carbon_api_tokens t ON t.account=a.id WHERE t.token_hash=?',
                        (token_hash(token),)).fetchone()
        if row:
            s.execute('UPDATE carbon_api_tokens SET last_used=? WHERE token_hash=?', (datetime.now(timezone.utc).isoformat(), token_hash(token)))
    return dict(row) if row else None


def account_flexible(request):
    return account_by_token(request) or account(request)


def event(s, user, action, revision, details):
    s.execute('INSERT INTO carbon_events VALUES (?,?,?,?,?,?)',
              (str(uuid.uuid4()), user, action, revision, datetime.now(timezone.utc).isoformat(), s.json(details)))


def bootstrap_account():
    """Provision the initial accounts from secrets; never reset an existing user's password or email."""
    now = datetime.now(timezone.utc).isoformat()
    digest = os.getenv('CARBON_BOOTSTRAP_PASSWORD_HASH')
    if digest:
        with db() as s:
            s.execute("INSERT INTO carbon_accounts (id,username,company,password,role,active,created) VALUES (?,?,?,?,?,?,?) ON CONFLICT(username) DO NOTHING",
                      (str(uuid.uuid4()), 'demo', 'IVZ Carbon', digest, 'client', True, now))
    admin_digest = os.getenv('CARBON_ADMIN_PASSWORD_HASH')
    if admin_digest:
        with db() as s:
            s.execute("INSERT INTO carbon_accounts (id,username,company,password,role,active,created) VALUES (?,?,?,?,?,?,?) ON CONFLICT(username) DO NOTHING",
                      (str(uuid.uuid4()), 'admin', 'Administración IVZ Carbon', admin_digest, 'admin', True, now))
    # Every account logs in with an email code, so the bootstrap accounts need an address before their first login.
    for username, var in (('demo', 'CARBON_BOOTSTRAP_EMAIL'), ('admin', 'CARBON_ADMIN_EMAIL')):
        email = os.getenv(var, '').strip()
        if email:
            with db() as s:
                s.execute("UPDATE carbon_accounts SET email=? WHERE username=? AND (email IS NULL OR email='')", (email, username))


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=256)


@app.post('/api/login')
def login(body: Login, response: Response):
    name, now = body.username.strip().lower(), time.time()
    with db() as s:
        limits = s.execute('INSERT INTO carbon_login_limits VALUES (?,1,?) ON CONFLICT(username) DO UPDATE SET '
            'attempts=CASE WHEN carbon_login_limits.reset_at<? THEN 1 ELSE carbon_login_limits.attempts+1 END, '
            'reset_at=CASE WHEN carbon_login_limits.reset_at<? THEN ? ELSE carbon_login_limits.reset_at END RETURNING attempts',
            (name, now+900, now, now, now+900)).fetchone()
        user = s.execute('SELECT * FROM carbon_accounts WHERE username=?', (name,)).fetchone()
    if limits['attempts'] > 10:
        raise HTTPException(429, 'Demasiados intentos. Esperá 15 minutos.')
    valid = verify_password(body.password, user['password'] if user else DUMMY)
    if not user or not valid:
        raise HTTPException(401, 'Usuario o contraseña incorrectos.')
    if not user['active']:
        raise HTTPException(403, 'Esta cuenta fue desactivada.')
    if not user['email']:
        raise HTTPException(403, 'Tu cuenta no tiene un correo para recibir el código de verificación. Pedile al administrador que lo cargue.')
    # The password alone never opens a session: it only starts an email code challenge.
    # carbon_login_limits is kept until the code is verified, so it also caps how many challenges an attacker gets.
    challenge, code = secrets.token_urlsafe(32), mfa.new_code()
    with db() as s:
        s.execute('DELETE FROM carbon_login_challenges WHERE expires<? OR account=?', (now, user['id']))
        s.execute('INSERT INTO carbon_login_challenges (token,account,code,expires,sent_at) VALUES (?,?,?,?,?)',
                  (token_hash(challenge), user['id'], mfa.code_digest(challenge, code), now+mfa.CODE_TTL, now))
    mfa.send_code(user['email'], code)
    response.set_cookie('ivz_carbon_mfa', challenge, httponly=True, samesite='strict', secure=secure_cookies(), max_age=mfa.CODE_TTL)
    return {'mfa': True, 'email': mfa.mask(user['email'])}


def secure_cookies():
    return os.getenv('CARBON_ENV') == 'production' or bool(os.getenv('VERCEL'))


CHALLENGE_GONE = 'El código venció o se usó demasiadas veces. Volvé a ingresar tu usuario y contraseña.'


class LoginCode(BaseModel):
    code: str = Field(min_length=1, max_length=20)


@app.post('/api/login/verify')
def login_verify(body: LoginCode, request: Request, response: Response):
    challenge, now = request.cookies.get('ivz_carbon_mfa', ''), time.time()
    with db() as s:
        # Count the attempt before comparing, atomically, so parallel guesses can't exceed the limit.
        row = s.execute('UPDATE carbon_login_challenges SET attempts=attempts+1 WHERE token=? AND expires>? RETURNING account,code,attempts',
                        (token_hash(challenge), now)).fetchone()
    if not row or row['attempts'] > mfa.MAX_ATTEMPTS:
        raise HTTPException(410, CHALLENGE_GONE)
    if not hmac.compare_digest(row['code'], mfa.code_digest(challenge, mfa.clean_code(body.code))):
        left = mfa.MAX_ATTEMPTS - row['attempts']
        if not left:
            raise HTTPException(410, CHALLENGE_GONE)
        raise HTTPException(401, f'Código incorrecto. Te quedan {left} intento{"s" if left > 1 else ""}.')
    token = secrets.token_urlsafe(32)
    with db() as s:
        # Single use: a code that raced another request is rejected by the DELETE count.
        used = s.execute('DELETE FROM carbon_login_challenges WHERE token=?', (token_hash(challenge),)).rowcount
        user = s.execute('SELECT id,username,company,active FROM carbon_accounts WHERE id=?', (row['account'],)).fetchone()
        if used and user and user['active']:
            s.execute('DELETE FROM carbon_sessions WHERE expires<?', (now,))
            s.execute('INSERT INTO carbon_sessions (token,account,expires) VALUES (?,?,?)', (token_hash(token), user['id'], now+SESSION_TTL))
            s.execute('DELETE FROM carbon_login_limits WHERE username=?', (user['username'],))
            event(s, user['id'], 'login', 0, {})
    response.delete_cookie('ivz_carbon_mfa')
    if not used:
        raise HTTPException(410, CHALLENGE_GONE)
    if not user or not user['active']:
        raise HTTPException(403, 'Esta cuenta fue desactivada.')
    response.set_cookie('ivz_carbon_session', token, httponly=True, samesite='strict', secure=secure_cookies(), max_age=SESSION_TTL)
    return {'company': user['company']}


@app.post('/api/login/resend')
def login_resend(request: Request):
    challenge, now = request.cookies.get('ivz_carbon_mfa', ''), time.time()
    code = mfa.new_code()
    with db() as s:
        row = s.execute('SELECT c.sends,c.sent_at,a.email FROM carbon_login_challenges c JOIN carbon_accounts a ON a.id=c.account '
                        'WHERE c.token=? AND c.expires>? AND c.attempts<?', (token_hash(challenge), now, mfa.MAX_ATTEMPTS)).fetchone()
        if not row or not row['email']:
            raise HTTPException(410, CHALLENGE_GONE)
        if row['sends'] >= mfa.MAX_SENDS:
            raise HTTPException(429, 'Ya te reenviamos el código varias veces. Volvé a ingresar tu usuario y contraseña.')
        wait = int(row['sent_at'] + mfa.RESEND_AFTER - now) + 1
        # Conditional update so two quick clicks can't both send.
        sent = s.execute('UPDATE carbon_login_challenges SET code=?,sends=sends+1,sent_at=?,expires=? WHERE token=? AND sent_at<=?',
                         (mfa.code_digest(challenge, code), now, now+mfa.CODE_TTL, token_hash(challenge), now-mfa.RESEND_AFTER)).rowcount
    if not sent:
        raise HTTPException(429, f'Esperá {max(wait, 1)} segundos para pedir otro código.')
    mfa.send_code(row['email'], code)
    return {'email': mfa.mask(row['email'])}


@app.post('/api/logout')
def logout(request: Request, response: Response):
    with db() as s:
        s.execute('DELETE FROM carbon_sessions WHERE token=?', (token_hash(request.cookies.get('ivz_carbon_session', '')),))
    response.delete_cookie('ivz_carbon_session')
    return {'ok': True}


@app.get('/api/me')
def me(request: Request):
    user = account(request)
    return {'id': user['id'], 'username': user['username'], 'company': user['company'],
            'role': user['role'], 'impersonating': bool(user.get('impersonated_by'))}


def load(user):
    with db() as s:
        row = s.execute('SELECT revision,body,updated FROM carbon_states WHERE account=?', (user,)).fetchone()
    if not row:
        return {'revision': 0, 'state': None, 'updated': None}
    return {'revision': row['revision'], 'state': decode(row['body']), 'updated': row['updated']}


@app.get('/api/state')
def get_state(request: Request):
    return load(account(request)['id'])


class Save(BaseModel):
    revision: int = Field(ge=0)
    state: dict


def save(user, revision, raw, action='save'):
    try:
        state = normalize(raw)
        summary = compute(state)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    updated = datetime.now(timezone.utc).isoformat()
    with db() as s:
        if revision == 0:
            result = s.execute('INSERT INTO carbon_states VALUES (?,1,?,?) ON CONFLICT(account) DO NOTHING RETURNING revision',
                               (user, s.json(state), updated)).fetchone()
        else:
            result = s.execute('UPDATE carbon_states SET revision=revision+1,body=?,updated=? WHERE account=? AND revision=? RETURNING revision',
                               (s.json(state), updated, user, revision)).fetchone()
        if not result:
            raise HTTPException(409, 'Otra pestaña guardó cambios. Descargá tus cambios y recargá antes de continuar.')
        s.execute('DELETE FROM carbon_entities WHERE account=?', (user,))
        for kind in ('FACTORS', 'SITES', 'PROCS', 'LINES', 'MACH', 'BIZ', 'WASTECAT', 'MOV', 'RULES'):
            rows = [(user, kind, row['id'], s.json(row)) for row in state[kind]]
            sql = 'INSERT INTO carbon_entities VALUES (?,?,?,?)'
            with s.conn.cursor() if s.postgres else _cursor(s.conn) as cursor:
                cursor.executemany(sql.replace('?', '%s') if s.postgres else sql, rows)
        s.execute('DELETE FROM carbon_records WHERE account=?', (user,))
        rows = [(user, r['rid'], r['p'], r['site'], r['scope'], r['factor'], r['qty'], r['kg'], s.json(r)) for r in state['REC']]
        sql = 'INSERT INTO carbon_records VALUES (?,?,?,?,?,?,?,?,?)'
        with s.conn.cursor() if s.postgres else _cursor(s.conn) as cursor:
            cursor.executemany(sql.replace('?', '%s') if s.postgres else sql, rows)
        event(s, user, action, result['revision'], {'records': len(rows), 'kg': summary['kg']})
    return {'revision': result['revision'], 'updated': updated, 'summary': summary}


from contextlib import contextmanager


@contextmanager
def _cursor(conn):
    cursor = conn.cursor()
    try:
        yield cursor
    finally:
        cursor.close()


@app.put('/api/state')
def put_state(body: Save, request: Request):
    return save(account(request)['id'], body.revision, body.state)


class Start(BaseModel):
    mode: Literal['demo', 'empty']


@app.post('/api/initialize')
def start(body: Start, request: Request):
    user = account(request)['id']
    state = json.loads((ROOT / 'seed.json').read_text(encoding='utf8'))
    if body.mode == 'empty':
        for k in ('SITES', 'PROCS', 'LINES', 'MACH', 'BIZ', 'REC', 'MOV', 'WASTECAT', 'demoRecordIds', 'demoMovementIds'):
            state[k] = []
        state['PLACES'] = {}
        state['PERIODS'] = [datetime.now(timezone.utc).strftime('%Y-%m')]
    save(user, 0, state, 'initialize_'+body.mode)
    return load(user)


@app.get('/api/summary')
def summary(request: Request, year: int | None = None, site: str | None = None):
    state = load(account_flexible(request)['id'])['state']
    if not state:
        raise HTTPException(404, 'Inicializá el inventario.')
    return compute(state, year, site)


class TokenCreate(BaseModel):
    label: str = Field(min_length=1, max_length=100)


@app.post('/api/tokens')
def create_token(body: TokenCreate, request: Request):
    user = account(request)
    token = 'ivzc_' + secrets.token_urlsafe(32)
    with db() as s:
        s.execute('INSERT INTO carbon_api_tokens VALUES (?,?,?,?,?,?)',
                  (str(uuid.uuid4()), user['id'], body.label.strip(), token_hash(token), datetime.now(timezone.utc).isoformat(), None))
    return {'token': token}


@app.get('/api/tokens')
def list_tokens(request: Request):
    user = account(request)
    with db() as s:
        rows = s.execute('SELECT id,label,created,last_used FROM carbon_api_tokens WHERE account=? ORDER BY created DESC', (user['id'],)).fetchall()
    return [dict(r) for r in rows]


@app.delete('/api/tokens/{token_id}')
def revoke_token(token_id: str, request: Request):
    user = account(request)
    with db() as s:
        s.execute('DELETE FROM carbon_api_tokens WHERE id=? AND account=?', (token_id, user['id']))
    return {'ok': True}


@app.get('/api/link/sites')
def link_sites(request: Request):
    state = load(account_flexible(request)['id'])['state']
    if not state:
        return []
    return [{'id': s['id'], 'name': s['name'], 'country': s.get('country', ''), 'cc': s.get('cc', '')} for s in state['SITES']]


@app.get('/api/link/periods')
def link_periods(request: Request):
    state = load(account_flexible(request)['id'])['state']
    return (state or {}).get('PERIODS', [])


@app.get('/api/records')
def records(request: Request, year: int | None = None, site: str | None = None, scope: int | None = None, limit: int = 100, offset: int = 0):
    user = account(request)['id']
    clauses, args = ['account=?'], [user]
    if year:
        clauses.append('period LIKE ?'); args.append(f'{year}-%')
    if site:
        clauses.append('site=?'); args.append(site)
    if scope:
        clauses.append('scope=?'); args.append(scope)
    where = ' AND '.join(clauses)
    with db() as s:
        total = s.execute('SELECT COUNT(*) AS n FROM carbon_records WHERE '+where, tuple(args)).fetchone()['n']
        rows = s.execute('SELECT body FROM carbon_records WHERE '+where+' ORDER BY period,id LIMIT ? OFFSET ?', tuple(args+[max(1,min(limit,1000)),max(0,offset)])).fetchall()
    return {'total': total, 'items': [decode(r['body']) for r in rows]}


@app.get('/api/audit')
def audit(request: Request):
    with db() as s:
        rows = s.execute('SELECT action,revision,created,details FROM carbon_events WHERE account=? ORDER BY created DESC LIMIT 100', (account(request)['id'],)).fetchall()
    return [dict(r, details=decode(r['details'])) for r in rows]


@app.get('/api/export')
def export(request: Request):
    state = load(account(request)['id'])
    return Response(json.dumps(state, ensure_ascii=False), media_type='application/json', headers={'Content-Disposition': 'attachment; filename="ivz-carbon-backup.json"'})


@app.get('/api/inventory.csv')
def inventory(request: Request):
    state = load(account(request)['id'])['state']
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    fields = ['rid', 'p', 'site', 'scope', 'cat', 'factor', 'qty', 'unit', 'kg', 'bio', 'source']
    writer.writerow(fields)
    for row in (state or {}).get('REC', []):
        values = [row.get(f, '') for f in fields]
        writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for v in values])
    return Response('\ufeff'+out.getvalue(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="inventario-carbon.csv"'})


@app.post('/api/parse-document')
async def parse_document_endpoint(request: Request, kind: Literal['elec', 'gas', 'waste'] = Form(...), file: UploadFile = File(...)):
    account(request)
    data = await file.read()
    return parse_document(data, file.filename or '', kind)


@app.get('/api/admin/accounts')
def admin_list_accounts(request: Request):
    require_admin(request)
    with db() as s:
        rows = s.execute('SELECT a.id,a.username,a.email,a.company,a.role,a.active,a.created,st.updated FROM carbon_accounts a '
                         'LEFT JOIN carbon_states st ON st.account=a.id ORDER BY a.created DESC, a.username').fetchall()
    return [dict(r) for r in rows]


class AdminAccountCreate(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    company: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=1, max_length=254)


@app.post('/api/admin/accounts')
def admin_create_account(body: AdminAccountCreate, request: Request):
    admin = require_admin(request)
    name = body.username.strip().lower()
    company = body.company.strip()
    email = mfa.clean_email(body.email)
    password = secrets.token_urlsafe(12)
    account_id = str(uuid.uuid4())
    with db() as s:
        if s.execute('SELECT id FROM carbon_accounts WHERE username=?', (name,)).fetchone():
            raise HTTPException(409, 'Ya existe una cuenta con ese usuario.')
        s.execute('INSERT INTO carbon_accounts (id,username,company,password,role,active,created,email) VALUES (?,?,?,?,?,?,?,?)',
                  (account_id, name, company, hash_password(password), 'client', True, datetime.now(timezone.utc).isoformat(), email))
        event(s, admin['id'], 'admin_create_account', 0, {'target': account_id, 'username': name, 'company': company, 'email': email})
    return {'id': account_id, 'username': name, 'company': company, 'email': email, 'password': password}


class AdminSetEmail(BaseModel):
    email: str = Field(min_length=1, max_length=254)


@app.put('/api/admin/accounts/{account_id}/email')
def admin_set_email(account_id: str, body: AdminSetEmail, request: Request):
    admin = require_admin(request)
    email = mfa.clean_email(body.email)
    with db() as s:
        target = s.execute('SELECT id,username FROM carbon_accounts WHERE id=?', (account_id,)).fetchone()
        if not target:
            raise HTTPException(404, 'Cuenta no encontrada.')
        s.execute('UPDATE carbon_accounts SET email=? WHERE id=?', (email, account_id))
        event(s, admin['id'], 'admin_set_email', 0, {'target': account_id, 'username': target['username'], 'email': email})
    return {'ok': True}


@app.post('/api/admin/accounts/{account_id}/reset-password')
def admin_reset_password(account_id: str, request: Request):
    admin = require_admin(request)
    password = secrets.token_urlsafe(12)
    with db() as s:
        target = s.execute('SELECT id,username FROM carbon_accounts WHERE id=?', (account_id,)).fetchone()
        if not target:
            raise HTTPException(404, 'Cuenta no encontrada.')
        s.execute('UPDATE carbon_accounts SET password=? WHERE id=?', (hash_password(password), account_id))
        s.execute('DELETE FROM carbon_sessions WHERE account=?', (account_id,))
        event(s, admin['id'], 'admin_reset_password', 0, {'target': account_id, 'username': target['username']})
    return {'password': password}


class AdminSetActive(BaseModel):
    active: bool


@app.put('/api/admin/accounts/{account_id}/active')
def admin_set_active(account_id: str, body: AdminSetActive, request: Request):
    admin = require_admin(request)
    if account_id == admin['id'] and not body.active:
        raise HTTPException(400, 'No podés desactivar tu propia cuenta de administrador.')
    with db() as s:
        target = s.execute('SELECT id,username FROM carbon_accounts WHERE id=?', (account_id,)).fetchone()
        if not target:
            raise HTTPException(404, 'Cuenta no encontrada.')
        s.execute('UPDATE carbon_accounts SET active=? WHERE id=?', (body.active, account_id))
        if not body.active:
            s.execute('DELETE FROM carbon_sessions WHERE account=?', (account_id,))
        event(s, admin['id'], 'admin_set_active', 0, {'target': account_id, 'username': target['username'], 'active': body.active})
    return {'ok': True}


@app.post('/api/admin/accounts/{account_id}/impersonate')
def admin_impersonate(account_id: str, request: Request, response: Response):
    admin = require_admin(request)
    with db() as s:
        target = s.execute('SELECT id,username,active FROM carbon_accounts WHERE id=?', (account_id,)).fetchone()
        if not target or not target['active']:
            raise HTTPException(404, 'Cuenta no encontrada o inactiva.')
        token = secrets.token_urlsafe(32)
        s.execute('INSERT INTO carbon_sessions (token,account,expires,impersonated_by) VALUES (?,?,?,?)',
                  (token_hash(token), account_id, time.time()+28800, admin['id']))
        event(s, admin['id'], 'admin_impersonate', 0, {'target': account_id, 'username': target['username']})
    secure = os.getenv('CARBON_ENV') == 'production' or bool(os.getenv('VERCEL'))
    response.set_cookie('ivz_carbon_admin_return', request.cookies.get('ivz_carbon_session', ''),
                        httponly=True, samesite='strict', secure=secure, max_age=28800)
    response.set_cookie('ivz_carbon_session', token, httponly=True, samesite='strict', secure=secure, max_age=28800)
    return {'ok': True}


@app.post('/api/admin/return')
def admin_return(request: Request, response: Response):
    return_token = request.cookies.get('ivz_carbon_admin_return', '')
    if not return_token:
        raise HTTPException(400, 'No hay una sesión de administrador para volver.')
    with db() as s:
        row = s.execute('SELECT a.id,a.role FROM carbon_accounts a JOIN carbon_sessions t ON t.account=a.id WHERE t.token=? AND t.expires>?',
                        (token_hash(return_token), time.time())).fetchone()
    if not row or row['role'] != 'admin':
        response.delete_cookie('ivz_carbon_admin_return')
        raise HTTPException(401, 'La sesión de administrador venció. Volvé a ingresar.')
    secure = os.getenv('CARBON_ENV') == 'production' or bool(os.getenv('VERCEL'))
    response.set_cookie('ivz_carbon_session', return_token, httponly=True, samesite='strict', secure=secure, max_age=SESSION_TTL)
    response.delete_cookie('ivz_carbon_admin_return')
    return {'ok': True}


@app.get('/health')
def health():
    with db() as s:
        s.execute('SELECT 1')
    return {'status': 'ok', 'database': 'postgresql' if database_url() else 'sqlite-local'}


@app.get('/')
def index(request: Request):
    try:
        user = account(request)
    except HTTPException:
        return FileResponse(ROOT / 'static/login.html')
    if user['role'] == 'admin' and not user.get('impersonated_by'):
        return RedirectResponse('/admin')
    return FileResponse(ROOT / 'static/index.html')


@app.get('/admin')
def admin_page(request: Request):
    try:
        user = account(request)
    except HTTPException:
        return FileResponse(ROOT / 'static/login.html')
    if user['role'] != 'admin':
        raise HTTPException(403, 'Necesitás permisos de administrador.')
    return FileResponse(ROOT / 'static/admin.html')


@app.get('/bridge.js')
def bridge(request: Request):
    account(request)
    return FileResponse(ROOT / 'static/bridge.js', media_type='text/javascript')
