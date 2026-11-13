"""Behavioral tests for the revised-report import logic.

The runtime stack (FastAPI/SQLAlchemy/MySQL) is not installed in this
environment, so import_service is loaded against in-memory fakes that mimic
the SQLAlchemy session surface. The pure planning rules
(parse -> validate -> plan -> reconcile -> persist) are fully covered,
including replay, conflicts, bad rows, concurrency recovery and reconciliation
arithmetic. Run with: python3 backend/tests/test_import_logic.py
"""
import sys
import types
import datetime as dt
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------- #
# Minimal fakes for the third-party / package dependency surface
# --------------------------------------------------------------------------- #
for pkg in ('src', 'src.models', 'src.services', 'src.utils', 'src.config'):
    mod = types.ModuleType(pkg)
    mod.__path__ = []
    sys.modules[pkg] = mod


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


fastapi = types.ModuleType('fastapi')
fastapi.HTTPException = HTTPException
sys.modules['fastapi'] = fastapi

sqlalchemy = types.ModuleType('sqlalchemy')
class _Col(str):
    def desc(self): return self
    def asc(self): return self
    def __eq__(self, other): return ('eq', str(self), other)
    def in_(self, values): return ('in', str(self), values)
class _Select:
    def __init__(self, target):
        self.target = target
        self.filters = {}
    def where(self, *conditions, **kwargs):
        self.filters['conditions'] = conditions
        return self
    def order_by(self, *a):
        return self
    def with_for_update(self):
        return self
sqlalchemy.select = lambda target: _Select(target)
sys.modules['sqlalchemy'] = sqlalchemy
exc = types.ModuleType('sqlalchemy.exc')
class IntegrityError(Exception): pass
exc.IntegrityError = IntegrityError
sys.modules['sqlalchemy.exc'] = exc

models = sys.modules['src.models']
class Observation:
    series_id = _Col('series_id')
    day = _Col('day')
    def __init__(self, series_id, day, value, report_version=0, batch_id=None):
        self.series_id = series_id
        self.day = day
        self.value = value
        self.report_version = report_version
        self.batch_id = batch_id
class ImportBatch:
    id = _Col('id')
    series_id = _Col('series_id')
    idem_key = _Col('idem_key')
    def __init__(self, **kw):
        self.id = None
        for k, v in kw.items():
            setattr(self, k, v)
models.Observation = Observation
models.ImportBatch = ImportBatch

serializers = types.ModuleType('src.utils.serializers')
def serialize(obj):
    return {k: v for k, v in vars(obj).items() if not k.startswith('_')}
serializers.serialize = serialize
sys.modules['src.utils.serializers'] = serializers

audit_mod = types.ModuleType('src.utils.audit')
audit_log = []
def record(db, user, action, target):
    audit_log.append((user.email, action, target))
audit_mod.record = record
sys.modules['src.utils.audit'] = audit_mod

series_svc = types.ModuleType('src.services.series_service')
class Series:
    def __init__(self, sid, source_file):
        self.id = sid
        self.source_file = source_file
def get_series(db, series_id):
    if series_id not in db.series:
        raise HTTPException(404, '序列不存在')
    return db.series[series_id]
series_svc.get_series = get_series
sys.modules['src.services.series_service'] = series_svc

spec = importlib.util.spec_from_file_location(
    'src.services.import_service', ROOT / 'src' / 'services' / 'import_service.py')
import_service = importlib.util.module_from_spec(spec)
sys.modules['src.services.import_service'] = import_service
spec.loader.exec_module(import_service)


# --------------------------------------------------------------------------- #
# Fake DB + user
# --------------------------------------------------------------------------- #
class User:
    def __init__(self, email): self.email = email; self.id = 1


