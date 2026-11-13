"""Idempotent schema upgrades for existing databases.

Fresh databases get the current shape directly from Base.metadata.create_all;
this module only alters pre-existing tables (currently the 001 -> 002
observation versioning change). Every step checks the inspector first so the
routine is safe to run on every startup. The upgrade is additive: no legacy
constraint is dropped, so existing data needs no migration or backfill.
"""
import logging
from sqlalchemy import inspect, text
from src.config.database import engine

log = logging.getLogger(__name__)


def _mysql_ensure_columns(ins, conn):
    cols = {c['name'] for c in ins.get_columns('observations')}
    if 'report_version' not in cols:
        conn.execute(text("ALTER TABLE observations ADD COLUMN report_version INT NOT NULL DEFAULT 0"))
        log.info('migration 002: added observations.report_version')
    if 'batch_id' not in cols:
        conn.execute(text("ALTER TABLE observations ADD COLUMN batch_id INT NULL"))
        conn.execute(text(
            "ALTER TABLE observations ADD CONSTRAINT fk_obs_batch "
            "FOREIGN KEY(batch_id) REFERENCES import_batches(id)"))
        log.info('migration 002: added observations.batch_id')


def _mysql_ensure_indexes(ins, conn):
    idx = {i['name'] for i in ins.get_indexes('observations')}
    if 'ix_obs_series_day_ver' not in idx:
        conn.execute(text(
            "CREATE INDEX ix_obs_series_day_ver "
            "ON observations (series_id, day, report_version)"))
        log.info('migration 002: added ix_obs_series_day_ver')


def ensure_schema():
    ins = inspect(engine)
    names = set(ins.get_table_names())
    if 'observations' not in names:
        return  # fresh database: create_all builds the current schema
    with engine.begin() as conn:
        if engine.dialect.name == 'mysql':
            _mysql_ensure_columns(ins, conn)
            _mysql_ensure_indexes(ins, conn)
        else:
            # SQLite and other dialects used in tests create the current shape
            # via create_all; legacy-shape upgrades only need the new columns.
            cols = {c['name'] for c in ins.get_columns('observations')}
            if 'report_version' not in cols:
                conn.execute(text(
                    "ALTER TABLE observations ADD COLUMN report_version INTEGER NOT NULL DEFAULT 0"))
            if 'batch_id' not in cols:
                conn.execute(text(
                    "ALTER TABLE observations ADD COLUMN batch_id INTEGER"))
