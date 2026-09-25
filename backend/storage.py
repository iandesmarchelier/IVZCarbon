"""PostgreSQL storage. SQLite is available only when explicitly selected for local tests.

Tenant isolation. Every connection says whose data it works on: db(account_id) for a client's
request, db(SYSTEM) for login, the administrator and maintenance scripts. On PostgreSQL,
db(account_id) runs its transaction as TENANT_ROLE with ivz.account set to that id, and
row-level security on every table with an account column (and on carbon_accounts itself) hides
and refuses the other accounts' rows, even if a query forgets its WHERE account=?. db(SYSTEM)
runs as the connecting role, which owns the tables and is not subject to the policies.
SQLite has no row-level security; the argument is required anyway.
"""
import json
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SYSTEM = object()
TENANT_ROLE = 'ivz_carbon_tenant'
# Whether the connecting role can switch to TENANT_ROLE; None until checked in this process.
_tenant_role = {'ready': None}


class Session:
    def __init__(self, conn, postgres):
        self.conn, self.postgres = conn, postgres

    def execute(self, sql, args=()):
        return self.conn.execute(sql.replace('?', '%s') if self.postgres else sql, args)

    def executemany(self, sql, rows):
        if not rows:
            return
        if self.postgres:
            with self.conn.cursor() as cursor:
                cursor.executemany(sql.replace('?', '%s'), rows)
        else:
            self.conn.executemany(sql, rows)

    def json(self, value):
        if self.postgres:
            from psycopg.types.json import Jsonb
            return Jsonb(value)
        return json.dumps(value, ensure_ascii=False, allow_nan=False)


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


def event(s, user, action, revision, details, actor=None):
    """user is the account; actor, who did it (a user of the account, or an administrator)."""
    s.execute('INSERT INTO carbon_events (id,account,action,revision,created,details,actor) VALUES (?,?,?,?,?,?,?)',
              (str(uuid.uuid4()), user, action, revision, datetime.now(timezone.utc).isoformat(), s.json(details), actor))


@contextmanager
def db(account, repeatable=False):
    """repeatable: one consistent view of the data for the whole transaction (long downloads)."""
    if account is not SYSTEM and not (isinstance(account, str) and account):
        raise ValueError('db() needs the account id, or SYSTEM')
    url = database_url()
    if url:
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(url, row_factory=dict_row, connect_timeout=10)
        if repeatable:
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    else:
        path = os.getenv('CARBON_SQLITE_PATH')
        if not path or os.getenv('CARBON_ENV') == 'production' or os.getenv('VERCEL'):
            raise RuntimeError('Configurá CARBON_DATABASE_URL para PostgreSQL.')
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
    try:
        if url and account is not SYSTEM:
            _enter(conn, account)
        yield Session(conn, bool(url))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _enter(conn, account):
    """Scope the transaction to one account. Both settings are transaction-local (SET LOCAL), so a
    pooled connection (Neon's pgbouncer) never carries them into someone else's transaction."""
    if _tenant_role['ready'] is None:
        _tenant_role['ready'] = bool(conn.execute(
            'SELECT 1 FROM pg_roles r WHERE r.rolname=%s AND pg_has_role(current_user, r.oid, %s)',
            (TENANT_ROLE, _switch(conn))).fetchone())
        if not _tenant_role['ready']:
            logging.getLogger(__name__).error('Row-level security is NOT enforced: %s is missing or cannot be used.', TENANT_ROLE)
    if _tenant_role['ready']:
        conn.execute("SELECT set_config('role', %s, true), set_config('ivz.account', %s, true)", (TENANT_ROLE, account))
    else:
        conn.execute("SELECT set_config('ivz.account', %s, true)", (account,))


def isolated(s):
    """Whether client connections are confined by row-level security; /health reports it."""
    if not s.postgres:
        return False
    role = s.execute('SELECT pg_has_role(current_user, oid, ?) AS ok FROM pg_roles WHERE rolname=?', (_switch(s.conn), TENANT_ROLE)).fetchone()
    exposed = s.execute("SELECT 1 FROM information_schema.columns c JOIN pg_class t ON t.relname=c.table_name "
                        "AND t.relnamespace=current_schema()::regnamespace WHERE c.table_schema=current_schema() "
                        "AND c.column_name='account' AND NOT t.relrowsecurity").fetchone()
    return bool(role and role['ok']) and not exposed


def _switch(conn):
    # Since PostgreSQL 16 a role's creator holds ADMIN on it but may still lack SET, which SET ROLE needs.
    return 'SET' if conn.info.server_version >= 160000 else 'MEMBER'


def database_url():
    # Vercel's PostgreSQL integration provides DATABASE_URL in this project's scope.
    return os.getenv('CARBON_DATABASE_URL') or (os.getenv('DATABASE_URL') if os.getenv('VERCEL') else None)


