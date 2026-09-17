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

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .metrics import normalize, compute
from .security import hash_password, verify_password, token_hash
from .storage import db, initialize, decode, database_url

ROOT = Path(__file__).resolve().parent.parent
DUMMY = hash_password('not-a-real-account')


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
        row = s.execute('SELECT a.id,a.username,a.company FROM carbon_accounts a JOIN carbon_sessions t ON t.account=a.id WHERE t.token=? AND t.expires>?',
                        (token_hash(request.cookies.get('ivz_carbon_session', '')), time.time())).fetchone()
    if not row:
        raise HTTPException(401, 'Ingresá a tu cuenta de IVZ Carbon.')
    return dict(row)


def event(s, user, action, revision, details):
    s.execute('INSERT INTO carbon_events VALUES (?,?,?,?,?,?)',
              (str(uuid.uuid4()), user, action, revision, datetime.now(timezone.utc).isoformat(), s.json(details)))


def bootstrap_account():
    """Provision the initial account from a secret hash; never reset an existing user."""
    digest = os.getenv('CARBON_BOOTSTRAP_PASSWORD_HASH')
    if not digest:
        return
    with db() as s:
        s.execute('INSERT INTO carbon_accounts VALUES (?,?,?,?) ON CONFLICT(username) DO NOTHING',
                  (str(uuid.uuid4()), 'demo', 'IVZ Carbon', digest))


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
    token = secrets.token_urlsafe(32)
    with db() as s:
        s.execute('DELETE FROM carbon_sessions WHERE expires<?', (now,))
        s.execute('INSERT INTO carbon_sessions VALUES (?,?,?)', (token_hash(token), user['id'], now+28800))
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
    return account(request)


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
    state = load(account(request)['id'])['state']
    if not state:
        raise HTTPException(404, 'Inicializá el inventario.')
    return compute(state, year, site)


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


@app.get('/health')
def health():
    with db() as s:
        s.execute('SELECT 1')
    return {'status': 'ok', 'database': 'postgresql' if database_url() else 'sqlite-local'}


@app.get('/')
def index(request: Request):
    try:
        account(request)
    except HTTPException:
        return FileResponse(ROOT / 'static/login.html')
    return FileResponse(ROOT / 'static/index.html')


@app.get('/bridge.js')
def bridge(request: Request):
    account(request)
    return FileResponse(ROOT / 'static/bridge.js', media_type='text/javascript')
