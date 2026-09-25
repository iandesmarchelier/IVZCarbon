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
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse, HTMLResponse
from pydantic import BaseModel, Field

from .invoice_parser import ocr_engine, parse_document
from . import documents, features, inventory, matching, reports, users
from .security import hash_password, verify_password, token_hash
from .storage import SYSTEM, db, initialize, decode, database_url, event, isolated

ROOT = Path(__file__).resolve().parent.parent
DUMMY = hash_password('not-a-real-account')


@asynccontextmanager
async def lifespan(app):
    initialize()
    with db(SYSTEM) as s:
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


IMPERSONATOR = 'Administrador de Invenzis'


def account(request):
    """The signed-in user: id and company are its account's (the tenant), username and access its own.

    role is 'admin' for Invenzis' own account. A viewer can only read: its other requests stop here.
    An administrator working inside a client's account has no user there and acts as its admin."""
    with db(SYSTEM) as s:
        row = s.execute('SELECT a.id,a.company,a.role,a.active,t.impersonated_by,u.id AS user_id,u.username,u.role AS access,'
                        'u.active AS user_active FROM carbon_sessions t JOIN carbon_accounts a ON a.id=t.account '
                        'LEFT JOIN carbon_users u ON u.id=t.user_id AND u.account=t.account WHERE t.token=? AND t.expires>?',
                        (token_hash(request.cookies.get('ivz_carbon_session', '')), time.time())).fetchone()
    if not row or not row['active']:
        raise HTTPException(401, 'Ingresá a tu cuenta de IVZ Carbon.')
    user = dict(row)
    if user['impersonated_by']:
        user.update(username=IMPERSONATOR, access='admin')
    elif not user['user_id'] or not user['user_active']:
        raise HTTPException(401, 'Ingresá a tu cuenta de IVZ Carbon.')
    if user['access'] == 'viewer' and request.method not in ('GET', 'HEAD'):
        raise HTTPException(403, 'Tu usuario es de solo lectura.')
    return user


def require_admin(request):
    user = account(request)
    if user['role'] != 'admin' or user['access'] != 'admin':
        raise HTTPException(403, 'Necesitás permisos de administrador.')
    return user


def company_admin(request):
    """A user who manages the other users of its company."""
    user = account(request)
    if user['access'] != 'admin':
        raise HTTPException(403, 'Solo un administrador de la empresa puede gestionar usuarios.')
    return user


def account_by_token(request):
    """Server-to-server auth for read-only cross-app endpoints (e.g. IVZ Sustainability Hub)."""
    header = request.headers.get('authorization', '')
    token = header[7:] if header.lower().startswith('bearer ') else ''
    if not token:
        return None
    with db(SYSTEM) as s:
        row = s.execute('SELECT a.id,a.username,a.company,a.settings FROM carbon_accounts a JOIN carbon_api_tokens t ON t.account=a.id WHERE t.token_hash=? AND a.active',
                        (token_hash(token),)).fetchone()
        if row and not features.parse(row['settings'])['integrations']['hub']:
            raise HTTPException(403, 'La integración con IVZ Sustainability Hub está desactivada para esta cuenta.')
        if row:
            s.execute('UPDATE carbon_api_tokens SET last_used=? WHERE token_hash=?', (datetime.now(timezone.utc).isoformat(), token_hash(token)))
    return {k: row[k] for k in ('id', 'username', 'company')} if row else None


def account_flexible(request):
    return account_by_token(request) or account(request)


def bootstrap_account():
    """Provision the initial accounts from secret hashes; never reset an existing user."""
    now = datetime.now(timezone.utc).isoformat()
    digest = os.getenv('CARBON_BOOTSTRAP_PASSWORD_HASH')
    for username, company, role, digest in (('demo', 'IVZ Carbon', 'client', digest),
                                            ('admin', 'Administración IVZ Carbon', 'admin', os.getenv('CARBON_ADMIN_PASSWORD_HASH'))):
        if digest:
            with db(SYSTEM) as s:
                if not s.execute('SELECT 1 FROM carbon_accounts WHERE username=?', (username,)).fetchone():
                    users.create_account(s, username, company, role, digest)


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=256)


