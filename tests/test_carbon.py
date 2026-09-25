import copy
import csv
import io
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
from backend.storage import SYSTEM, db, database_url, decode
from backend.security import hash_password, verify_password
from backend import features, matching
from backend.invoice_parser import ocr_engine, parse_document
from backend.storage import Session

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


class MatchingTests(unittest.TestCase):
    """Automatic factor assignment by text similarity (pg_trgm, or the same arithmetic in Python)."""
    factors = SEED['FACTORS']

    def rank(self, *items):
        return matching.rank(Session(None, False), self.factors, list(items))

    def test_picks_the_most_similar_factor_among_those_that_fit(self):
        cases = [({'text': 'Acero inoxidable — ASTM A276', 'scope': 3, 'cat': 1, 'unit': 'kg'}, 'FE-031'),
                 ({'text': 'Honorarios consultora ambiental', 'scope': 3, 'cat': 1, 'unit': 'usd'}, 'FE-033'),
                 ({'text': 'Vuelo Buenos Aires → Neuquén', 'scope': 3, 'cat': 6}, 'FE-060'),
                 ({'text': 'Hotelería', 'scope': 3, 'cat': 6}, 'FE-062'),
                 ({'text': 'home office Uruguay', 'scope': 3, 'cat': 7}, 'FE-073'),
                 ({'text': 'Electricidad de red Uruguay', 'scope': 2, 'unit': 'kWh'}, 'FE-011'),
                 ({'text': 'Transporte marítimo', 'scope': 3, 'cat': 4, 'unit': 't·km'}, 'FE-041')]
        for (item, expected), result in zip(cases, self.rank(*[c[0] for c in cases])):
            with self.subTest(item['text']):
                self.assertEqual(result['factor'], expected)
                self.assertTrue(0 < result['pct'] <= 100)
                self.assertTrue(all(a['pct'] <= result['pct'] for a in result['alts']))
        self.assertEqual(self.rank({'text': 'Hotelería', 'scope': 3, 'cat': 6})[0]['pct'], 100)

    def test_unit_scope_and_supplier_limit_the_candidates(self):
        paint = self.rank({'text': 'Pintura epoxi', 'scope': 3, 'cat': 1, 'unit': 'L'})[0]
        self.assertIsNone(paint['factor'])
        self.assertIn('unidad L', paint['error'])
        generic = self.rank({'text': 'Acero aleado', 'scope': 3, 'cat': 1, 'unit': 'kg'})[0]
        self.assertNotIn('FE-034', [generic['factor']] + [a['factor'] for a in generic['alts']])
        own = self.rank({'text': 'Acero aleado', 'scope': 3, 'cat': 1, 'unit': 'kg', 'supplier': 'PRV-1004'})[0]
        self.assertIn('FE-034', [a['factor'] for a in own['alts']])

    def test_equal_similarity_is_a_tie(self):
        twins = [dict(f, id=f['id'] + '-B', alias=matching.LIBRARY_TERMS[f['id']]) for f in self.factors if f['id'] == 'FE-062'] + self.factors
        result = matching.rank(Session(None, False), twins, [{'text': 'Hotel', 'scope': 3, 'cat': 6}])[0]
        self.assertTrue(result['tie'])
        self.assertIn(result['factor'], ('FE-062', 'FE-062-B'))
        self.assertEqual(len(result['tiedWith']), 1)
        self.assertFalse(self.rank({'text': 'Hotel', 'scope': 3, 'cat': 6})[0]['tie'])

    def test_learnt_terms_match_exactly(self):
        learnt = [dict(f, alias=['Varilla roscada M12']) if f['id'] == 'FE-030' else f for f in self.factors]
        result = matching.rank(Session(None, False), learnt, [{'text': 'varilla roscada m12', 'scope': 3, 'cat': 1, 'unit': 'kg'}])[0]
        self.assertEqual((result['factor'], result['pct']), ('FE-030', 100))

    def test_rounding_is_the_same_for_both_engines(self):
        self.assertEqual(matching.percent(0.725), 73)
        self.assertEqual(matching.percent(0.72500002), 73)
        self.assertEqual(matching.percent(0.72494), 72)

    @unittest.skipUnless(os.getenv('CARBON_TEST_POSTGRES_URL'), 'CARBON_TEST_POSTGRES_URL no configurada')
    def test_python_gives_what_pg_trgm_gives(self):
        import psycopg
        from psycopg.rows import dict_row
        texts = sorted({r['source'] for r in SEED['REC']})[:150] + [t for ts in matching.LIBRARY_TERMS.values() for t in ts]
        items = [{'text': t} for t in texts] + [{'text': t, 'scope': 3, 'cat': c} for t, c in zip(texts, [1, 4, 5, 6, 7] * 100)]
        with psycopg.connect(os.environ['CARBON_TEST_POSTGRES_URL'], row_factory=dict_row) as conn:
            s = Session(conn, True)
            matching.enable(s)
            self.assertEqual(matching.engine(s), 'pg_trgm')
            self.assertEqual(matching.rank(s, self.factors, items), self.rank(*items))
            for a in texts[:40]:
                for b in texts[-40:]:
                    ka, kb = matching.text_key(a), matching.text_key(b)
                    pg = s.execute('SELECT similarity(%s,%s) AS s, strict_word_similarity(%s,%s) AS w', (ka, kb, ka, kb)).fetchone()
                    self.assertAlmostEqual(pg['s'], matching.similarity(ka, kb), places=6)
                    self.assertAlmostEqual(pg['w'], matching.strict_word_similarity(ka, kb), places=6)
            conn.rollback()


