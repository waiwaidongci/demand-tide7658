"""End-to-end integration test against real SQLAlchemy + SQLite.

Unlike test_import_logic.py (pure rules with fakes), this boots the actual
models, the real import_service DB helpers (select/scalars/with_for_update),
the single-row observation service and the forecast read path. MySQL-specific
DDL is covered separately by the migrator's inspector logic; here SQLite
exercises the same ORM semantics and the natural-key uniqueness constraint.

Run: PYTHONPATH=/tmp/pylibs:. python3 tests/test_import_integration.py
"""
import os
import sys
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DATABASE_URL', 'sqlite:////tmp/tide_import_it.sqlite3')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6399/0')
os.environ.setdefault('JWT_SECRET', 'it-secret')

if os.path.exists('/tmp/tide_import_it.sqlite3'):
    os.remove('/tmp/tide_import_it.sqlite3')

from sqlalchemy import inspect, select
from src.config.database import Base, engine, SessionLocal
from src import models
from src.services import import_service
from src.services.observation_service import add_observation, observations
from src.services.run_service import compute
from src.schemas.observation import ObservationInput
from src.models import Series, Observation, ImportBatch, User

PASS, FAILS = 0, []
def check(name, cond, detail=''):
    global PASS
    if cond:
        PASS += 1
        print(f'  PASS  {name}')
    else:
        FAILS.append((name, detail))
        print(f'  FAIL  {name}  {detail}')


class Actor:
    id = 1
    email = 'analyst@tide.demo'


HEADER = 'day,value,report_version\n'


def seed(db):
    db.add(User(email=Actor.email, name='分析师', role='analyst', password_hash='x'))
    db.add(Series(code='it-rose', name='集成玫瑰', city='杭州', product='玫瑰',
                  source_file='it.csv'))
    db.commit()
    series = db.scalar(select(Series).where(Series.code == 'it-rose'))
    return series.id