def initialize():
    with db(SYSTEM) as s:
        # Serialize schema creation across workers on PostgreSQL.
        if s.postgres:
            s.execute('SELECT pg_advisory_xact_lock(8347021)')
        j = 'JSONB' if s.postgres else 'TEXT'
        for sql in [
            'CREATE TABLE IF NOT EXISTS carbon_schema (version INTEGER PRIMARY KEY)',
            'CREATE TABLE IF NOT EXISTS carbon_accounts (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, company TEXT NOT NULL, password TEXT NOT NULL)',
            f'CREATE TABLE IF NOT EXISTS carbon_reports (id TEXT PRIMARY KEY, account TEXT NOT NULL REFERENCES carbon_accounts(id) ON DELETE CASCADE, year INTEGER NOT NULL, created TEXT NOT NULL, body {j} NOT NULL)',
            'CREATE TABLE IF NOT EXISTS carbon_sessions (token TEXT PRIMARY KEY, account TEXT NOT NULL REFERENCES carbon_accounts(id) ON DELETE CASCADE, expires DOUBLE PRECISION NOT NULL)',
            'CREATE TABLE IF NOT EXISTS carbon_login_limits (username TEXT PRIMARY KEY, attempts INTEGER NOT NULL, reset_at DOUBLE PRECISION NOT NULL)',
            f'CREATE TABLE IF NOT EXISTS carbon_states (account TEXT PRIMARY KEY REFERENCES carbon_accounts(id), revision INTEGER NOT NULL, body {j} NOT NULL, updated TEXT NOT NULL)',
            f'CREATE TABLE IF NOT EXISTS carbon_entities (account TEXT NOT NULL REFERENCES carbon_accounts(id), kind TEXT NOT NULL, id TEXT NOT NULL, body {j} NOT NULL, PRIMARY KEY(account,kind,id))',
            f'CREATE TABLE IF NOT EXISTS carbon_records (account TEXT NOT NULL REFERENCES carbon_accounts(id), id TEXT NOT NULL, period TEXT NOT NULL, site TEXT NOT NULL, scope INTEGER NOT NULL CHECK(scope IN (1,2,3)), factor TEXT NOT NULL, quantity DOUBLE PRECISION NOT NULL, kg DOUBLE PRECISION NOT NULL, body {j} NOT NULL, PRIMARY KEY(account,id))',
            f'CREATE TABLE IF NOT EXISTS carbon_events (id TEXT PRIMARY KEY, account TEXT NOT NULL REFERENCES carbon_accounts(id), action TEXT NOT NULL, revision INTEGER NOT NULL, created TEXT NOT NULL, details {j} NOT NULL)',
            'CREATE TABLE IF NOT EXISTS carbon_api_tokens (id TEXT PRIMARY KEY, account TEXT NOT NULL REFERENCES carbon_accounts(id) ON DELETE CASCADE, label TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL, created TEXT NOT NULL, last_used TEXT)',
            f'CREATE TABLE IF NOT EXISTS carbon_state_backups (account TEXT NOT NULL REFERENCES carbon_accounts(id) ON DELETE CASCADE, created TEXT NOT NULL, revision INTEGER NOT NULL, body {j} NOT NULL)',
            f'CREATE TABLE IF NOT EXISTS carbon_uploads (account TEXT NOT NULL REFERENCES carbon_accounts(id) ON DELETE CASCADE, batch TEXT NOT NULL, part INTEGER NOT NULL, created DOUBLE PRECISION NOT NULL, body {j} NOT NULL, PRIMARY KEY(account,batch,part))',
            f'CREATE TABLE IF NOT EXISTS carbon_closures (account TEXT NOT NULL REFERENCES carbon_accounts(id) ON DELETE CASCADE, year INTEGER NOT NULL, closed_at TEXT NOT NULL, closed_by TEXT NOT NULL, results {j} NOT NULL, factors {j} NOT NULL, sites {j} NOT NULL, PRIMARY KEY(account,year))',
            # Uploaded bills and manifests (backend/documents.py).
            f"CREATE TABLE IF NOT EXISTS carbon_documents (id TEXT PRIMARY KEY, account TEXT NOT NULL REFERENCES carbon_accounts(id) ON DELETE CASCADE, name TEXT NOT NULL, type TEXT NOT NULL, size INTEGER NOT NULL, created DOUBLE PRECISION NOT NULL, linked BOOLEAN NOT NULL DEFAULT FALSE, data {'BYTEA' if s.postgres else 'BLOB'} NOT NULL)",
            'CREATE INDEX IF NOT EXISTS carbon_documents_account ON carbon_documents(account,linked)',
            # The people who sign in to each account (backend/users.py).
            'CREATE TABLE IF NOT EXISTS carbon_users (id TEXT PRIMARY KEY, account TEXT NOT NULL REFERENCES carbon_accounts(id) ON DELETE CASCADE, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT NOT NULL, active BOOLEAN NOT NULL DEFAULT TRUE, created TEXT NOT NULL)',
            'CREATE INDEX IF NOT EXISTS carbon_users_account ON carbon_users(account)',
            'CREATE INDEX IF NOT EXISTS carbon_records_filter ON carbon_records(account,period,site,scope)',
            'CREATE INDEX IF NOT EXISTS carbon_api_tokens_account ON carbon_api_tokens(account)',
            'CREATE INDEX IF NOT EXISTS carbon_sessions_expiry ON carbon_sessions(expires)',
            'CREATE INDEX IF NOT EXISTS carbon_events_account ON carbon_events(account,created)',
            'INSERT INTO carbon_schema(version) VALUES(1) ON CONFLICT(version) DO NOTHING',
        ]:
            s.execute(sql)
        _ensure_column(s, 'carbon_accounts', 'role', "role TEXT NOT NULL DEFAULT 'client'")
        _ensure_column(s, 'carbon_accounts', 'active', 'active BOOLEAN NOT NULL DEFAULT TRUE')
        _ensure_column(s, 'carbon_accounts', 'created', "created TEXT NOT NULL DEFAULT ''")
        # Sections and integrations an administrator switched on or off for the account (backend/features.py).
        _ensure_column(s, 'carbon_accounts', 'settings', "settings TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(s, 'carbon_sessions', 'impersonated_by', 'impersonated_by TEXT')
        _ensure_column(s, 'carbon_sessions', 'user_id', 'user_id TEXT')
        _ensure_column(s, 'carbon_events', 'actor', 'actor TEXT')
        # Accounts from before users existed: their login becomes the first administrator, with the
        # account's id, and their open sessions belong to it. Users are never deleted, so this adds
        # nothing once done.
        s.execute("INSERT INTO carbon_users (id,account,username,password,role,active,created) "
                  "SELECT id,id,username,password,'admin',TRUE,created FROM carbon_accounts WHERE TRUE ON CONFLICT DO NOTHING")
        s.execute('UPDATE carbon_sessions SET user_id=account WHERE user_id IS NULL AND impersonated_by IS NULL')
        # Row order inside the inventory: records and movements are stored as rows, not inside the state body.
        _ensure_column(s, 'carbon_records', 'seq', 'seq INTEGER')
        _ensure_column(s, 'carbon_entities', 'seq', 'seq INTEGER')
        if s.postgres:
            isolate(s, 'carbon_accounts')


def isolate(s, accounts_table):
    """Row-level security for every table with an account column, found in the schema so that a new
    table is covered without being listed, plus the accounts table on its id. Only changes what is
    missing: ALTER TABLE locks the table, and this runs on every cold start."""
    tables = {r['table_name']: 'account' for r in s.execute(
        "SELECT DISTINCT table_name FROM information_schema.columns WHERE table_schema=current_schema() AND column_name='account'").fetchall()}
    tables[accounts_table] = 'id'
    secured = {r['relname'] for r in s.execute(
        'SELECT relname FROM pg_class WHERE relnamespace=current_schema()::regnamespace AND relrowsecurity').fetchall()}
    policies = {r['tablename'] for r in s.execute(
        "SELECT tablename FROM pg_policies WHERE schemaname=current_schema() AND policyname='tenant'").fetchall()}
    for table, column in sorted(tables.items()):
        if table not in policies:
            # USING also checks inserted and updated rows: an account cannot write rows for another.
            s.execute(f"CREATE POLICY tenant ON {table} USING ({column} = current_setting('ivz.account', true))")
        if table not in secured:
            s.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
    # The table owner (the connecting role) is exempt from the policies, so requests switch to a role
    # that is not. It may only read and write the protected tables.
    s.execute('SAVEPOINT tenant_role')
    try:
        if not s.execute('SELECT 1 FROM pg_roles WHERE rolname=?', (TENANT_ROLE,)).fetchone():
            s.execute(f'CREATE ROLE {TENANT_ROLE} NOLOGIN NOBYPASSRLS')
        if not s.execute('SELECT pg_has_role(current_user, ?, ?) AS ok', (TENANT_ROLE, _switch(s.conn))).fetchone()['ok']:
            s.execute(f'GRANT {TENANT_ROLE} TO CURRENT_USER')
        missing = [r['relname'] for r in s.execute(
            'SELECT relname FROM pg_class WHERE relnamespace=current_schema()::regnamespace AND relname=ANY(?) AND NOT ('
            + ' AND '.join(f"has_table_privilege(?, oid, '{p}')" for p in ('SELECT', 'INSERT', 'UPDATE', 'DELETE')) + ') ORDER BY relname',
            (sorted(tables), *[TENANT_ROLE] * 4)).fetchall()]
        if missing:
            s.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(missing)} TO {TENANT_ROLE}")
        s.execute('RELEASE SAVEPOINT tenant_role')
        _tenant_role['ready'] = True
    except Exception:
        s.execute('ROLLBACK TO SAVEPOINT tenant_role')
        _tenant_role['ready'] = False
        logging.getLogger(__name__).exception(
            'Row-level security is NOT enforced: the database role cannot create or use %s.', TENANT_ROLE)


def _column_exists(s, table, column):
    if s.postgres:
        row = s.execute('SELECT 1 FROM information_schema.columns WHERE table_name=? AND column_name=?', (table, column)).fetchone()
    else:
        row = next((r for r in s.execute(f'PRAGMA table_info({table})').fetchall() if r[1] == column), None)
    return bool(row)


def _ensure_column(s, table, column, coldef):
    if not _column_exists(s, table, column):
        s.execute(f'ALTER TABLE {table} ADD COLUMN {coldef}')
