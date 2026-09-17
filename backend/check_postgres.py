"""Non-destructive smoke test: creates and removes only its own disposable account."""
import json
import os
import secrets
import uuid
from fastapi.testclient import TestClient
from .app import app, ROOT
from .manage import create_user
from .storage import db


def main():
    if not os.getenv('CARBON_DATABASE_URL'):
        raise RuntimeError('Requiere CARBON_DATABASE_URL de una base PostgreSQL dedicada.')
    username='smoke-'+uuid.uuid4().hex
    password=secrets.token_urlsafe(24)
    user=None
    try:
        with TestClient(app) as client:
            create_user(username,'Prueba PostgreSQL',password)
            with db() as s:
                user=s.execute('SELECT id FROM carbon_accounts WHERE username=?',(username,)).fetchone()['id']
            h={'X-IVZ-Carbon':'1'}
            r=client.post('/api/login',headers=h,json={'username':username,'password':password});r.raise_for_status()
            r=client.post('/api/initialize',headers=h,json={'mode':'demo'});r.raise_for_status()
            body=r.json()
            body['state']['SITES'][0]['name']='Persistencia PostgreSQL'
            r=client.put('/api/state',headers=h,json=body);r.raise_for_status()
            assert r.json()['revision']==2
            assert client.put('/api/state',headers=h,json=body).status_code==409
            assert client.get('/api/state').json()['state']['SITES'][0]['name']=='Persistencia PostgreSQL'
            seed=json.loads((ROOT/'seed.json').read_text(encoding='utf8'))
            assert client.get('/api/records').json()['total']==len(seed['REC'])
            with db() as s:
                assert s.execute('SELECT jsonb_typeof(body) AS kind FROM carbon_states WHERE account=?',(user,)).fetchone()['kind']=='object'
            print('PostgreSQL OK: JSONB, persistencia, registros y control de concurrencia.')
    finally:
        if user:
            with db() as s:
                for table in ('carbon_events','carbon_entities','carbon_records','carbon_states','carbon_sessions'):
                    s.execute(f'DELETE FROM {table} WHERE account=?',(user,))
                s.execute('DELETE FROM carbon_accounts WHERE id=?',(user,))
                s.execute('DELETE FROM carbon_login_limits WHERE username=?',(username,))


if __name__=='__main__':main()
