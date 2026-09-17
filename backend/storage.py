"""PostgreSQL storage. SQLite is available only when explicitly selected for local tests."""
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Session:
    def __init__(self, conn, postgres):
        self.conn, self.postgres = conn, postgres

    def execute(self, sql, args=()):
        return self.conn.execute(sql.replace('?', '%s') if self.postgres else sql, args)

    def json(self, value):
        if self.postgres:
            from psycopg.types.json import Jsonb
            return Jsonb(value)
        return json.dumps(value, ensure_ascii=False, allow_nan=False)


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


@contextmanager
def db():
    url = database_url()
    if url:
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(url, row_factory=dict_row, connect_timeout=10)
    else:
        path = os.getenv('CARBON_SQLITE_PATH')
        if not path or os.getenv('CARBON_ENV') == 'production' or os.getenv('VERCEL'):
            raise RuntimeError('Configurá CARBON_DATABASE_URL para PostgreSQL.')
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
    try:
        yield Session(conn, bool(url))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def database_url():
    # Vercel's PostgreSQL integration provides DATABASE_URL in this project's scope.
    return os.getenv('CARBON_DATABASE_URL') or (os.getenv('DATABASE_URL') if os.getenv('VERCEL') else None)


def initialize():
    with db() as s:
        # Serialize schema creation across workers on PostgreSQL.
        if s.postgres:
            s.execute('SELECT pg_advisory_xact_lock(8347021)')
        j = 'JSONB' if s.postgres else 'TEXT'
        for sql in [
            'CREATE TABLE IF NOT EXISTS carbon_schema (version INTEGER PRIMARY KEY)',
            'CREATE TABLE IF NOT EXISTS carbon_accounts (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, company TEXT NOT NULL, password TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS carbon_sessions (token TEXT PRIMARY KEY, account TEXT NOT NULL REFERENCES carbon_accounts(id) ON DELETE CASCADE, expires DOUBLE PRECISION NOT NULL)',
            'CREATE TABLE IF NOT EXISTS carbon_login_limits (username TEXT PRIMARY KEY, attempts INTEGER NOT NULL, reset_at DOUBLE PRECISION NOT NULL)',
            f'CREATE TABLE IF NOT EXISTS carbon_states (account TEXT PRIMARY KEY REFERENCES carbon_accounts(id), revision INTEGER NOT NULL, body {j} NOT NULL, updated TEXT NOT NULL)',
            f'CREATE TABLE IF NOT EXISTS carbon_entities (account TEXT NOT NULL REFERENCES carbon_accounts(id), kind TEXT NOT NULL, id TEXT NOT NULL, body {j} NOT NULL, PRIMARY KEY(account,kind,id))',
            f'CREATE TABLE IF NOT EXISTS carbon_records (account TEXT NOT NULL REFERENCES carbon_accounts(id), id TEXT NOT NULL, period TEXT NOT NULL, site TEXT NOT NULL, scope INTEGER NOT NULL CHECK(scope IN (1,2,3)), factor TEXT NOT NULL, quantity DOUBLE PRECISION NOT NULL, kg DOUBLE PRECISION NOT NULL, body {j} NOT NULL, PRIMARY KEY(account,id))',
            f'CREATE TABLE IF NOT EXISTS carbon_events (id TEXT PRIMARY KEY, account TEXT NOT NULL REFERENCES carbon_accounts(id), action TEXT NOT NULL, revision INTEGER NOT NULL, created TEXT NOT NULL, details {j} NOT NULL)',
            'CREATE INDEX IF NOT EXISTS carbon_records_filter ON carbon_records(account,period,site,scope)',
            'CREATE INDEX IF NOT EXISTS carbon_sessions_expiry ON carbon_sessions(expires)',
            'CREATE INDEX IF NOT EXISTS carbon_events_account ON carbon_events(account,created)',
            'INSERT INTO carbon_schema(version) VALUES(1) ON CONFLICT(version) DO NOTHING',
        ]:
            s.execute(sql)
