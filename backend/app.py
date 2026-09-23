import csv
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
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from .invoice_parser import parse_document
from . import inventory, matching
from .security import hash_password, verify_password, token_hash
from .storage import db, initialize, decode, database_url, event

ROOT = Path(__file__).resolve().parent.parent
DUMMY = hash_password('not-a-real-account')


@asynccontextmanager
async def lifespan(app):
    initialize()
    with db() as s:
        matching.enable(s)
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


def bootstrap_account():
    """Provision the initial accounts from secret hashes; never reset an existing user."""
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
    token = secrets.token_urlsafe(32)
    with db() as s:
        s.execute('DELETE FROM carbon_sessions WHERE expires<?', (now,))
        s.execute('INSERT INTO carbon_sessions (token,account,expires) VALUES (?,?,?)', (token_hash(token), user['id'], now+28800))
        s.execute('DELETE FROM carbon_login_limits WHERE username=?', (name,))
        event(s, user['id'], 'login', 0, {})
    response.set_cookie('ivz_carbon_session', token, httponly=True, samesite='strict',
                        secure=os.getenv('CARBON_ENV') == 'production' or bool(os.getenv('VERCEL')), max_age=28800)
    return {'company': user['company']}


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


@app.get('/api/state')
def get_state(request: Request):
    """The whole inventory in one response. The screen uses the paged endpoints below instead."""
    return inventory.load(account(request)['id'])


@app.get('/api/state/catalogue')
def get_catalogue(request: Request):
    return inventory.load_catalogue(account(request)['id'])


@app.get('/api/state/rows')
def get_rows(request: Request, kind: Literal['REC', 'MOV'], offset: int = 0, limit: int = 2000):
    return inventory.load_page(account(request)['id'], kind, offset, limit)


class Upload(BaseModel):
    batch: str = Field(min_length=8, max_length=64)
    part: int = Field(ge=0, le=10000)
    changes: dict


@app.post('/api/state/upload')
def upload_changes(body: Upload, request: Request):
    return inventory.upload(account(request)['id'], body.batch, body.part, body.changes)


class ChangeSet(BaseModel):
    revision: int = Field(ge=0)
    catalogue: dict | None = None
    changes: dict = {}
    order: dict = {}
    batch: str | None = Field(default=None, min_length=8, max_length=64)
    parts: int = Field(default=0, ge=0, le=10000)


@app.post('/api/state/changes')
def save_changes(body: ChangeSet, request: Request):
    return inventory.save(account(request)['id'], body.revision, catalogue=body.catalogue, changes=body.changes,
                          order=body.order, batch=body.batch, parts=body.parts)


class Save(BaseModel):
    revision: int = Field(ge=0)
    state: dict


@app.put('/api/state')
def put_state(body: Save, request: Request):
    return inventory.save(account(request)['id'], body.revision, full=body.state)


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
    inventory.save(user, 0, 'initialize_'+body.mode, full=state)
    return inventory.load(user)


@app.get('/api/summary')
def summary(request: Request, year: int | None = None, site: str | None = None):
    return inventory.summary(account_flexible(request)['id'], year, site)


@app.get('/api/closures')
def list_closures(request: Request):
    return inventory.closures(account(request)['id'])


class CloseYear(BaseModel):
    year: int = Field(ge=1900, le=2200)


def acting_as(user):
    return 'Administrador de Invenzis' if user.get('impersonated_by') else user['username']


@app.post('/api/closures')
def close_year(body: CloseYear, request: Request):
    user = account(request)
    return inventory.close_year(user['id'], body.year, acting_as(user))


