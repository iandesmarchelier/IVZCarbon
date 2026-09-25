"""Tenant isolation.

On PostgreSQL (CARBON_TEST_ISOLATION_URL, a disposable database named *test*: its tables are dropped) every API
test of test_carbon runs again with row-level security on, plus checks that the database itself
keeps each account's rows apart. Without PostgreSQL, a check of the code: only the login, the
administrator and the maintenance scripts may open a connection that is not scoped to one account.
Run from the repository root: python -m unittest discover -s tests
"""
import ast
import os
import unittest
import uuid
from pathlib import Path

import test_carbon as base
from backend import storage
from backend.storage import SYSTEM, db

URL = os.getenv('CARBON_TEST_ISOLATION_URL')
ACCOUNT_TABLES = ("SELECT DISTINCT table_name FROM information_schema.columns "
                  "WHERE table_schema=current_schema() AND column_name='account'")


@unittest.skipUnless(URL, 'CARBON_TEST_ISOLATION_URL no configurada')
class PostgresTests(base.ApiTests):
    def use_database(self):
        os.environ['CARBON_DATABASE_URL'] = URL
        with db(SYSTEM) as s:
            if 'test' not in s.conn.info.dbname:
                raise RuntimeError('CARBON_TEST_ISOLATION_URL debe ser una base de prueba descartable (con «test» en el nombre).')
            for r in s.execute('SELECT tablename FROM pg_tables WHERE schemaname=current_schema()').fetchall():
                s.execute(f'DROP TABLE {r["tablename"]} CASCADE')

    @unittest.skip('espera el motor Python de SQLite; en PostgreSQL responde pg_trgm (MatchingTests compara ambos)')
    def test_factor_matching_endpoint_and_approval(self):
        pass

    @unittest.skip('espera que no haya CARBON_DATABASE_URL, que acá apunta a la base de prueba')
    def test_vercel_database_configuration(self):
        pass

    def account_tables(self):
        with db(SYSTEM) as s:
            return sorted(r['table_name'] for r in s.execute(ACCOUNT_TABLES).fetchall())

    def ids(self):
        with db(SYSTEM) as s:
            return {r['username']: r['id'] for r in s.execute("SELECT id,username FROM carbon_accounts WHERE username IN ('one','two')").fetchall()}

    def fill_both_accounts(self):
        """A row for each account in every table that has an account column; returns {username: id}."""
        for user in ('one', 'two'):
            self.login(user)
            self.initialize()
            self.assertEqual(self.client.post('/api/reports', headers=self.h, json={'year': 2025}).status_code, 200)
            self.assertEqual(self.client.post('/api/tokens', headers=self.h, json={'label': 'Hub'}).status_code, 200)
            self.assertEqual(self.client.post('/api/state/upload', headers=self.h,
                                              json={'batch': 'batch-' + user, 'part': 0, 'changes': {}}).status_code, 200)
        ids = self.ids()
        with db(SYSTEM) as s:
            for account in ids.values():
                s.execute('INSERT INTO carbon_closures VALUES (?,?,?,?,?,?,?)', (account, 1999, 'x', 'x', s.json({}), s.json([]), s.json([])))
                s.execute('INSERT INTO carbon_state_backups VALUES (?,?,?,?)', (account, 'x', 1, s.json({})))
                s.execute('INSERT INTO carbon_documents (id,account,name,type,size,created,linked,data) VALUES (?,?,?,?,?,?,?,?)',
                          (uuid.uuid4().hex, account, 'x.pdf', 'application/pdf', 1, 0, False, b'x'))
            for table in self.account_tables():
                found = {r['account'] for r in s.execute(f'SELECT DISTINCT account FROM {table}').fetchall()}
                self.assertEqual(found, set(ids.values()), f'{table}: agregá filas de prueba para la tabla nueva')
        return ids

    def test_every_account_table_has_row_level_security(self):
        with db(SYSTEM) as s:
            secured = {r['relname'] for r in s.execute(
                'SELECT c.relname FROM pg_class c JOIN pg_policies p ON p.tablename=c.relname AND p.schemaname=current_schema() '
                'WHERE c.relnamespace=current_schema()::regnamespace AND c.relrowsecurity').fetchall()}
            role = s.execute('SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=?', (storage.TENANT_ROLE,)).fetchone()
        tables = set(self.account_tables()) | {'carbon_accounts'}
        self.assertGreaterEqual(len(tables), 12)
        self.assertEqual(tables - secured, set())
        self.assertEqual(dict(role), {'rolsuper': False, 'rolbypassrls': False})
        self.assertEqual(self.client.get('/health').json()['isolation'], 'row-level-security')

    def test_a_query_without_the_account_filter_sees_only_its_own_rows(self):
        ids = self.fill_both_accounts()
        for account in ids.values():
            with db(account) as s:
                for table in self.account_tables():
                    with self.subTest(account=account, table=table):
                        self.assertEqual({r['account'] for r in s.execute(f'SELECT account FROM {table}').fetchall()}, {account})
                self.assertEqual([r['id'] for r in s.execute('SELECT id FROM carbon_accounts').fetchall()], [account])
        with db('nobody') as s:
            for table in self.account_tables() + ['carbon_accounts']:
                self.assertEqual(s.execute(f'SELECT COUNT(*) AS n FROM {table}').fetchone()['n'], 0, table)

    def test_an_account_cannot_write_the_rows_of_another(self):
        import psycopg
        ids = self.fill_both_accounts()
        one, two = ids['one'], ids['two']
        count = lambda s: {t: s.execute(f'SELECT COUNT(*) AS n FROM {t} WHERE account=?', (two,)).fetchone()['n'] for t in self.account_tables()}
        with db(SYSTEM) as s:
            before = count(s)
        with db(one) as s:
            for table in self.account_tables():
                self.assertEqual(s.execute(f'DELETE FROM {table} WHERE account=?', (two,)).rowcount, 0, table)
            self.assertEqual(s.execute("UPDATE carbon_accounts SET company='x' WHERE id=?", (two,)).rowcount, 0)
        for sql, args in (("INSERT INTO carbon_events (id,account,action,revision,created,details) VALUES (?,?,'x',0,'x',?)", (uuid.uuid4().hex, two, '{}')),  # a row for another account
                          ('UPDATE carbon_reports SET account=?', (two,))):                                   # moving its own rows to another
            with self.subTest(sql=sql), self.assertRaises(psycopg.errors.InsufficientPrivilege), db(one) as s:
                s.execute(sql, args)
        with db(SYSTEM) as s:
            self.assertEqual(count(s), before)
            self.assertEqual(s.execute('SELECT company FROM carbon_accounts WHERE id=?', (two,)).fetchone()['company'], 'Empresa dos')

    def test_a_client_connection_reaches_only_the_protected_tables(self):
        import psycopg
        with self.assertRaises(psycopg.errors.InsufficientPrivilege), db('one') as s:
            s.execute('SELECT * FROM carbon_login_limits')
        with self.assertRaises(psycopg.errors.InsufficientPrivilege), db('one') as s:
            s.execute('DROP TABLE carbon_events')

    def test_the_account_scope_ends_with_its_transaction(self):
        # Neon's pooler hands the same server connection to other requests between transactions.
        import psycopg
        with psycopg.connect(URL) as conn:
            owner = conn.execute('SELECT current_user').fetchone()[0]
            conn.commit()
            storage._enter(conn, 'one')
            self.assertEqual(conn.execute("SELECT current_user, current_setting('ivz.account')").fetchone(), (storage.TENANT_ROLE, 'one'))
            conn.commit()
            self.assertEqual(conn.execute("SELECT current_user, current_setting('ivz.account', true)").fetchone(), (owner, ''))