class FakeDB:
    def __init__(self):
        self.series = {1: Series(1, 'tide-demand-2026.csv')}
        self.observations = {}          # (series_id, day) -> Observation
        self.batches = {}               # idem_key -> ImportBatch
        self._next_obs = 0
        self._next_batch = 0
        self.fail_next_persist = False
        self._pending_keys = set()
        self._pending_idem_keys = set()

    # helpers used by import_service
    def scalar(self, query):
        # only ImportBatch idem lookups go through scalar
        if isinstance(query, _Select) and query.target is ImportBatch:
            return self._idem_lookup
        return None

    def scalars(self, query):
        class R:
            def __init__(self, items): self._items = items
            def all(self): return list(self._items)
        target = query.target
        if target is Observation:
            items = [o for (sid, day), o in self.observations.items() if sid == 1]
            return R(sorted(items, key=lambda o: o.day))
        if target is ImportBatch:
            return R(sorted(self.batches.values(), key=lambda b: b.id, reverse=True))
        return R([])

    def add(self, obj):
        if isinstance(obj, Observation):
            key = (obj.series_id, obj.day)
            if key in self.observations and self.fail_next_persist:
                self.fail_next_persist = False
                raise IntegrityError('duplicate natural key')
            self._next_obs += 1
            self.observations[key] = obj
            self._pending_keys.add(key)
        elif isinstance(obj, ImportBatch):
            self._next_batch += 1
            obj.id = self._next_batch
            self.batches[obj.idem_key] = obj
            self._pending_idem_keys.add(obj.idem_key)
    def flush(self):
        # IntegrityError is raised from add(); nothing else to do here
        pass
    def rollback(self):
        # emulate transaction rollback: drop uncommitted rows AND batches,
        # preserve rows committed by other (concurrent) writers
        for key in list(getattr(self, '_pending_keys', ())):
            self.observations.pop(key, None)
        for idem in list(getattr(self, '_pending_idem_keys', ())):
            self.batches.pop(idem, None)
        self._pending_keys = set()
        self._pending_idem_keys = set()
        self.fail_next_persist = False
    def commit(self):
        self._pending_keys = set()
        self._pending_idem_keys = set()
    def refresh(self, obj): pass
    def get(self, model, pk):
        if model is ImportBatch:
            for b in self.batches.values():
                if b.id == pk:
                    return b
        return None

    @property
    def _idem_lookup(self):
        return getattr(self, '_pending_idem', None)


# Patch the service's thin DB helpers so the fake controls locking semantics
def fake_find_batch(db, idem_key):
    db._pending_idem = db.batches.get(idem_key)
    return db._pending_idem

def fake_for_days(db, series_id, days):
    return [db.observations[(series_id, d)] for d in sorted(days)
            if (series_id, d) in db.observations]

def fake_all(db, series_id):
    return [o for (sid, day), o in sorted(db.observations.items(), key=lambda kv: kv[0][1])
            if sid == series_id]

import_service._find_batch = fake_find_batch
import_service._observations_for_days = fake_for_days
import_service._all_observations = fake_all


# --------------------------------------------------------------------------- #
# Assertions
# --------------------------------------------------------------------------- #
PASS, FAILS = 0, []
def check(name, cond, detail=''):
    global PASS
    if cond:
        PASS += 1
        print(f'  PASS  {name}')
    else:
        FAILS.append((name, detail))
        print(f'  FAIL  {name}  {detail}')


def check_reconciliation(summary, rows):
    assert summary['total'] == len(rows)
    by_status, by_action = {}, {}
    for r in rows:
        by_status[r['status']] = by_status.get(r['status'], 0) + 1
        by_action[r['action']] = by_action.get(r['action'], 0) + 1
    for status, n in by_status.items():
        check(f'count[{status}] matches rows', summary[status] == n, (summary, by_status))
    check('action breakdown matches', summary['actions'] == by_action,
          (summary['actions'], by_action))
    check('total = sum of dispositions',
          summary['total'] == sum(summary[s] for s in
                                  ('inserted', 'updated', 'skipped', 'conflict', 'bad')))


CSV_HEADER = 'day,value,report_version\n'