class ReopenYear(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@app.post('/api/closures/{year}/reopen')
def reopen_year(year: int, body: ReopenYear, request: Request):
    user = account(request)
    # Only an administrator, working inside the client's account, can reopen a closed year.
    if not user.get('impersonated_by'):
        raise HTTPException(403, 'Solo un administrador puede reabrir un año cerrado.')
    return inventory.reopen_year(user['id'], year, acting_as(user), body.reason.strip())


class MatchItem(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    scope: Literal[1, 2, 3] | None = None
    cat: int | None = Field(default=None, ge=1, le=15)
    sub: str | None = Field(default=None, max_length=20)
    unit: str | None = Field(default=None, max_length=40)
    supplier: str | None = Field(default=None, max_length=200)


class MatchRequest(BaseModel):
    items: list[MatchItem] = Field(min_length=1, max_length=5000)


@app.post('/api/factors/match')
def match_factors(body: MatchRequest, request: Request):
    """Library factor most similar to each description (pg_trgm), for the screen or an ERP integration."""
    return matching.match(account_flexible(request)['id'], [item.model_dump() for item in body.items])


@app.get('/api/factors/terms')
def library_terms(request: Request):
    account(request)
    return matching.LIBRARY_TERMS


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
    state = inventory.load(account_flexible(request)['id'])['state']
    if not state:
        return []
    return [{'id': s['id'], 'name': s['name'], 'country': s.get('country', ''), 'cc': s.get('cc', '')} for s in state['SITES']]


@app.get('/api/link/periods')
def link_periods(request: Request):
    state = inventory.load(account_flexible(request)['id'])['state']
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


# Downloads are streamed: a whole inventory can exceed Vercel's 4.5 MB limit for regular responses.
def joined(parts, size=65536):
    """Comma-join JSON parts, yielding ~64 KB chunks."""
    buffer, length, first = [], 0, True
    for part in parts:
        buffer.append(part if first else ', ' + part)
        first, length = False, length + len(part)
        if length > size:
            yield ''.join(buffer)
            buffer, length = [], 0
    if buffer:
        yield ''.join(buffer)


@app.get('/api/export')
def export(request: Request):
    user = account(request)['id']

    def chunks():
        with inventory.snapshot(user) as (head, rows):
            if not head:
                yield json.dumps({'revision': 0, 'state': None, 'updated': None})
                return
            state = head.pop('state')
            yield json.dumps(head, ensure_ascii=False)[:-1] + ', "state": ' + json.dumps(state, ensure_ascii=False)[:-1]
            for i, kind in enumerate(inventory.ROWS):
                yield (', ' if state or i else '') + json.dumps(kind) + ': ['
                yield from joined(json.dumps(item, ensure_ascii=False) for item in rows(kind))
                yield ']'
            yield '}}'
    return StreamingResponse(chunks(), media_type='application/json', headers={'Content-Disposition': 'attachment; filename="ivz-carbon-backup.json"'})


@app.get('/api/inventory.csv')
def inventory_csv(request: Request):
    user = account(request)['id']
    fields = ['rid', 'p', 'site', 'scope', 'cat', 'factor', 'qty', 'unit', 'kg', 'bio', 'source']

    def chunks():
        out = io.StringIO(newline='')
        writer = csv.writer(out)
        writer.writerow(fields)
        yield '\ufeff'
        with inventory.snapshot(user) as (head, rows):
            for row in rows('REC') if head else ():
                values = [row.get(f, '') for f in fields]
                writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for v in values])
                if out.tell() > 65536:
                    yield out.getvalue()
                    out.seek(0)
                    out.truncate()
        yield out.getvalue()
    return StreamingResponse(chunks(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="inventario-carbon.csv"'})


@app.post('/api/parse-document')
async def parse_document_endpoint(request: Request, kind: Literal['elec', 'gas', 'waste'] = Form(...), file: UploadFile = File(...)):
    account(request)
    data = await file.read()
    return parse_document(data, file.filename or '', kind)


@app.get('/api/admin/accounts')
def admin_list_accounts(request: Request):
    require_admin(request)
    with db() as s:
        rows = s.execute('SELECT a.id,a.username,a.company,a.role,a.active,a.created,st.updated,'
                         '(SELECT COUNT(*) FROM carbon_records r WHERE r.account=a.id) AS records FROM carbon_accounts a '
                         'LEFT JOIN carbon_states st ON st.account=a.id ORDER BY a.created DESC, a.username').fetchall()
    return [dict(r) for r in rows]


class AdminAccountCreate(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    company: str = Field(min_length=1, max_length=200)


@app.post('/api/admin/accounts')
def admin_create_account(body: AdminAccountCreate, request: Request):
    admin = require_admin(request)
    name = body.username.strip().lower()
    company = body.company.strip()
    password = secrets.token_urlsafe(12)
    account_id = str(uuid.uuid4())
    with db() as s:
        if s.execute('SELECT id FROM carbon_accounts WHERE username=?', (name,)).fetchone():
            raise HTTPException(409, 'Ya existe una cuenta con ese usuario.')
        s.execute('INSERT INTO carbon_accounts (id,username,company,password,role,active,created) VALUES (?,?,?,?,?,?,?)',
                  (account_id, name, company, hash_password(password), 'client', True, datetime.now(timezone.utc).isoformat()))
        event(s, admin['id'], 'admin_create_account', 0, {'target': account_id, 'username': name, 'company': company})
    return {'id': account_id, 'username': name, 'company': company, 'password': password}


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
    response.set_cookie('ivz_carbon_session', return_token, httponly=True, samesite='strict', secure=secure, max_age=28800)
    response.delete_cookie('ivz_carbon_admin_return')
    return {'ok': True}


@app.get('/health')
def health():
    with db() as s:
        s.execute('SELECT 1')
        similarity = matching.engine(s)
    return {'status': 'ok', 'database': 'postgresql' if database_url() else 'sqlite-local', 'similarity': similarity}


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


@app.get('/globe.js')
def globe_js():
    return FileResponse(ROOT / 'static/globe.js', media_type='text/javascript', headers={'Cache-Control': 'no-store, max-age=0'})


@app.get('/world.js')
def world_js():
    # Static country outlines; the page requests it with a version query, so it can be cached.
    return FileResponse(ROOT / 'static/world.js', media_type='text/javascript', headers={'Cache-Control': 'public, max-age=86400'})