@app.post('/api/login')
def login(body: Login, response: Response):
    name, now = body.username.strip().lower(), time.time()
    with db(SYSTEM) as s:
        limits = s.execute('INSERT INTO carbon_login_limits VALUES (?,1,?) ON CONFLICT(username) DO UPDATE SET '
            'attempts=CASE WHEN carbon_login_limits.reset_at<? THEN 1 ELSE carbon_login_limits.attempts+1 END, '
            'reset_at=CASE WHEN carbon_login_limits.reset_at<? THEN ? ELSE carbon_login_limits.reset_at END RETURNING attempts',
            (name, now+900, now, now, now+900)).fetchone()
        user = s.execute('SELECT u.id,u.account,u.username,u.password,u.active,a.active AS account_active,a.company FROM carbon_users u '
                         'JOIN carbon_accounts a ON a.id=u.account WHERE u.username=?', (name,)).fetchone()
    if limits['attempts'] > 10:
        raise HTTPException(429, 'Demasiados intentos. Esperá 15 minutos.')
    valid = verify_password(body.password, user['password'] if user else DUMMY)
    if not user or not valid:
        raise HTTPException(401, 'Usuario o contraseña incorrectos.')
    if not user['account_active']:
        raise HTTPException(403, 'Esta cuenta fue desactivada.')
    if not user['active']:
        raise HTTPException(403, 'Tu usuario fue desactivado. Pedile al administrador de tu empresa que lo reactive.')
    token = secrets.token_urlsafe(32)
    with db(SYSTEM) as s:
        s.execute('DELETE FROM carbon_sessions WHERE expires<?', (now,))
        s.execute('INSERT INTO carbon_sessions (token,account,expires,user_id) VALUES (?,?,?,?)', (token_hash(token), user['account'], now+28800, user['id']))
        s.execute('DELETE FROM carbon_login_limits WHERE username=?', (name,))
        event(s, user['account'], 'login', 0, {}, user['username'])
    response.set_cookie('ivz_carbon_session', token, httponly=True, samesite='strict',
                        secure=os.getenv('CARBON_ENV') == 'production' or bool(os.getenv('VERCEL')), max_age=28800)
    return {'company': user['company']}


@app.post('/api/logout')
def logout(request: Request, response: Response):
    with db(SYSTEM) as s:
        s.execute('DELETE FROM carbon_sessions WHERE token=?', (token_hash(request.cookies.get('ivz_carbon_session', '')),))
    response.delete_cookie('ivz_carbon_session')
    return {'ok': True}


@app.get('/api/me')
def me(request: Request):
    user = account(request)
    with db(user['id']) as s:
        switches = features.of(s, user['id'])
    return {'id': user['id'], 'username': user['username'], 'company': user['company'], 'role': user['role'],
            'access': user['access'], 'impersonating': bool(user.get('impersonated_by')), 'features': switches}


def section_user(request, section):
    """The signed-in account, provided the administrator left this section visible for it."""
    user = account(request)
    with db(user['id']) as s:
        features.require(s, user['id'], 'sections', section, 'Esta sección no está habilitada para tu cuenta.')
    return user


def hub_user(request):
    user = account(request)
    with db(user['id']) as s:
        features.require(s, user['id'], 'integrations', 'hub', 'La integración con IVZ Sustainability Hub está desactivada para esta cuenta.')
    return user


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
    user = account(request)
    return inventory.save(user['id'], body.revision, catalogue=body.catalogue, changes=body.changes,
                          order=body.order, batch=body.batch, parts=body.parts, by=acting_as(user))


class Save(BaseModel):
    revision: int = Field(ge=0)
    state: dict


@app.put('/api/state')
def put_state(body: Save, request: Request):
    user = account(request)
    return inventory.save(user['id'], body.revision, full=body.state, by=acting_as(user))


class Start(BaseModel):
    mode: Literal['demo', 'empty']


@app.post('/api/initialize')
def start(body: Start, request: Request):
    person = account(request)
    user = person['id']
    state = json.loads((ROOT / 'seed.json').read_text(encoding='utf8'))
    if body.mode == 'empty':
        for k in ('SITES', 'PROCS', 'LINES', 'MACH', 'BIZ', 'REC', 'MOV', 'WASTECAT', 'demoRecordIds', 'demoMovementIds'):
            state[k] = []
        state['PLACES'] = {}
        state['PERIODS'] = [datetime.now(timezone.utc).strftime('%Y-%m')]
    inventory.save(user, 0, 'initialize_'+body.mode, full=state, by=acting_as(person))
    return inventory.load(user)


@app.get('/api/summary')
def summary(request: Request, year: int | None = None, site: str | None = None):
    return inventory.summary(account_flexible(request)['id'], year, site)


@app.get('/api/closures')
def list_closures(request: Request):
    return inventory.closures(account(request)['id'])