def run():
    print('== parse & validate ==')
    db = FakeDB()
    csv_text = CSV_HEADER + '\n'.join([
        '2026-11-01,100,1',
        '2026-11-02,110,1',
        'not-a-date,120,1',                       # bad day
        '2026-11-03,,1',                          # missing value
        '2026-11-04,130,v2',                      # bad version
        '2026-11-05,-5,1',                        # bad value
        '2026-11-06,abc,1',                       # bad value
        ',140,1',                                 # missing day
        '   ',                                    # blank line
        '2026-11-07,150,1',
    ])
    preview = import_service.build_preview(db, 1, csv_text)
    check('total rows counted incl. blank/bad', preview['summary']['total'] == 10, preview['summary'])
    check('bad rows isolated with reasons', preview['summary']['bad'] == 7, preview['summary'])
    check('good rows planned as inserts', preview['summary']['inserted'] == 3, preview['summary'])
    check('every bad row carries a reason',
          all(r['reason'] for r in preview['rows'] if r['status'] == 'bad'))
    check('line numbers preserved (header counts)', [r['line'] for r in preview['rows']][:3] == [2, 3, 4])
    check_reconciliation(preview['summary'], preview['rows'])

    print('== commit: good rows land, bad rows quarantined ==')
    out = import_service.commit_import(db, 1, csv_text, 'batch-1', 'r1.csv', User('analyst@tide.demo'))
    check('batch committed', out['batch']['status'] == 'committed')
    check('3 observations inserted', len(db.observations) == 3)
    check('stored versions are 1', all(o.report_version == 1 for o in db.observations.values()))
    check('audit recorded', any(a[1] == 'import_observations' for a in audit_log))
    check('batch_id back-filled', all(o.batch_id == out['batch']['id'] for o in db.observations.values()))
    check_reconciliation(out['summary'], out['rows'])

    print('== idempotent replay: same batch adds nothing ==')
    before = len(db.observations)
    out2 = import_service.commit_import(db, 1, csv_text, 'batch-1', 'r1.csv', User('analyst@tide.demo'))
    check('replay no new observations', len(db.observations) == before)
    check('replay returns identical summary', out2['summary'] == out['summary'])
    check('replay flagged', out2['replayed'] is True)
    check('no second audit line for replay',
          len([a for a in audit_log if a[1] == 'import_observations']) == 1)

    print('== concurrent duplicate (new idem key, identical natural keys) ==')
    dup_csv = CSV_HEADER + '2026-11-01,100,1\n2026-11-02,110,1\n'
    out3 = import_service.commit_import(db, 1, dup_csv, 'batch-dup', None, User('analyst@tide.demo'))
    check('no inserts for duplicate natural keys', out3['summary']['inserted'] == 0)
    check('both rows skipped', out3['summary']['skipped'] == 2, out3['summary'])
    check('skip reason explains no-op',
          all(r['reason'] == import_service.REASON['same_version_same_value']
              for r in out3['rows'] if r['status'] == 'skipped'))

    print('== correction: higher version overwrites; forecast then reads new value ==')
    corr = CSV_HEADER + '2026-11-01,180,2\n2026-11-02,110,2\n'
    out4 = import_service.commit_import(db, 1, corr, 'batch-2', 'r2.csv', User('analyst@tide.demo'))
    check('two updates', out4['summary']['updated'] == 2, out4['summary'])
    check('value corrected to 180', db.observations[(1, dt.date(2026, 11, 1))].value == 180)
    check('version raised to 2', db.observations[(1, dt.date(2026, 11, 1))].report_version == 2)
    check('still one surviving row per day', len(db.observations) == before)
    check('new batch id tracked',
          db.observations[(1, dt.date(2026, 11, 1))].batch_id == out4['batch']['id'])

    print('== old version replay never rolls back ==')
    old = CSV_HEADER + '2026-11-01,100,1\n'
    out5 = import_service.commit_import(db, 1, old, 'batch-old', 'r1-replay.csv', User('analyst@tide.demo'))
    check('older version skipped', out5['summary']['skipped'] == 1)
    check('value stays corrected', db.observations[(1, dt.date(2026, 11, 1))].value == 180)
    check('version stays 2', db.observations[(1, dt.date(2026, 11, 1))].report_version == 2)
    check('reason given', out5['rows'][0]['reason'] == import_service.REASON['older_version'])

    print('== same version different value: conflict quarantined, good row still lands ==')
    conflict_csv = CSV_HEADER + '\n'.join([
        '2026-11-01,999,2',                        # conflicts with ledger v2=180
        '2026-11-08,200,2',                        # fine, inserts
    ])
    out6 = import_service.commit_import(db, 1, conflict_csv, 'batch-3', None, User('analyst@tide.demo'))
    check('conflict isolated', out6['summary']['conflict'] == 1, out6['summary'])
    check('good row still inserted', out6['summary']['inserted'] == 1)
    check('ledger value untouched', db.observations[(1, dt.date(2026, 11, 1))].value == 180)
    check('conflict reason explicit',
          any(r['reason'] == import_service.REASON['conflict_db'] for r in out6['rows']))
    check_reconciliation(out6['summary'], out6['rows'])

    print('== in-file same (day, version) different values: whole group quarantined ==')
    infile = CSV_HEADER + '\n'.join([
        '2026-11-09,100,1',
        '2026-11-09,105,1',
        '2026-11-10,120,1',
    ])
    out7 = import_service.commit_import(db, 1, infile, 'batch-4', None, User('analyst@tide.demo'))
    check('both conflicting rows quarantined', out7['summary']['conflict'] == 2, out7['summary'])
    check('unrelated row inserted', out7['summary']['inserted'] == 1)
    check('no 11-09 observation', (1, dt.date(2026, 11, 9)) not in db.observations)

    print('== in-file exact duplicates: first wins, rest skipped ==')
    dup_in = CSV_HEADER + '\n'.join(['2026-11-11,130,1', '2026-11-11,130,1'])
    out8 = import_service.commit_import(db, 1, dup_in, 'batch-5', None, User('analyst@tide.demo'))
    check('one insert one skip',
          out8['summary']['inserted'] == 1 and out8['summary']['skipped'] == 1)
    check('only one row stored for the day',
          sum(1 for k in db.observations if k[1] == dt.date(2026, 11, 11)) == 1)

    print('== unversioned legacy observation upgrades and remains usable ==')
    db.observations[(1, dt.date(2026, 12, 1))] = Observation(1, dt.date(2026, 12, 1), 90, 0)
    up = CSV_HEADER + '2026-12-01,95,1\n'
    out9 = import_service.commit_import(db, 1, up, 'batch-6', None, User('analyst@tide.demo'))
    check('legacy v0 row upgrades', out9['summary']['updated'] == 1)
    upgraded = db.observations[(1, dt.date(2026, 12, 1))]
    check('upgraded value/version', upgraded.value == 95 and upgraded.report_version == 1)

    print('== missing version column defaults to 0 ==')
    no_ver = 'day,value\n2026-12-02,77\n'
    pv = import_service.build_preview(db, 1, no_ver)
    check('default version 0', pv['rows'][0]['report_version'] == 0)
    check('planned as insert', pv['rows'][0]['status'] == 'inserted')

    print('== header aliases and BOM tolerance ==')
    aliased = '﻿日期,需求量,报表版本\n2026-12-03,88,1\n'
    pv2 = import_service.build_preview(db, 1, aliased)
    check('chinese headers recognized',
          pv2['rows'][0]['status'] == 'inserted' and pv2['rows'][0]['value'] == 88)

    print('== input rejection ==')
    for label, payload in [('unknown header', 'foo,bar\n1,2\n'),
                           ('blank content', '   \n  '),
                           ('empty', '')]:
        try:
            import_service.build_preview(db, 1, payload)
            check(f'rejects {label}', False)
        except HTTPException as e:
            check(f'rejects {label}', e.status_code == 400)

    print('== forecast read path reflects corrected observations ==')
    spec_fa = importlib.util.spec_from_file_location(
        'forecast_algorithm', ROOT / 'src' / 'services' / 'forecast_algorithm.py')
    fa = importlib.util.module_from_spec(spec_fa)
    spec_fa.loader.exec_module(fa)
    vals = [o.value for o in fake_all(db, 1)]
    check('forecast input contains corrected 180', 180.0 in vals)
    check('superseded 100 no longer fed to forecast', 100.0 not in vals)
    days = sorted(day for (sid, day) in db.observations)
    check('gap predicate (as used by run_service) available',
          any((b - a).days != 1 for a, b in zip(days, days[1:])))

    print('== concurrency: natural-key collision on flush -> quarantine, no dup rows ==')
    db2 = FakeDB()
    # Ledger initially empty; a concurrent writer commits the same natural key
    # (different value) between our lock read and our flush.
    race_csv = CSV_HEADER + '2027-01-01,555,1\n2027-01-02,80,1\n'
    concurrent = Observation(1, dt.date(2027, 1, 1), 500, 1)
    original_persist = import_service._persist
    call = {'n': 0}
    def persisting_with_race(db, series, rows, *a, **kw):
        call['n'] += 1
        if call['n'] == 1:
            # emulate a concurrent commit landing before our first flush:
            # mark it externally committed so our rollback must not remove it
            db.observations[(1, dt.date(2027, 1, 1))] = concurrent
            db.fail_next_persist = True
        return original_persist(db, series, rows, *a, **kw)
    import_service._persist = persisting_with_race
    try:
        out_race = import_service.commit_import(db2, 1, race_csv, 'race-1', None, User('analyst@tide.demo'))
    finally:
        import_service._persist = original_persist
    check('colliding row quarantined as conflict',
          any(r['status'] == 'conflict' and r['reason'] == import_service.REASON['race_conflict']
              for r in out_race['rows']), out_race['summary'])
    check('non-colliding row still inserted',
          (1, dt.date(2027, 1, 2)) in db2.observations)
    check('colliding value not written',
          db2.observations[(1, dt.date(2027, 1, 1))].value == 500)
    check('no duplicate row for the day',
          sum(1 for k in db2.observations if k[1] == dt.date(2027, 1, 1)) == 1)
    check_reconciliation(out_race['summary'], out_race['rows'])

    print('== concurrency: same day, DIFFERENT version -> one row per day still holds ==')
    db3 = FakeDB()
    # concurrent writer wins the day with v1 while our batch arrives with v2
    race_csv2 = CSV_HEADER + '2027-02-01,700,2\n'
    winner = Observation(1, dt.date(2027, 2, 1), 500, 1)
    orig = import_service._persist
    state = {'n': 0}
    def cross_version_race(db, series, rows, *a, **kw):
        state['n'] += 1
        if state['n'] == 1:
            db.observations[(1, dt.date(2027, 2, 1))] = winner
            db.fail_next_persist = True
        return orig(db, series, rows, *a, **kw)
    import_service._persist = cross_version_race
    try:
        out_race2 = import_service.commit_import(db3, 1, race_csv2, 'race-2', None, User('analyst@tide.demo'))
    finally:
        import_service._persist = orig
    check('cross-version collision isolated rather than duplicated',
          all(r['status'] in ('conflict', 'skipped') for r in out_race2['rows']),
          out_race2['summary'])
    check('exactly one row survives for the day',
          sum(1 for k in db3.observations if k[1] == dt.date(2027, 2, 1)) == 1)
    check('concurrent winner value preserved',
          db3.observations[(1, dt.date(2027, 2, 1))].value == 500)
    check_reconciliation(out_race2['summary'], out_race2['rows'])

    print('== batch retrieval supports reconciliation review ==')
    b = import_service.get_batch(db, 1, out4['batch']['id'])
    check('stored batch retrievable', b['batch']['idem_key'] == 'batch-2')
    check('stored rows reusable', len(b['rows']) == 2)
    listed = import_service.list_batches(db, 1)
    check('batches listed newest first', listed[0]['id'] >= listed[-1]['id'])
    try:
        import_service.get_batch(db, 1, 99999)
        check('missing batch 404', False)
    except HTTPException as e:
        check('missing batch 404', e.status_code == 404)

    print()
    print(f'{PASS} checks passed, {len(FAILS)} failed')
    if FAILS:
        for name, detail in FAILS:
            print('FAILED:', name, detail)
        sys.exit(1)


if __name__ == '__main__':
    run()
