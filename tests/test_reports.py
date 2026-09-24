"""Report regressions using isolated test inventories."""
import unittest
import test_carbon as fixture
from backend.reports import FIELDS, bars


class ReportRegressions(unittest.TestCase):
    setUp = fixture.ApiTests.setUp
    use_database = fixture.ApiTests.use_database
    tearDown = fixture.ApiTests.tearDown
    login = fixture.ApiTests.login
    initialize = fixture.ApiTests.initialize

    def test_whitespace_is_missing_and_history_identifies_scope(self):
        self.login()
        body=self.initialize()
        body['state']['demoRecordIds']=[]
        self.assertEqual(self.client.put('/api/state',headers=self.h,json=body).status_code,200)
        response=self.client.post('/api/reports',headers=self.h,json={
            'year':2025,'site':'S1','notes':{k:'   ' for k in FIELDS}})
        self.assertEqual(response.status_code,200,response.text)
        report=response.json()
        self.assertEqual(report['notes']['activity'],'')
        self.assertEqual(self.client.post('/api/reports/'+report['id']+'/approve',headers=self.h).status_code,422)
        history=self.client.get('/api/reports').json()
        self.assertEqual(history[0]['site'],'S1')
        self.assertIsNone(history[0]['approvedAt'])

    def test_closed_year_keeps_original_factors_and_uncertainty(self):
        self.login()
        self.initialize()
        closed=self.client.post('/api/closures',headers=self.h,json={'year':2025})
        self.assertEqual(closed.status_code,200,closed.text)
        original=self.client.get('/api/summary?year=2025').json()
        body=self.client.get('/api/state').json()
        for f in body['state']['FACTORS']:
            f['v']*=2
        body['state']['settings']['uqOverrides']={
            r.get('source','').split(' · ')[0]:{'pct':500} for r in body['state']['REC']}
        self.assertEqual(self.client.put('/api/state',headers=self.h,json=body).status_code,200)
        response=self.client.post('/api/reports',headers=self.h,json={'year':2025})
        self.assertEqual(response.status_code,200,response.text)
        report=response.json()
        self.assertEqual(report['result']['uncertainty'],original['uncertainty'])
        self.assertAlmostEqual(report['result']['locationKg'],original['locationKg'])
        self.assertAlmostEqual(sum(report['iso'].values()),original['locationKg'])
        site=self.client.post('/api/reports',headers=self.h,json={'year':2025,'site':'S1'}).json()
        self.assertIsNone(site['result']['uncertainty']['pct'])
        self.assertIn('No disponible por sitio',self.client.get('/reports/'+site['id']).text)

    def test_missing_months_are_not_zero_in_chart(self):
        chart=bars(['Enero','Febrero'],[None,0],'Prueba')
        self.assertIn('Sin datos',chart)
        self.assertEqual(chart.count('<rect'),1)
        self.assertIn('0,00',chart)
        self.assertIn('#4B7F55',chart)