@app.post('/api/reports')
def generate_report(body: reports.ReportRequest, request: Request):
    return reports.create(section_user(request, 'reportes'), body)


@app.get('/api/reports')
def report_history(request: Request):
    return reports.history(section_user(request, 'reportes')['id'])


@app.get('/api/reports/{report_id}')
def report_data(report_id: str, request: Request):
    return reports.get(section_user(request, 'reportes')['id'], report_id)


@app.post('/api/reports/{report_id}/approve')
def approve_report(report_id: str, request: Request):
    return reports.approve(section_user(request, 'reportes'), report_id)


@app.get('/reports/{report_id}', response_class=HTMLResponse)
def report_document(report_id: str, request: Request):
    return reports.render(reports.get(section_user(request, 'reportes')['id'], report_id))


@app.get('/reports.js')
def reports_js(request: Request):
    account(request)
    return FileResponse(ROOT / 'static/reports.js', media_type='text/javascript')


class CloseYear(BaseModel):
    year: int = Field(ge=1900, le=2200)


def acting_as(user):
    return user['username']  # IMPERSONATOR when an administrator works inside the account


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


def new_token(s, account_id, label):
    token = 'ivzc_' + secrets.token_urlsafe(32)
    s.execute('INSERT INTO carbon_api_tokens VALUES (?,?,?,?,?,?)',
              (str(uuid.uuid4()), account_id, label.strip(), token_hash(token), datetime.now(timezone.utc).isoformat(), None))
    return token


def tokens_of(s, account_id):
    return [dict(r) for r in s.execute('SELECT id,label,created,last_used FROM carbon_api_tokens WHERE account=? ORDER BY created DESC', (account_id,)).fetchall()]


@app.post('/api/tokens')
def create_token(body: TokenCreate, request: Request):
    user = hub_user(request)
    with db(user['id']) as s:
        return {'token': new_token(s, user['id'], body.label)}


@app.get('/api/tokens')
def list_tokens(request: Request):
    user = hub_user(request)
    with db(user['id']) as s:
        return tokens_of(s, user['id'])


@app.delete('/api/tokens/{token_id}')
def revoke_token(token_id: str, request: Request):
    user = hub_user(request)
    with db(user['id']) as s:
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
    with db(user) as s:
        total = s.execute('SELECT COUNT(*) AS n FROM carbon_records WHERE '+where, tuple(args)).fetchone()['n']
        rows = s.execute('SELECT body FROM carbon_records WHERE '+where+' ORDER BY period,id LIMIT ? OFFSET ?', tuple(args+[max(1,min(limit,1000)),max(0,offset)])).fetchall()
    return {'total': total, 'items': [decode(r['body']) for r in rows]}


@app.get('/api/audit')
def audit(request: Request):
    user = account(request)['id']
    with db(user) as s:
        rows = s.execute('SELECT action,revision,created,details,actor FROM carbon_events WHERE account=? ORDER BY created DESC LIMIT 100', (user,)).fetchall()
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
async def parse_document_endpoint(request: Request, kind: Literal['elec', 'gas', 'waste', 'auto'] = Form(...), file: UploadFile = File(...)):
    user = account(request)
    data = await file.read()
    result = parse_document(data, file.filename or '', kind)
    # Kept so the record can open it later ("Ver documento"); linked when a record that names it is saved.
    result['document'] = documents.store(user['id'], file.filename or '', data)
    return result


@app.get('/api/documents/{doc_id}')
def get_document(doc_id: str, request: Request):
    return documents.open_document(account(request)['id'], doc_id)


@app.get('/api/admin/accounts')
def admin_list_accounts(request: Request):
    require_admin(request)
    with db(SYSTEM) as s:
        rows = s.execute('SELECT a.id,a.username,a.company,a.role,a.active,a.created,st.updated,'
                         '(SELECT COUNT(*) FROM carbon_users u WHERE u.account=a.id AND u.active) AS users,'
                         '(SELECT COUNT(*) FROM carbon_records r WHERE r.account=a.id) AS records FROM carbon_accounts a '
                         'LEFT JOIN carbon_states st ON st.account=a.id ORDER BY a.created DESC, a.username').fetchall()
    return [dict(r) for r in rows]