def main():
    Base.metadata.create_all(engine)
    ins = inspect(engine)
    check('observations table created', 'observations' in ins.get_table_names())
    check('import_batches table created', 'import_batches' in ins.get_table_names())
    obs_cols = {c['name'] for c in ins.get_columns('observations')}
    check('version columns present',
          {'report_version', 'batch_id'}.issubset(obs_cols), obs_cols)
    batch_cols = {c['name'] for c in ins.get_columns('import_batches')}
    check('idem_key column present', 'idem_key' in batch_cols)
    uq = {u['name'] for u in ins.get_unique_constraints('observations')}
    check('one-row-per-day hard invariant present', 'uq_series_day' in uq, uq)
    idx = {i['name'] for i in ins.get_indexes('observations')}
    check('version lookup index present', 'ix_obs_series_day_ver' in idx, idx)

    with SessionLocal() as db:
        sid = seed(db)

        print('== preview then commit through the real session ==')
        csv_text = HEADER + '\n'.join([
            '2026-11-01,100,1',
            '2026-11-02,110,1',
            'bad-day,120,1',
            '2026-11-03,130,1',
        ])
        preview = import_service.build_preview(db, sid, csv_text)
        check('preview counts', preview['summary']['inserted'] == 3
              and preview['summary']['bad'] == 1, preview['summary'])
        check('preview does not write',
              db.scalar(select(Observation).where(Observation.series_id == sid)) is None)

        out = import_service.commit_import(db, sid, csv_text, 'it-batch-1', 'r1.csv', Actor())
        check('commit inserts 3', db.query(Observation).filter_by(series_id=sid).count() == 3)
        check('summary persisted on batch', out['batch']['summary']['total'] == 4)
        check('reconciliation identity',
              out['summary']['total'] == sum(out['summary'][s] for s in
              ('inserted', 'updated', 'skipped', 'conflict', 'bad')))

        print('== idempotent replay via identical idem_key ==')
        again = import_service.commit_import(db, sid, csv_text, 'it-batch-1', 'r1.csv', Actor())
        check('replay flagged', again['replayed'] is True)
        check('replay adds nothing',
              db.query(Observation).filter_by(series_id=sid).count() == 3)
        check('single batch row per idem key',
              db.query(ImportBatch).filter_by(idem_key='it-batch-1').count() == 1)

        print('== database rejects duplicate natural key even at ORM level ==')
        from sqlalchemy.exc import IntegrityError as SAIntegrityError
        db.add(Observation(series_id=sid, day=dt.date(2026, 11, 1),
                           value=100, report_version=1))
        try:
            db.flush()
            check('natural key duplicate rejected', False)
        except SAIntegrityError:
            db.rollback()
            check('natural key duplicate rejected', True)

        print('== higher-version correction supersedes value ==')
        corr = HEADER + '2026-11-01,180,2\n'
        c = import_service.commit_import(db, sid, corr, 'it-batch-2', None, Actor())
        check('correction classified as update', c['summary']['updated'] == 1)
        held = db.scalar(select(Observation).where(
            Observation.series_id == sid, Observation.day == dt.date(2026, 11, 1)))
        check('value corrected', held.value == 180 and held.report_version == 2)
        check('one row per day', db.query(Observation).filter_by(series_id=sid).count() == 3)

        print('== old version replay cannot roll back ==')
        old = import_service.commit_import(
            db, sid, HEADER + '2026-11-01,100,1\n', 'it-batch-old', None, Actor())
        check('old version skipped', old['summary']['skipped'] == 1)
        held = db.scalar(select(Observation).where(
            Observation.series_id == sid, Observation.day == dt.date(2026, 11, 1)))
        check('value preserved', held.value == 180)

        print('== same-version different value quarantined, good row lands ==')
        mix = HEADER + '2026-11-01,999,2\n2026-11-04,140,2\n'
        m = import_service.commit_import(db, sid, mix, 'it-batch-3', None, Actor())
        check('one conflict one insert',
              m['summary']['conflict'] == 1 and m['summary']['inserted'] == 1, m['summary'])
        check('conflict did not overwrite',
              db.scalar(select(Observation.value).where(
                  Observation.series_id == sid,
                  Observation.day == dt.date(2026, 11, 1))) == 180)

        print('== single-row service: version-aware upsert ==')
        # newer version overwrites
        row = add_observation(db, sid, ObservationInput(
            day=dt.date(2026, 11, 2), value=115, report_version=3))
        db.flush()
        check('single-row upgrade', row.value == 115 and row.report_version == 3)
        # same version same value: idempotent no-op
        same = add_observation(db, sid, ObservationInput(
            day=dt.date(2026, 11, 2), value=115, report_version=3))
        check('same version same value no-op', same.id == row.id)
        # same version different value: 409
        from fastapi import HTTPException
        try:
            add_observation(db, sid, ObservationInput(
                day=dt.date(2026, 11, 2), value=116, report_version=3))
            check('same version diff value rejected', False)
        except HTTPException as e:
            check('same version diff value rejected', e.status_code == 409)
        # older version: 409
        try:
            add_observation(db, sid, ObservationInput(
                day=dt.date(2026, 11, 2), value=1, report_version=1))
            check('older version rejected', False)
        except HTTPException as e:
            check('older version rejected', e.status_code == 409)
        db.commit()
        listed = observations(db, sid)
        check('observations API serializes version',
              all('report_version' in o for o in listed))

        print('== forecast reads corrected observations ==')
        # build a continuous 45-day series on a second series so compute() can run
        db.add(Series(code='it-cont', name='连续序列', city='上海', product='百合',
                      source_file='it.csv'))
        db.commit()
        cont = db.scalar(select(Series).where(Series.code == 'it-cont')).id
        bulk = '\n'.join(f'2026-01-{d:02d},{100 + d},1' for d in range(1, 32))
        bulk += '\n' + '\n'.join(f'2026-02-{d:02d},{131 + d},1' for d in range(1, 15))
        import_service.commit_import(db, cont, 'day,value,report_version\n' + bulk + '\n',
                                     'it-seed', None, Actor())
        check('continuous series has 45 days',
              db.query(Observation).filter_by(series_id=cont).count() == 45)
        # correct an early day with a higher version; forecast must use new value
        import_service.commit_import(
            db, cont, 'day,value,report_version\n2026-01-05,999,2\n',
            'it-fix', None, Actor())
        fixed = db.scalar(select(Observation).where(
            Observation.series_id == cont, Observation.day == dt.date(2026, 1, 5)))
        check('correction landed on continuous series', fixed.value == 999)

        run = models.ForecastRun(name='it 运行', series_id=cont, horizon=7,
                                 season=7, created_by=Actor.id)
        db.add(run)
        db.commit()
        compute(db, run.id)
        db.refresh(run)
        check('forecast completes on corrected data', run.status == 'completed', run.error)
        check('forecast checksum includes corrected 999',
              run.result['checksum'] == round(
                  sum(o.value for o in db.scalars(select(Observation).where(
                      Observation.series_id == cont))), 4))
        pts = db.scalars(select(models.ForecastPoint).where(
            models.ForecastPoint.run_id == run.id)).all()
        check('7 forecast points stored', len(pts) == 7)

        print('== batch listing & detail for reconciliation ==')
        batches = import_service.list_batches(db, sid)
        check('batch history listed', len(batches) >= 4)
        detail = import_service.get_batch(db, sid, out['batch']['id'])
        check('batch detail rows retained', len(detail['rows']) == 4)
        check('detail row reasons present for bad row',
              any(r['status'] == 'bad' and r['reason'] for r in detail['rows']))

    print()
    print(f'{PASS} checks passed, {len(FAILS)} failed')
    if FAILS:
        for name, detail in FAILS:
            print('FAILED:', name, detail)
        sys.exit(1)


if __name__ == '__main__':
    main()
