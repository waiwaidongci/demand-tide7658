"""Full HTTP-stack test using FastAPI's TestClient with a fake Redis.

Exercises authentication, RBAC, the preview/commit/replay endpoints,
version-aware single-row upserts and error handling over real HTTP.
MySQL is replaced by SQLite; Redis (rate limiting / queue) is faked because
the stack only needs incr/expire/ping/rpush/blpop here.

Run:
  DATABASE_URL=sqlite:////tmp/tide_http.sqlite3 \
  PYTHONPATH=/tmp/pylibs:. python3 tests/test_import_http.py
"""
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DATABASE_URL', 'sqlite:////tmp/tide_http.sqlite3')
os.environ.setdefault('SEED_DEMO', 'false')
if os.path.exists('/tmp/tide_http.sqlite3'):
    os.remove('/tmp/tide_http.sqlite3')

# --------------------------------------------------------------------------- #
# Fake redis (must be installed before src.config.queue is imported)
# --------------------------------------------------------------------------- #
fake_redis = types.ModuleType('redis')
fake_redis.__path__ = []


class FakeRedis:
    def __init__(self, *a, **k):
        self.kv = {}

    @classmethod
    def from_url(cls, *a, **k):
        return cls()

    def incr(self, key):
        self.kv[key] = self.kv.get(key, 0) + 1
        return self.kv[key]

    def expire(self, *a):
        return True

    def ping(self):
        return True

    def rpush(self, *a):
        return 1

    def blpop(self, *a, **k):
        return None


class FakeRedisError(Exception):
    pass


fake_redis.Redis = FakeRedis
fake_redis.RedisError = FakeRedisError
sys.modules['redis'] = fake_redis
fake_exc = types.ModuleType('redis.exceptions')
fake_exc.RedisError = FakeRedisError
sys.modules['redis.exceptions'] = fake_exc
fake_redis.exceptions = fake_exc

# --------------------------------------------------------------------------- #
from fastapi.testclient import TestClient
from src.config.database import Base, engine, SessionLocal
from src.models import User, Series
from src.utils.security import hash_password

Base.metadata.create_all(engine)
with SessionLocal() as db:
    db.add(User(email='analyst@tide.demo', name='分析师', role='analyst',
                password_hash=hash_password('Tide2026!')))
    db.add(User(email='viewer@tide.demo', name='查看员', role='viewer',
                password_hash=hash_password('Tide2026!')))
    db.add(Series(code='e2e', name='端到端序列', city='杭州', product='玫瑰',
                  source_file='e2e.csv'))
    db.commit()

from src.main import app

client = TestClient(app)
PASS, FAILS = 0, []


def check(name, cond, detail=''):
    global PASS
    if cond:
        PASS += 1
        print(f'  PASS  {name}')
    else:
        FAILS.append((name, detail))
        print(f'  FAIL  {name}  {detail}')


def login(email):
    r = client.post('/auth/login', json={'email': email, 'password': 'Tide2026!'})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['access_token']}"}


def main():
    headers = login('analyst@tide.demo')
    viewer_headers = login('viewer@tide.demo')
    sid = client.get('/series', headers=headers).json()[0]['id']

    payload = 'day,value,report_version\n' + '\n'.join([
        '2026-03-01,100,1', '2026-03-02,110,1', 'nope,1,1',
        '2026-03-03,120,1', '2026-03-03,125,1',
    ])

    r = client.post(f'/series/{sid}/imports/preview', headers=headers, json={'content': payload})
    check('preview 200 with reconciled counts', r.status_code == 200 and r.json()['summary'] == {
        'total': 5, 'inserted': 2, 'updated': 0, 'skipped': 0, 'conflict': 2, 'bad': 1,
        'actions': {'inserted': 2, 'conflict': 2, 'bad': 1}}, r.text)

    r = client.post(f'/series/{sid}/imports', headers=headers,
                    json={'content': payload, 'idem_key': 'e2e-batch-1', 'filename': 'r1.csv'})
    check('commit 201, not a replay', r.status_code == 201 and r.json()['replayed'] is False)

    r = client.post(f'/series/{sid}/imports', headers=headers,
                    json={'content': payload, 'idem_key': 'e2e-batch-1', 'filename': 'r1.csv'})
    check('replay returns first result, no new rows',
          r.status_code == 201 and r.json()['replayed'] is True)
    obs = client.get(f'/series/{sid}/observations', headers=headers).json()
    check('two observations, both v1',
          len(obs) == 2 and all(o['report_version'] == 1 for o in obs))

    check('higher version corrects',
          client.post(f'/series/{sid}/imports', headers=headers,
                      json={'content': 'day,value,report_version\n2026-03-01,180,2\n',
                            'idem_key': 'e2e-batch-2'}).json()['summary']['updated'] == 1)
    check('older version skipped',
          client.post(f'/series/{sid}/imports', headers=headers,
                      json={'content': 'day,value,report_version\n2026-03-01,100,1\n',
                            'idem_key': 'e2e-old'}).json()['summary']['skipped'] == 1)
    conflict = client.post(f'/series/{sid}/imports', headers=headers,
                           json={'content': 'day,value,report_version\n2026-03-01,999,2\n',
                                 'idem_key': 'e2e-conflict'}).json()
    check('same-version different value quarantined with reason',
          conflict['summary']['conflict'] == 1 and bool(conflict['rows'][0]['reason']))
    fixed = [o for o in client.get(f'/series/{sid}/observations', headers=headers).json()
             if o['day'] == '2026-03-01'][0]
    check('ledger value/version stay at corrected state',
          fixed['value'] == 180 and fixed['report_version'] == 2)

    batches = client.get(f'/series/{sid}/imports', headers=headers)
    check('batch history lists 4 batches', batches.status_code == 200 and len(batches.json()) == 4)
    bid = batches.json()[0]['id']
    detail = client.get(f'/series/{sid}/imports/{bid}', headers=headers)
    check('batch detail retrievable', detail.status_code == 200
          and detail.json()['batch']['id'] == bid)

    check('single-row higher version upsert 201',
          client.post(f'/series/{sid}/observations', headers=headers,
                      json={'day': '2026-03-02', 'value': 115, 'report_version': 3}).status_code == 201)
    check('single-row same version different value 409',
          client.post(f'/series/{sid}/observations', headers=headers,
                      json={'day': '2026-03-02', 'value': 116, 'report_version': 3}).status_code == 409)
    check('single-row older version 409',
          client.post(f'/series/{sid}/observations', headers=headers,
                      json={'day': '2026-03-02', 'value': 1, 'report_version': 1}).status_code == 409)

    check('viewer cannot preview (403)',
          client.post(f'/series/{sid}/imports/preview', headers=viewer_headers,
                      json={'content': payload}).status_code == 403)
    check('viewer can read batch history (200)',
          client.get(f'/series/{sid}/imports', headers=viewer_headers).status_code == 200)

    check('unrecognized header -> 400',
          client.post(f'/series/{sid}/imports/preview', headers=headers,
                      json={'content': 'foo,bar\n1,2\n'}).status_code == 400)
    check('empty idem_key -> 422',
          client.post(f'/series/{sid}/imports', headers=headers,
                      json={'content': payload, 'idem_key': ''}).status_code == 422)
    check('unauthenticated request -> 401',
          client.get(f'/series/{sid}/imports').status_code == 401)

    print()
    print(f'{PASS} checks passed, {len(FAILS)} failed')
    if FAILS:
        for name, detail in FAILS:
            print('FAILED:', name, detail)
        sys.exit(1)


if __name__ == '__main__':
    main()