class AdminAccountCreate(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    company: str = Field(min_length=1, max_length=200)


@app.post('/api/admin/accounts')
def admin_create_account(body: AdminAccountCreate, request: Request):
    admin = require_admin(request)
    company = body.company.strip()
    with db(SYSTEM) as s:
        account_id, name, password = users.create_account(s, body.username, company)
        event(s, admin['id'], 'admin_create_account', 0, {'target': account_id, 'username': name, 'company': company}, admin['username'])
    return {'id': account_id, 'username': name, 'company': company, 'password': password}


@app.post('/api/admin/accounts/{account_id}/reset-password')
def admin_reset_password(account_id: str, request: Request):
    """The password of the account's first user (its id is the account's); other users have their own."""
    admin = require_admin(request)
    with db(SYSTEM) as s:
        username, password = users.reset_password(s, account_id, account_id)
        event(s, admin['id'], 'admin_reset_password', 0, {'target': account_id, 'username': username}, admin['username'])
    return {'password': password}


class AdminSetActive(BaseModel):
    active: bool


@app.put('/api/admin/accounts/{account_id}/active')
def admin_set_active(account_id: str, body: AdminSetActive, request: Request):
    admin = require_admin(request)
    if account_id == admin['id'] and not body.active:
        raise HTTPException(400, 'No podés desactivar tu propia cuenta de administrador.')
    with db(SYSTEM) as s:
        target = s.execute('SELECT id,username FROM carbon_accounts WHERE id=?', (account_id,)).fetchone()
        if not target:
            raise HTTPException(404, 'Cuenta no encontrada.')
        s.execute('UPDATE carbon_accounts SET active=? WHERE id=?', (body.active, account_id))
        if not body.active:
            s.execute('DELETE FROM carbon_sessions WHERE account=?', (account_id,))
        event(s, admin['id'], 'admin_set_active', 0, {'target': account_id, 'username': target['username'], 'active': body.active}, admin['username'])
    return {'ok': True}


def admin_target(s, account_id):
    target = s.execute('SELECT id,username,role FROM carbon_accounts WHERE id=?', (account_id,)).fetchone()
    if not target:
        raise HTTPException(404, 'Cuenta no encontrada.')
    return target


@app.get('/api/admin/accounts/{account_id}/settings')
def admin_get_settings(account_id: str, request: Request):
    require_admin(request)
    with db(SYSTEM) as s:
        admin_target(s, account_id)
        return {**features.catalogue(features.of(s, account_id)), 'tokens': tokens_of(s, account_id)}


class AdminSettings(BaseModel):
    sections: dict[str, bool] = {}
    integrations: dict[str, bool] = {}


@app.put('/api/admin/accounts/{account_id}/settings')
def admin_put_settings(account_id: str, body: AdminSettings, request: Request):
    admin = require_admin(request)
    with db(SYSTEM) as s:
        target = admin_target(s, account_id)
        current = features.update(s, account_id, body.model_dump())
        event(s, admin['id'], 'admin_settings', 0, {'target': account_id, 'username': target['username'], **body.model_dump()}, admin['username'])
    return features.catalogue(current)


@app.post('/api/admin/accounts/{account_id}/tokens')
def admin_create_token(account_id: str, body: TokenCreate, request: Request):
    admin = require_admin(request)
    with db(SYSTEM) as s:
        target = admin_target(s, account_id)
        features.require(s, account_id, 'integrations', 'hub', 'Activá primero la integración con IVZ Sustainability Hub.')
        token = new_token(s, account_id, body.label)
        event(s, admin['id'], 'admin_create_token', 0, {'target': account_id, 'username': target['username'], 'label': body.label.strip()}, admin['username'])
    return {'token': token}


@app.delete('/api/admin/accounts/{account_id}/tokens/{token_id}')
def admin_revoke_token(account_id: str, token_id: str, request: Request):
    admin = require_admin(request)
    with db(SYSTEM) as s:
        target = admin_target(s, account_id)
        s.execute('DELETE FROM carbon_api_tokens WHERE id=? AND account=?', (token_id, account_id))
        event(s, admin['id'], 'admin_revoke_token', 0, {'target': account_id, 'username': target['username']}, admin['username'])
    return {'ok': True}


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    role: Literal['admin', 'editor', 'viewer']


class UserChange(BaseModel):
    role: Literal['admin', 'editor', 'viewer'] | None = None
    active: bool | None = None


# Users of the signed-in company, managed by its own administrators (backend/users.py).
@app.get('/api/users')
def list_users(request: Request):
    user = company_admin(request)
    with db(user['id']) as s:
        return users.listing(s, user['id'])


@app.post('/api/users')
def create_user(body: UserCreate, request: Request):
    user = company_admin(request)
    with db(user['id']) as s:
        created, password = users.add(s, user['id'], body.username, body.role)
        event(s, user['id'], 'create_user', 0, {'username': created['username'], 'role': body.role}, acting_as(user))
    return {**created, 'password': password}


@app.put('/api/users/{user_id}')
def change_user(user_id: str, body: UserChange, request: Request):
    user = company_admin(request)
    with db(user['id']) as s:
        changed = users.update(s, user['id'], user_id, body.role, body.active)
        event(s, user['id'], 'change_user', 0, changed, acting_as(user))
    return changed


@app.post('/api/users/{user_id}/reset-password')
def reset_user_password(user_id: str, request: Request):
    user = company_admin(request)
    with db(user['id']) as s:
        username, password = users.reset_password(s, user['id'], user_id)
        event(s, user['id'], 'reset_password', 0, {'username': username}, acting_as(user))
    return {'password': password}


# The same, for Invenzis' administrator on any client.
@app.get('/api/admin/accounts/{account_id}/users')
def admin_list_users(account_id: str, request: Request):
    require_admin(request)
    with db(SYSTEM) as s:
        admin_target(s, account_id)
        return users.listing(s, account_id)


@app.post('/api/admin/accounts/{account_id}/users')
def admin_create_user(account_id: str, body: UserCreate, request: Request):
    admin = require_admin(request)
    with db(SYSTEM) as s:
        admin_target(s, account_id)
        created, password = users.add(s, account_id, body.username, body.role)
        event(s, admin['id'], 'admin_create_user', 0, {'target': account_id, 'username': created['username'], 'role': body.role}, admin['username'])
    return {**created, 'password': password}


@app.put('/api/admin/accounts/{account_id}/users/{user_id}')
def admin_change_user(account_id: str, user_id: str, body: UserChange, request: Request):
    admin = require_admin(request)
    with db(SYSTEM) as s:
        admin_target(s, account_id)
        changed = users.update(s, account_id, user_id, body.role, body.active)
        event(s, admin['id'], 'admin_change_user', 0, {'target': account_id, **changed}, admin['username'])
    return changed


@app.post('/api/admin/accounts/{account_id}/users/{user_id}/reset-password')
def admin_reset_user_password(account_id: str, user_id: str, request: Request):
    admin = require_admin(request)
    with db(SYSTEM) as s:
        admin_target(s, account_id)
        username, password = users.reset_password(s, account_id, user_id)
        event(s, admin['id'], 'admin_reset_password', 0, {'target': account_id, 'username': username}, admin['username'])
    return {'password': password}


@app.post('/api/admin/accounts/{account_id}/impersonate')
def admin_impersonate(account_id: str, request: Request, response: Response):
    admin = require_admin(request)
    with db(SYSTEM) as s:
        target = s.execute('SELECT id,username,active FROM carbon_accounts WHERE id=?', (account_id,)).fetchone()
        if not target or not target['active']:
            raise HTTPException(404, 'Cuenta no encontrada o inactiva.')
        token = secrets.token_urlsafe(32)
        s.execute('INSERT INTO carbon_sessions (token,account,expires,impersonated_by) VALUES (?,?,?,?)',
                  (token_hash(token), account_id, time.time()+28800, admin['id']))
        event(s, admin['id'], 'admin_impersonate', 0, {'target': account_id, 'username': target['username']}, admin['username'])
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
    with db(SYSTEM) as s:
        row = s.execute("SELECT a.id,a.role FROM carbon_accounts a JOIN carbon_sessions t ON t.account=a.id JOIN carbon_users u ON u.id=t.user_id "
                        "WHERE t.token=? AND t.expires>? AND a.active AND u.active AND u.role='admin'",
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
    with db(SYSTEM) as s:
        s.execute('SELECT 1')
        similarity = matching.engine(s)
        isolation = 'row-level-security' if isolated(s) else 'off'
    return {'status': 'ok', 'database': 'postgresql' if database_url() else 'sqlite-local', 'similarity': similarity, 'isolation': isolation, 'ocr': ocr_engine()}


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


@app.get('/users-ui.js')
def users_ui():
    return FileResponse(ROOT / 'static/users-ui.js', media_type='text/javascript', headers={'Cache-Control': 'no-store, max-age=0'})


@app.get('/globe.js')
def globe_js():
    return FileResponse(ROOT / 'static/globe.js', media_type='text/javascript', headers={'Cache-Control': 'no-store, max-age=0'})


@app.get('/world.js')
def world_js():
    # Static country outlines; the page requests it with a version query, so it can be cached.
    return FileResponse(ROOT / 'static/world.js', media_type='text/javascript', headers={'Cache-Control': 'public, max-age=86400'})