class SystemScopeTests(unittest.TestCase):
    """db(SYSTEM) skips the account's row-level security; keep it where no single account applies."""
    ROOT = Path(__file__).resolve().parent.parent / 'backend'
    ALLOWED = {'app.py': {'lifespan', 'account', 'account_by_token', 'bootstrap_account', 'login', 'logout', 'admin_return', 'health'},
               'storage.py': {'initialize'}, 'manage.py': {'create_user'}, 'unsplit.py': {'main'}, 'check_postgres.py': {'main'}}
    ADMIN_CHECKS = {'require_admin'}

    def test_system_connections_are_only_for_login_admin_and_scripts(self):
        found = []
        for path in sorted(self.ROOT.glob('*.py')):
            for fn in ast.walk(ast.parse(path.read_text(encoding='utf8'))):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
                if not any(c.func.id == 'db' and c.args and isinstance(c.args[0], ast.Name) and c.args[0].id == 'SYSTEM' for c in calls):
                    continue
                found.append(fn.name)
                admin = any(c.func.id in self.ADMIN_CHECKS for c in calls)
                self.assertTrue(admin or fn.name in self.ALLOWED.get(path.name, ()),
                                f'{path.name}:{fn.lineno} {fn.name} usa db(SYSTEM) sin ser login, administración ni un script')
        self.assertIn('account', found)


if __name__ == '__main__':
    unittest.main()
