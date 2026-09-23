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
from backend.storage import db, database_url, decode
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
        create_user('one','Empresa uno','test-password-123')
        create_user('two','Empresa dos','test-password-123')
        self.h={'X-IVZ-Carbon':'1'}

    def tearDown(self):
        self.client.__exit__(None,None,None);self.env.stop();self.tmp.cleanup()

    def login(self,user='one'):
        r=self.client.post('/api/login',headers=self.h,json={'username':user,'password':'test-password-123'})
        self.assertEqual(r.status_code,200,r.text)

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

    def diff(self,before,after):
        """What the screen sends: catalogue if changed, changed/removed rows, explicit order only if it moved."""
        body={'revision':before['revision']}
        cat=lambda st:{k:v for k,v in st.items() if k not in ('REC','MOV')}
        if cat(before['state'])!=cat(after):body['catalogue']=cat(after)
        body['changes'],body['order']={},{}
        for kind,key in (('REC','rid'),('MOV','id')):
            old={x[key]:x for x in before['state'][kind]};ids=[x[key] for x in after[kind]];keep=set(ids)
            upsert=[x for x in after[kind] if old.get(x[key])!=x];delete=[k for k in old if k not in keep]
            if upsert or delete:body['changes'][kind]={'upsert':upsert,'delete':delete}
            expected=[k for k in old if k in keep]+[k for k in ids if k not in old]
            if ids!=expected:body['order'][kind]=ids
        return body

    def paged(self):
        cat=self.client.get('/api/state/catalogue').json();state=dict(cat['state'])
        for kind in ('REC','MOV'):
            state[kind]=[]
            for offset in range(0,cat['counts'][kind],700):
                page=self.client.get(f'/api/state/rows?kind={kind}&offset={offset}&limit=700').json()
                self.assertEqual(page['revision'],cat['revision']);state[kind]+=page['items']
        return {'revision':cat['revision'],'state':state}

    def test_saving_changes_matches_saving_everything(self):
        self.login();current=self.initialize()
        self.assertEqual(self.paged(),{'revision':current['revision'],'state':current['state']})
        def edit(s):s['REC'][5]['qty']=123.0;s['REC'][40]['qty']=0.0
        def delete(s):
            gone={r['rid'] for r in s['REC'][10:20]}
            s['REC'][:]=[r for i,r in enumerate(s['REC']) if not 10<=i<20]
            for r in s['REC']:
                if r.get('pair') in gone:r.pop('pair')
            s['MOV'].pop(3)
        def add(s):
            r=copy.deepcopy(s['REC'][0]);r.update(rid='R99001',qty=7.5);r.pop('pair',None);s['REC'].append(r)
            m=copy.deepcopy(s['MOV'][0]);m['id']='MOV-TEST-1';s['MOV'].append(m)
        def factor(s):s['FACTORS'][0]['v']*=2  # every record using it gets a new kg on the server
        def reorder(s):s['REC'].reverse()
        def insert_middle(s):
            r=copy.deepcopy(s['REC'][1]);r.update(rid='R99002',qty=1.0);r.pop('pair',None);s['REC'].insert(3,r)
        for step in (edit,delete,add,factor,reorder,insert_middle):
            with self.subTest(step=step.__name__):
                after=copy.deepcopy(current['state']);step(after)
                r=self.client.post('/api/state/changes',headers=self.h,json=self.diff(current,after))
                self.assertEqual(r.status_code,200,r.text)
                expected=normalize(after)
                self.assertEqual(r.json()['summary'],json.loads(json.dumps(compute(expected))))
                current=self.client.get('/api/state').json()
                self.assertEqual(current['state'],expected)
                self.assertEqual(self.paged(),{'revision':current['revision'],'state':current['state']})
        factor0=current['state']['FACTORS'][0]
        with db() as s:  # the queryable copy follows the factor change
            rows=s.execute("SELECT kg,quantity FROM carbon_records WHERE factor=? AND account=(SELECT id FROM carbon_accounts WHERE username='one')",(factor0['id'],)).fetchall()
        self.assertTrue(rows and all(abs(r['kg']-r['quantity']*factor0['v'])<1e-6 for r in rows))

    def test_changes_are_atomic_and_checked(self):
        self.login();current=self.initialize();revision=current['revision']
        bad=copy.deepcopy(current['state']);bad['REC'][0]['factor']='NOPE'
        self.assertEqual(self.client.post('/api/state/changes',headers=self.h,json=self.diff(current,bad)).status_code,422)
        stale=copy.deepcopy(current['state']);stale['REC'][0]['qty']=1.0
        body=self.diff(current,stale);body['revision']=revision-1
        self.assertEqual(self.client.post('/api/state/changes',headers=self.h,json=body).status_code,409)
        self.assertEqual(self.client.post('/api/state/changes',headers=self.h,json={'revision':revision,'changes':{'REC':{'upsert':[{'qty':1}]}}}).status_code,422)
        self.assertEqual(self.client.get('/api/state').json(),current)
        # A large change arrives in staged parts; a missing part rejects the whole save.
        big=copy.deepcopy(current['state'])
        for r in big['REC']:r['qty']=r['qty']+1
        body=self.diff(current,big);upsert=body['changes']['REC'].pop('upsert')
        chunks=[upsert[i:i+1000] for i in range(0,len(upsert),1000)]
        for i,chunk in enumerate(chunks):
            r=self.client.post('/api/state/upload',headers=self.h,json={'batch':'batch-0001','part':i,'changes':{'REC':{'upsert':chunk}}})
            self.assertEqual(r.status_code,200,r.text)
        body['changes']['REC']['upsert']=[];body.update(batch='batch-0001',parts=len(chunks)+1)
        self.assertEqual(self.client.post('/api/state/changes',headers=self.h,json=body).status_code,409)
        self.assertEqual(self.client.get('/api/state').json(),current)
        for i,chunk in enumerate(chunks):
            self.client.post('/api/state/upload',headers=self.h,json={'batch':'batch-0002','part':i,'changes':{'REC':{'upsert':chunk}}})
        body.update(batch='batch-0002',parts=len(chunks))
        r=self.client.post('/api/state/changes',headers=self.h,json=body)
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.client.get('/api/state').json()['state'],normalize(big))
        self.login('two')
        self.assertIsNone(self.client.get('/api/state/catalogue').json()['state'])
        self.assertEqual(self.client.get('/api/state/rows?kind=REC').status_code,404)

    def test_single_body_inventories_are_moved_to_rows_once(self):
        self.login();current=self.initialize()
        with db() as s:  # write the inventory in the pre-split format, as production has it
            user=s.execute("SELECT id FROM carbon_accounts WHERE username='one'").fetchone()['id']
            s.execute('UPDATE carbon_states SET body=? WHERE account=?',(json.dumps(current['state']),user))
            s.execute('UPDATE carbon_records SET seq=NULL WHERE account=?',(user,))
            s.execute("DELETE FROM carbon_entities WHERE account=? AND kind='MOV'",(user,))
        self.assertEqual(self.paged(),{'revision':current['revision'],'state':current['state']})
        self.assertEqual(self.client.get('/api/state').json(),current)
        with db() as s:
            body=decode(s.execute('SELECT body FROM carbon_states WHERE account=?',(user,)).fetchone()['body'])
            backups=s.execute('SELECT body FROM carbon_state_backups WHERE account=?',(user,)).fetchall()
        self.assertNotIn('REC',body)
        self.assertEqual([decode(b['body']) for b in backups],[current['state']])

    def test_vercel_database_configuration(self):
        with patch.dict(os.environ, {'VERCEL':'1','DATABASE_URL':'postgresql://integration-test'}):
            self.assertEqual(database_url(),'postgresql://integration-test')
        with patch.dict(os.environ, {'VERCEL':'1','DATABASE_URL':''}):
            with self.assertRaises(RuntimeError), db():pass


if __name__=='__main__':unittest.main()
