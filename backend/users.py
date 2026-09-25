"""The people who sign in to a client's account.

The account is the company (the tenant): its data, its switches and its row-level security hang on
its id. Each user has its own login and one role in that company:
  admin   everything, including managing these users
  editor  loads and edits data, generates and approves reports, closes years
  viewer  looks and downloads; app.account() refuses any other request
An administrator of Invenzis manages them from /admin; a company's own admins, from their account,
except passwords: only Invenzis' administrator gives a user a new one (reset_password).
Users are deactivated, never deleted, so the account keeps who did what.
"""
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from .security import hash_password

ROLES = ('admin', 'editor', 'viewer')
LAST_ADMIN = 'La empresa tiene que conservar al menos un administrador activo.'
TAKEN = 'Ya existe un usuario con ese nombre.'


def _duplicate(exc):
    # sqlite3.IntegrityError or psycopg's UniqueViolation. Under row-level security a company cannot
    # see the usernames of other companies, so the unique index is what catches those.
    return type(exc).__name__ in ('IntegrityError', 'UniqueViolation')


def _clean(username):
    username = username.strip().lower()
    if not username:
        raise HTTPException(422, 'Ingresá el usuario.')
    return username


def create_account(s, username, company, role='client', password_hash=None):
    """A new company with its first user, an administrator whose id is the account's.

    Returns (account id, username, generated password or None when password_hash is given)."""
    username, company = _clean(username), company.strip()
    if not company:
        raise HTTPException(422, 'Ingresá el nombre de la empresa.')
    if s.execute('SELECT 1 FROM carbon_accounts WHERE username=?', (username,)).fetchone():
        raise HTTPException(409, 'Ya existe una cuenta con ese usuario.')
    password = None if password_hash else secrets.token_urlsafe(12)
    password_hash = password_hash or hash_password(password)
    account_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    # accounts.username and accounts.password only keep the first login, as before users existed.
    s.execute('INSERT INTO carbon_accounts (id,username,company,password,role,active,created) VALUES (?,?,?,?,?,?,?)',
              (account_id, username, company, password_hash, role, True, now))
    _insert(s, account_id, account_id, username, password_hash, 'admin', now)
    return account_id, username, password


def _insert(s, user_id, account, username, password_hash, role, now):
    try:
        s.execute('INSERT INTO carbon_users (id,account,username,password,role,active,created) VALUES (?,?,?,?,?,?,?)',
                  (user_id, account, username, password_hash, role, True, now))
    except Exception as exc:
        if _duplicate(exc):
            raise HTTPException(409, TAKEN) from exc
        raise


def listing(s, account):
    rows = s.execute('SELECT id,username,role,active,created FROM carbon_users WHERE account=? ORDER BY created,username', (account,)).fetchall()
    return [dict(r, active=bool(r['active'])) for r in rows]


def add(s, account, username, role):
    """A new user of the company, with a generated password; returns (user, password)."""
    if role not in ROLES:
        raise HTTPException(422, 'Rol inválido.')
    username = _clean(username)
    if s.execute('SELECT 1 FROM carbon_users WHERE username=?', (username,)).fetchone():
        raise HTTPException(409, TAKEN)
    password = secrets.token_urlsafe(12)
    user_id = str(uuid.uuid4())
    _insert(s, user_id, account, username, hash_password(password), role, datetime.now(timezone.utc).isoformat())
    return {'id': user_id, 'username': username, 'role': role, 'active': True}, password


def _target(s, account, user_id):
    if s.postgres:  # one change at a time per company, so two admins cannot demote each other at once
        s.execute('SELECT id FROM carbon_accounts WHERE id=? FOR UPDATE', (account,))
    row = s.execute('SELECT id,username,role,active FROM carbon_users WHERE id=? AND account=?', (user_id, account)).fetchone()
    if not row:
        raise HTTPException(404, 'Usuario no encontrado.')
    return row


def update(s, account, user_id, role=None, active=None):
    """Change a user's role or switch it off; the company always keeps an active administrator."""
    if role is not None and role not in ROLES:
        raise HTTPException(422, 'Rol inválido.')
    row = _target(s, account, user_id)
    role = row['role'] if role is None else role
    active = bool(row['active']) if active is None else active
    if row['role'] == 'admin' and row['active'] and (role != 'admin' or not active):
        others = s.execute("SELECT COUNT(*) AS n FROM carbon_users WHERE account=? AND role='admin' AND active AND id<>?",
                           (account, user_id)).fetchone()['n']
        if not others:
            raise HTTPException(409, LAST_ADMIN)
    s.execute('UPDATE carbon_users SET role=?, active=? WHERE id=?', (role, active, user_id))
    if not active:
        s.execute('DELETE FROM carbon_sessions WHERE user_id=?', (user_id,))
    return {'id': user_id, 'username': row['username'], 'role': role, 'active': active}


def reset_password(s, account, user_id):
    """A new generated password; the user's open sessions end."""
    row = _target(s, account, user_id)
    password = secrets.token_urlsafe(12)
    s.execute('UPDATE carbon_users SET password=? WHERE id=?', (hash_password(password), user_id))
    s.execute('DELETE FROM carbon_sessions WHERE user_id=?', (user_id,))
    return row['username'], password
