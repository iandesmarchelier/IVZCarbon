import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.app import app, bootstrap_account
from backend.manage import create_user
from backend.metrics import compute, normalize
from backend.storage import db, database_url
from backend.security import hash_password, verify_password

SEED = json.loads((ROOT/'seed.json').read_text(encoding='utf8'))


class MetricsTests(unittest.TestCase):
    def test_matches_v35_reference(self):
        reference = json.loads((ROOT/'tests/reference.json').read_text())
        result = compute(normalize(SEED))
        self.assertEqual(result['count'], reference['count'])
        for key, value in reference['scopes'].items():
            self.assertAlmostEqual(result['scopes'][int(key)], value, places=6)
        self.assertAlmostEqual(result['locationKg'], reference['location'], places=6)
        self.assertAlmostEqual(result['uncertainty']['pct'], reference['uncertainty']['pct'], places=10)

    def test_recomputes_untrusted_totals(self):
        s = copy.deepcopy(SEED)
        s['REC'][0].update(kg=-999, scope=3)
        fixed = normalize(s)
        f = next(f for f in s['FACTORS'] if f['id'] == s['REC'][0]['factor'])
        self.assertEqual(fixed['REC'][0]['kg'], s['REC'][0]['qty']*f['v'])
        self.assertEqual(fixed['REC'][0]['scope'], f['scope'])

    def test_invalid_quantities_and_references(self):
        for key, value in [('qty',-1),('qty',float('nan')),('site','missing'),('factor','missing'),('unit','wrong')]:
            s=copy.deepcopy(SEED);s['REC'][0][key]=value
            with self.subTest(key=key,value=value), self.assertRaises(ValueError):normalize(s)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ, {'CARBON_DATABASE_URL':'','CARBON_SQLITE_PATH':str(Path(self.tmp.name)/'carbon.sqlite'),'CARBON_ENV':'test'})
        self.env.start()
        self.client=TestClient(app).__enter__()
        create_user('one','Empresa uno','test-password-123','one@example.com')
        create_user('two','Empresa dos','test-password-123','two@example.com')
        self.h={'X-IVZ-Carbon':'1'}
        self.codes=[]
        self.mailer=patch('backend.mfa.send_code',lambda email,code:self.codes.append((email,code)))
        self.mailer.start()

    def tearDown(self):
        self.mailer.stop();self.client.__exit__(None,None,None);self.env.stop();self.tmp.cleanup()

    def password_step(self,user='one'):
        r=self.client.post('/api/login',headers=self.h,json={'username':user,'password':'test-password-123'})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json(),{'mfa':True,'email':user[:2]+'•••@example.com'})
        return self.codes[-1][1]

    def verify(self,code):
        return self.client.post('/api/login/verify',headers=self.h,json={'code':code})

    def login(self,user='one'):
        r=self.verify(self.password_step(user))
        self.assertEqual(r.status_code,200,r.text)

    def test_email_code_is_required_single_use_and_limited(self):
        code=self.password_step()
        self.assertEqual(self.client.get('/api/state').status_code,401)
        wrong=('1' if code[0]!='1' else '2')+code[1:]
        self.assertEqual(self.verify(wrong).status_code,401)
        self.assertEqual(self.verify(code).status_code,200)
        self.assertEqual(self.client.get('/api/me').json()['username'],'one')
        self.assertEqual(self.verify(code).status_code,410)
        self.client.post('/api/logout',headers=self.h)
        code=self.password_step();wrong=('1' if code[0]!='1' else '2')+code[1:]
        for _ in range(4):self.assertEqual(self.verify(wrong).status_code,401)
        self.assertEqual(self.verify(wrong).status_code,410)
        self.assertEqual(self.verify(code).status_code,410)
        self.assertEqual(self.client.post('/api/login/resend',headers=self.h,json={}).status_code,410)

    def test_account_without_email_cannot_log_in(self):
        with db() as s:s.execute("UPDATE carbon_accounts SET email=NULL WHERE username='one'")
        r=self.client.post('/api/login',headers=self.h,json={'username':'one','password':'test-password-123'})
        self.assertEqual(r.status_code,403)
        self.assertEqual(self.codes,[])

    def initialize(self,mode='demo'):
        r=self.client.post('/api/initialize',headers=self.h,json={'mode':mode})
        self.assertEqual(r.status_code,200,r.text)
        return r.json()

    def test_login_gate_and_csrf(self):
        self.assertIn('Ingresar',self.client.get('/').text)
        self.assertEqual(self.client.get('/api/state').status_code,401)
        self.assertEqual(self.client.get('/bridge.js').status_code,401)
        self.assertEqual(self.client.post('/api/login',json={}).status_code,403)
        self.login()
        self.assertIn('/bridge.js',self.client.get('/').text)
        self.client.post('/api/logout',headers=self.h)
        self.assertEqual(self.client.get('/api/state').status_code,401)

    def test_persistence_conflict_and_isolation(self):
        self.login(); body=self.initialize()
        body['state']['SITES'][0]['name']='Planta guardada'
        r=self.client.put('/api/state',headers=self.h,json=body)
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.client.put('/api/state',headers=self.h,json=body).status_code,409)
        self.assertEqual(self.client.get('/api/state').json()['state']['SITES'][0]['name'],'Planta guardada')
        self.assertEqual(self.client.get('/api/records?limit=2').json()['total'],len(SEED['REC']))
        self.assertEqual(len(self.client.get('/api/records?limit=2').json()['items']),2)
        self.login('two')
        self.assertIsNone(self.client.get('/api/state').json()['state'])
        self.assertEqual(self.client.get('/api/records').json()['total'],0)
        self.assertEqual(self.client.get('/api/summary').status_code,404)
        self.initialize('empty')
        self.assertEqual(self.client.get('/api/summary').json()['count'],0)

    def test_atomic_invalid_save_and_exports(self):
        self.login();body=self.initialize()
        body['state']['REC'][0]['qty']=-3
        self.assertEqual(self.client.put('/api/state',headers=self.h,json=body).status_code,422)
        self.assertEqual(self.client.get('/api/state').json()['revision'],1)
        report=self.client.get('/api/summary?year=2025').json()
        self.assertEqual(report['count'],len([r for r in SEED['REC'] if r['p'].startswith('2025')]))
        self.assertEqual(self.client.get('/api/export').json()['revision'],1)
        self.assertIn('attachment',self.client.get('/api/inventory.csv').headers['content-disposition'])
        actions=[x['action'] for x in self.client.get('/api/audit').json()]
        self.assertIn('initialize_demo',actions)
        self.assertEqual(self.client.post('/api/initialize',headers=self.h,json={'mode':'empty'}).status_code,409)

    def test_expiry_and_rate_limit(self):
        self.login()
        with db() as s:s.execute('UPDATE carbon_sessions SET expires=0')
        self.assertEqual(self.client.get('/api/state').status_code,401)
        for _ in range(10):
            self.assertEqual(self.client.post('/api/login',headers=self.h,json={'username':'unknown','password':'bad'}).status_code,401)
        self.assertEqual(self.client.post('/api/login',headers=self.h,json={'username':'unknown','password':'bad'}).status_code,429)

    def test_bootstrap_does_not_reset_existing_account(self):
        with patch.dict(os.environ, {'CARBON_BOOTSTRAP_PASSWORD_HASH':hash_password('first-password-123')}):
            bootstrap_account()
        with patch.dict(os.environ, {'CARBON_BOOTSTRAP_PASSWORD_HASH':hash_password('second-password-456')}):
            bootstrap_account()
        with db() as s:
            rows=s.execute('SELECT password FROM carbon_accounts WHERE username=?',('demo',)).fetchall()
        self.assertEqual(len(rows),1)
        self.assertTrue(verify_password('first-password-123',rows[0]['password']))

    def test_api_token_link_endpoints(self):
        self.login(); self.initialize()
        token = self.client.post('/api/tokens', headers=self.h, json={'label': 'IVZ Sustainability Hub'}).json()['token']
        listed = self.client.get('/api/tokens', headers=self.h).json()
        self.assertEqual(len(listed), 1)
        self.assertNotIn('token', listed[0])
        auth = {'Authorization': 'Bearer ' + token}
        self.assertEqual(self.client.get('/api/summary', headers=auth).status_code, 200)
        sites = self.client.get('/api/link/sites', headers=auth).json()
        self.assertEqual({s['id'] for s in sites}, {s['id'] for s in SEED['SITES']})
        self.assertEqual(self.client.get('/api/link/periods', headers=auth).json(), SEED['PERIODS'])
        with TestClient(app) as anon:
            # A read token (no session cookie) must not grant write access to mutating endpoints.
            self.assertEqual(anon.put('/api/state', headers={**self.h, **auth}, json={'revision': 1, 'state': {}}).status_code, 401)
            self.assertEqual(anon.get('/api/summary', headers=auth).status_code, 200)
            self.assertEqual(anon.get('/api/summary').status_code, 401)
            self.client.delete('/api/tokens/' + listed[0]['id'], headers=self.h)
            self.assertEqual(anon.get('/api/summary', headers=auth).status_code, 401)

    def test_vercel_database_configuration(self):
        with patch.dict(os.environ, {'VERCEL':'1','DATABASE_URL':'postgresql://integration-test'}):
            self.assertEqual(database_url(),'postgresql://integration-test')
        with patch.dict(os.environ, {'VERCEL':'1','DATABASE_URL':''}):
            with self.assertRaises(RuntimeError), db():pass


if __name__=='__main__':unittest.main()