class DocumentReadingTests(unittest.TestCase):
    """Bulk upload: the server tells what each document is and what points to its site."""
    BILL = ('EDENOR S.A. Liquidación de Servicio Público N° 0123-45678901\nNº de Cliente: 0012345678   NIS 4455667\n'
            'Domicilio de suministro: Av. del Libertador 4820, Vicente López\nPeríodo de consumo: 01/08/2025 AL 31/08/2025\nTotal Consumo 16.814,84 kWh')
    GAS = 'METROGAS\nConsumo total en m3: 312\nPERÍODO DE LIQUIDACIÓN: 01/07/2025 A 31/07/2025\nCuenta: 11-2233445-6'
    MANIFEST = ('MANIFIESTO DE TRANSPORTE DE RESIDUOS N° 48211\nGenerador: Planta Vicente López\nTransportista: Ecoprotec S.A.\n'
                'Fecha de retiro: 15/08/2025\nResiduo: Chatarra y viruta metálica. Peso neto: 6.800 kg. Tratamiento: reciclaje')

    def read(self, text, method='pdf-text'):
        with patch('backend.invoice_parser.extract_text', return_value=(text, method)):
            return parse_document(b'', 'documento.pdf', 'auto')

    def test_kind_period_quantity_and_location_hints(self):
        bill, gas, manifest = self.read(self.BILL), self.read(self.GAS), self.read(self.MANIFEST)
        self.assertEqual((bill['kind'], bill['fields']['period'], bill['fields']['qty']), ('elec', '2025-08', 16814.84))
        self.assertEqual(bill['hints']['accounts'], ['0012345678', '4455667'])
        self.assertIn('Av. del Libertador 4820', bill['hints']['addresses'][0])
        self.assertEqual((gas['kind'], gas['fields']['period'], gas['fields']['qty'], gas['hints']['accounts']), ('gas', '2025-07', 312.0, ['11-2233445-6']))
        self.assertEqual((manifest['kind'], manifest['fields']['date'], manifest['fields']['qty'], manifest['fields']['doc'], manifest['fields']['treatment']),
                         ('waste', '2025-08-15', 6800.0, '48211', 'reciclaje'))

    def test_unreadable_or_unknown_documents_ask_for_manual_data(self):
        self.assertEqual(self.read('', 'ocr-unavailable')['ok'], False)
        unknown = self.read('Recibo de sueldo de agosto')
        self.assertEqual((unknown['ok'], unknown['kind']), (False, None))
        with self.assertRaises(ValueError):
            parse_document(b'', 'x.pdf', 'other')

    @unittest.skipIf(ocr_engine() == 'off', 'Tesseract no está instalado (sí en el contenedor)')
    def test_photographed_bill_is_read_with_tesseract(self):
        from PIL import Image, ImageDraw, ImageFont
        image = Image.new('RGB', (1800, 420), 'white')
        ImageDraw.Draw(image).multiline_text((40, 30), self.BILL, fill='black', font=ImageFont.load_default(size=34), spacing=18)
        photo = io.BytesIO()
        image.save(photo, 'PNG')
        bill = parse_document(photo.getvalue(), 'factura.png', 'auto')
        self.assertEqual(ocr_engine(), 'tesseract-spa')
        self.assertEqual((bill['kind'], bill['fields']['period'], bill['fields']['qty']), ('elec', '2025-08', 16814.84))


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ, {'CARBON_DATABASE_URL':'','CARBON_SQLITE_PATH':str(Path(self.tmp.name)/'carbon.sqlite'),'CARBON_ENV':'test'})
        self.env.start()
        self.use_database()
        self.client=TestClient(app).__enter__()
        create_user('one','Empresa uno','test-password-123')
        create_user('two','Empresa dos','test-password-123')
        self.h={'X-IVZ-Carbon':'1'}

    def use_database(self):
        """SQLite here; tests/test_isolation.py runs these same tests on PostgreSQL with row-level security."""

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

    def test_annual_report_snapshot_and_tenant_isolation(self):
        self.assertEqual(self.client.get('/api/reports').status_code,401)
        self.login(); body=self.initialize()
        response=self.client.post('/api/reports',headers=self.h,json={'year':2025,'notes':{'activity':'<script>alert(1)</script>'}})
        self.assertEqual(response.status_code,200,response.text)
        report=response.json(); rid=report['id']
        summary=self.client.get('/api/summary?year=2025').json()
        self.assertAlmostEqual(report['result']['locationKg'],summary['locationKg'])
        self.assertAlmostEqual(sum(report['iso'].values()),summary['locationKg'])
        self.assertEqual(len(report['months']),12)
        document=self.client.get('/reports/'+rid)
        self.assertEqual(document.status_code,200)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;',document.text)
        self.assertNotIn('<script>alert(1)</script>',document.text)
        self.assertIn('window.print()',document.text)
        body['state']['REC'][0]['qty']*=2
        self.assertEqual(self.client.put('/api/state',headers=self.h,json=body).status_code,200)
        self.assertEqual(self.client.get('/api/reports/'+rid).json(),report)
        self.assertEqual(len(self.client.get('/api/reports').json()),1)
        self.login('two')
        self.assertEqual(self.client.get('/api/reports').json(),[])
        self.assertEqual(self.client.get('/api/reports/'+rid).status_code,404)
        self.assertEqual(self.client.get('/reports/'+rid).status_code,404)

    def test_annual_report_boundaries_and_validation(self):
        self.login();self.initialize()
        for payload in ({'year':1899},{'year':2025,'site':'other'},{'year':2199},{'year':2025,'notes':{'unknown':'x'}}):
            self.assertEqual(self.client.post('/api/reports',headers=self.h,json=payload).status_code,422)
        report=self.client.post('/api/reports',headers=self.h,json={'year':2025,'site':'S1'}).json()
        self.assertEqual(len(report['sites']),1)
        self.assertAlmostEqual(report['result']['locationKg'],self.client.get('/api/summary?year=2025&site=S1').json()['locationKg'])
        self.assertIn('No informado',self.client.get('/reports/'+report['id']).text)

    def test_annual_report_approval(self):
        from backend.reports import FIELDS
        self.login();body=self.initialize()
        draft=self.client.post('/api/reports',headers=self.h,json={'year':2025}).json()
        self.assertEqual(self.client.post('/api/reports/'+draft['id']+'/approve',headers=self.h).status_code,422)
        body['state']['demoRecordIds']=[]
        self.assertEqual(self.client.put('/api/state',headers=self.h,json=body).status_code,200)
        report=self.client.post('/api/reports',headers=self.h,json={'year':2025,'notes':{k:'Declaración revisada de prueba' for k in FIELDS}}).json()
        response=self.client.post('/api/reports/'+report['id']+'/approve',headers=self.h)
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(response.json()['approvedAt'])
        self.assertEqual(response.json()['result'],report['result'])
        self.assertIn('Aprobado internamente',self.client.get('/reports/'+report['id']).text)
        self.login('two')
        self.assertEqual(self.client.post('/api/reports/'+report['id']+'/approve',headers=self.h).status_code,404)

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
        with db(SYSTEM) as s:s.execute('UPDATE carbon_sessions SET expires=0')
        self.assertEqual(self.client.get('/api/state').status_code,401)
        for _ in range(10):
            self.assertEqual(self.client.post('/api/login',headers=self.h,json={'username':'unknown','password':'bad'}).status_code,401)
        self.assertEqual(self.client.post('/api/login',headers=self.h,json={'username':'unknown','password':'bad'}).status_code,429)

    def test_bootstrap_does_not_reset_existing_account(self):
        with patch.dict(os.environ, {'CARBON_BOOTSTRAP_PASSWORD_HASH':hash_password('first-password-123')}):
            bootstrap_account()
        with patch.dict(os.environ, {'CARBON_BOOTSTRAP_PASSWORD_HASH':hash_password('second-password-456')}):
            bootstrap_account()
        with db(SYSTEM) as s:
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
        with db(SYSTEM) as s:  # the queryable copy follows the factor change
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
        with db(SYSTEM) as s:  # write the inventory in the pre-split format, as production has it
            user=s.execute("SELECT id FROM carbon_accounts WHERE username='one'").fetchone()['id']
            s.execute('UPDATE carbon_states SET body=? WHERE account=?',(json.dumps(current['state']),user))
            s.execute('UPDATE carbon_records SET seq=NULL WHERE account=?',(user,))
            s.execute("DELETE FROM carbon_entities WHERE account=? AND kind='MOV'",(user,))
        self.assertEqual(self.paged(),{'revision':current['revision'],'state':current['state']})
        self.assertEqual(self.client.get('/api/state').json(),current)
        with db(SYSTEM) as s:
            body=decode(s.execute('SELECT body FROM carbon_states WHERE account=?',(user,)).fetchone()['body'])
            backups=s.execute('SELECT body FROM carbon_state_backups WHERE account=?',(user,)).fetchall()
        self.assertNotIn('REC',body)
        self.assertEqual([decode(b['body']) for b in backups],[current['state']])

    def test_downloads_match_the_whole_inventory(self):
        self.login()
        self.assertEqual(self.client.get('/api/export').json(),{'revision':0,'state':None,'updated':None})
        current=self.initialize()
        big=copy.deepcopy(current['state'])  # several streamed chunks and database batches
        for i in range(3):
            big['REC']+=[dict(r,rid=f'{r["rid"]}-{i}',pair=None) for r in current['state']['REC']]
        self.assertEqual(self.client.put('/api/state',headers=self.h,json={'revision':current['revision'],'state':big}).status_code,200)
        current=self.client.get('/api/state').json()
        self.assertEqual(self.client.get('/api/export').json(),current)
        r=self.client.get('/api/inventory.csv')
        self.assertIn('attachment',r.headers['content-disposition'])
        out=io.StringIO(newline='');writer=csv.writer(out)
        fields=['rid','p','site','scope','cat','factor','qty','unit','kg','bio','source'];writer.writerow(fields)
        for row in current['state']['REC']:
            values=[row.get(f,'') for f in fields]
            writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@','\t','\r')) else v for v in values])
        self.assertEqual(r.content.decode('utf-8'),'﻿'+out.getvalue())

    def test_closed_year_is_frozen_until_an_admin_reopens_it(self):
        self.login();current=self.initialize();state=current['state']
        before=self.client.get('/api/summary?year=2024').json()
        site=state['SITES'][0]['id'];before_site=self.client.get(f'/api/summary?year=2024&site={site}').json()
        self.assertEqual(self.client.post('/api/closures',headers=self.h,json={'year':1999}).status_code,422)
        r=self.client.post('/api/closures',headers=self.h,json={'year':2024})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['closedBy'],'one')
        self.assertEqual(self.client.post('/api/closures',headers=self.h,json={'year':2024}).status_code,409)
        self.assertEqual([c['year'] for c in self.client.get('/api/closures').json()],[2024])
        closed=self.client.get('/api/summary?year=2024').json()
        self.assertEqual(closed.pop('closed')['closedBy'],'one');self.assertEqual(closed,before)
        i24=next(i for i,r in enumerate(state['REC']) if r['p'].startswith('2024'))
        i25=next(i for i,r in enumerate(state['REC']) if r['p'].startswith('2025'))
        def attempt(change):
            after=copy.deepcopy(current['state']);change(after)
            return self.client.post('/api/state/changes',headers=self.h,json=self.diff(current,after))
        def edit(s):s['REC'][i24]['qty']+=1
        def delete(s):s['REC'].pop(i24)
        def add(s):s['REC'].append(dict(s['REC'][i24],rid='R99100',pair=None))
        def move(s):s['REC'][i25]['p']=s['REC'][i24]['p']
        for change in (edit,delete,add,move):
            with self.subTest(change=change.__name__):
                r=attempt(change);self.assertEqual(r.status_code,423,r.text);self.assertIn('2024',r.json()['detail'])
        self.assertEqual(self.client.get('/api/state').json(),current)
        # What the server derives from the factor is ignored, and 2025 stays editable.
        def derived_and_open(s):s['REC'][i24]['kg']=1.0;s['REC'][i25]['qty']+=1
        self.assertEqual(attempt(derived_and_open).status_code,200)
        current=self.client.get('/api/state').json()
        self.assertEqual(current['state']['REC'][i24],state['REC'][i24])
        # A factor correction changes open years only; the closed year keeps its records and results.
        factor=state['REC'][i24]['factor'];full=copy.deepcopy(current['state'])
        next(f for f in full['FACTORS'] if f['id']==factor)['v']*=2
        r=self.client.put('/api/state',headers=self.h,json={'revision':current['revision'],'state':full})
        self.assertEqual(r.status_code,200,r.text)
        current=self.client.get('/api/state').json()
        self.assertEqual(current['state']['REC'][i24],state['REC'][i24])
        changed=[r for r in current['state']['REC'] if r['factor']==factor and r['p'].startswith('2025')]
        self.assertTrue(changed and all(abs(r['kg']-r['qty']*next(f for f in full['FACTORS'] if f['id']==factor)['v'])<1e-6 for r in changed))
        closed=self.client.get('/api/summary?year=2024').json();closed.pop('closed');self.assertEqual(closed,before)
        closed_site=self.client.get(f'/api/summary?year=2024&site={site}').json();closed_site.pop('closed');self.assertEqual(closed_site,before_site)
        # Reopening: only an administrator working inside the account, with a reason.
        self.assertEqual(self.client.post('/api/closures/2024/reopen',headers=self.h,json={'reason':'Corrección'}).status_code,403)
        with db(SYSTEM) as s:
            s.execute("UPDATE carbon_accounts SET role='admin' WHERE username='two'")
            one=s.execute("SELECT id FROM carbon_accounts WHERE username='one'").fetchone()['id']
        self.login('two')
        self.assertEqual(self.client.post(f'/api/admin/accounts/{one}/impersonate',headers=self.h).status_code,200)
        self.assertEqual(self.client.post('/api/closures/2024/reopen',headers=self.h,json={'reason':''}).status_code,422)
        r=self.client.post('/api/closures/2024/reopen',headers=self.h,json={'reason':'Factor de red corregido'})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.client.get('/api/closures').json(),[])
        audit=self.client.get('/api/audit').json()
        reopen=next(e for e in audit if e['action']=='reopen_year')['details']
        self.assertEqual((reopen['reason'],reopen['by'],reopen['closedResults']['tCO2e']),('Factor de red corregido','Administrador de Invenzis',before['tCO2e']))
        self.assertEqual(attempt(edit).status_code,200)

    def test_factor_matching_endpoint_and_approval(self):
        self.assertEqual(self.client.post('/api/factors/match',headers=self.h,json={'items':[{'text':'Vuelo'}]}).status_code,401)
        self.login()
        self.assertEqual(self.client.post('/api/factors/match',headers=self.h,json={'items':[{'text':'Vuelo'}]}).status_code,404)
        current=self.initialize()
        self.assertEqual(self.client.post('/api/factors/match',headers=self.h,json={'items':[]}).status_code,422)
        r=self.client.post('/api/factors/match',headers=self.h,json={'items':[{'text':'Vuelo a Houston','scope':3,'cat':6},{'text':'Pintura','scope':3,'cat':1,'unit':'L'}]})
        self.assertEqual(r.status_code,200,r.text)
        body=r.json();self.assertEqual(body['engine'],'python')
        self.assertEqual(body['results'][0]['factor'],'FE-060');self.assertIsNone(body['results'][1]['factor'])
        self.assertEqual(self.client.get('/health').json()['similarity'],'python')
        self.assertIn('FE-060',self.client.get('/api/factors/terms').json())
        # Terms the organisation adds (or learns from approvals) are used for the next match.
        state=copy.deepcopy(current['state'])
        next(f for f in state['FACTORS'] if f['id']=='FE-031')['alias']=['Bulón inox M10']
        rec=next(r for r in state['REC'] if r['type']=='purchase' and r['p'].startswith('2025'))
        rec['fm']={'text':'Bulón inox M10','auto':rec['factor'],'pct':41,'tie':False,'cands':[{'factor':rec['factor'],'pct':41}],'ok':False,'engine':'python'}
        saved=self.client.post('/api/state/changes',headers=self.h,json=self.diff(current,state))
        self.assertEqual(saved.status_code,200,saved.text)
        hit=self.client.post('/api/factors/match',headers=self.h,json={'items':[{'text':'bulon inox m10','scope':3,'cat':1,'unit':'kg'}]}).json()['results'][0]
        self.assertEqual((hit['factor'],hit['pct']),('FE-031',100))
        # A year with unapproved automatic assignments cannot be closed.
        blocked=self.client.post('/api/closures',headers=self.h,json={'year':2025})
        self.assertEqual(blocked.status_code,409);self.assertIn('sin aprobar',blocked.json()['detail'])
        before=self.paged();after=copy.deepcopy(before['state'])
        next(r for r in after['REC'] if r['rid']==rec['rid'])['fm'].update(ok=True,by='one',at='2026-09-23T12:00:00Z')
        self.assertEqual(self.client.post('/api/state/changes',headers=self.h,json=self.diff(before,after)).status_code,200)
        self.assertEqual(self.client.post('/api/closures',headers=self.h,json={'year':2025}).status_code,200)
        # Malformed assignments are rejected.
        before=self.paged();after=copy.deepcopy(before['state'])
        next(r for r in after['REC'] if r['p'].startswith('2024'))['fm']={'pct':140}
        self.assertEqual(self.client.post('/api/state/changes',headers=self.h,json=self.diff(before,after)).status_code,422)

    def test_admin_switches_sections_and_integrations_per_account(self):
        create_user('three','Empresa tres','test-password-123')
        with db(SYSTEM) as s:
            s.execute("UPDATE carbon_accounts SET role='admin' WHERE username='two'")
            one=s.execute("SELECT id FROM carbon_accounts WHERE username='one'").fetchone()['id']
            three=s.execute("SELECT id FROM carbon_accounts WHERE username='three'").fetchone()['id']
        settings=f'/api/admin/accounts/{one}/settings'
        self.login(); self.initialize()
        me=self.client.get('/api/me').json()['features']
        self.assertFalse(me['sections']['energia'])  # hidden until an administrator turns it on
        self.assertTrue(me['sections']['reportes'] and me['integrations']['hub'])
        auth={'Authorization':'Bearer '+self.client.post('/api/tokens',headers=self.h,json={'label':'Hub'}).json()['token']}
        self.assertEqual(self.client.put(settings,headers=self.h,json={'sections':{'energia':True}}).status_code,403)
        self.login('two')
        cfg=self.client.get(settings).json()
        self.assertEqual((len(cfg['tokens']),next(x for x in cfg['sections'] if x['key']=='energia')['on']),(1,False))
        for bad in ({'sections':{'dash':False}},{'integrations':{'nope':True}},{'sections':{'energia':'si'}}):
            self.assertEqual(self.client.put(settings,headers=self.h,json=bad).status_code,422)
        r=self.client.put(settings,headers=self.h,json={'sections':{'energia':True,'reportes':False},'integrations':{'hub':False,'sap':False}})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.client.post(f'/api/admin/accounts/{one}/tokens',headers=self.h,json={'label':'Hub'}).status_code,403)
        # The client sees the change and the server enforces it.
        self.login()
        me=self.client.get('/api/me').json()['features']
        self.assertEqual((me['sections']['energia'],me['sections']['reportes'],me['integrations']['sap']),(True,False,False))
        self.assertEqual(self.client.get('/api/reports').status_code,403)
        self.assertEqual(self.client.get('/api/tokens').status_code,403)
        with TestClient(app) as anon:
            self.assertEqual(anon.get('/api/link/sites',headers=auth).status_code,403)
        with db(SYSTEM) as s:
            self.assertEqual(features.of(s,three),features.parse('{}'))  # other accounts keep the defaults
        # Back on: the old token works again; the administrator mints and revokes tokens for the client.
        self.login('two')
        self.client.put(settings,headers=self.h,json={'integrations':{'hub':True}})
        minted={'Authorization':'Bearer '+self.client.post(f'/api/admin/accounts/{one}/tokens',headers=self.h,json={'label':'Hub'}).json()['token']}
        with TestClient(app) as anon:
            self.assertEqual(anon.get('/api/link/sites',headers=auth).status_code,200)
            self.assertEqual(anon.get('/api/link/sites',headers=minted).status_code,200)
            tokens=self.client.get(settings).json()['tokens']
            self.assertEqual(len(tokens),2)
            for t in tokens:self.client.delete(f'/api/admin/accounts/{one}/tokens/{t["id"]}',headers=self.h)
            self.assertEqual(anon.get('/api/link/sites',headers=minted).status_code,401)

    def test_uploaded_documents_open_from_their_record(self):
        self.login(); self.initialize(); before=self.paged()
        pdf=b'%PDF-1.4\n% factura de prueba\n'
        upload=lambda name,data:self.client.post('/api/parse-document',headers=self.h,data={'kind':'auto'},files={'file':(name,data,'application/pdf')}).json()
        kept,discarded=upload('factura agosto.pdf',pdf)['document'],upload('otra.pdf',pdf)['document']
        self.assertIsNone(upload('pagina.pdf',b'<html><script>alert(1)</script>')['document'])  # only real PDF, PNG or JPEG bytes
        r=self.client.get('/api/documents/'+kept)
        self.assertEqual((r.status_code,r.content,r.headers['content-type']),(200,pdf,'application/pdf'))
        self.assertIn("filename*=UTF-8''factura%20agosto.pdf",r.headers['content-disposition'])
        # A saved record that names the file links it; files never linked are removed after a day.
        after=copy.deepcopy(before['state']);after['REC'][0]['origin']['files']=[kept]
        self.assertEqual(self.client.post('/api/state/changes',headers=self.h,json=self.diff(before,after)).status_code,200)
        with db(SYSTEM) as s:s.execute('UPDATE carbon_documents SET created=0')
        upload('nueva.pdf',pdf)
        self.assertEqual(self.client.get('/api/documents/'+kept).status_code,200)
        self.assertEqual(self.client.get('/api/documents/'+discarded).status_code,404)
        self.login('two')
        self.assertEqual(self.client.get('/api/documents/'+kept).status_code,404)

    def test_every_connection_names_its_account(self):
        with self.assertRaises(TypeError), db():pass
        for bad in ('', None, 7):
            with self.subTest(account=bad), self.assertRaises(ValueError), db(bad):pass

    def test_vercel_database_configuration(self):
        with patch.dict(os.environ, {'VERCEL':'1','DATABASE_URL':'postgresql://integration-test'}):
            self.assertEqual(database_url(),'postgresql://integration-test')
        with patch.dict(os.environ, {'VERCEL':'1','DATABASE_URL':''}):
            with self.assertRaises(RuntimeError), db(SYSTEM):pass


if __name__=='__main__':unittest.main()
