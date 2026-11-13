"""Revised daily-demand report import.

Design contract (see migrations/002_observation_versions.md):

* One surviving observation per (series, day): the row with the highest
  ``report_version`` wins. A higher version corrects the stored value; an
  older version replayed later never rolls a value back.
* Rows with the same (day, version) but different values are an irreconcilable
  conflict: the whole conflict group is quarantined with an explicit reason and
  never written, while every other good row still lands.
* Malformed rows are quarantined individually; good rows still land.
* Every response carries the input total plus per-disposition counts so the
  numbers can be reconciled: total == inserted + updated + skipped + conflict
  + bad (skipped splits into same / older / duplicate).
* Commits are idempotent on ``idem_key`` and on the natural key
  (series, day, version): replaying or concurrently submitting the same batch
  never creates extra observations.
"""
from __future__ import annotations

import csv
import io
from datetime import date

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.models import ImportBatch, Observation
from src.utils.serializers import serialize
from src.utils.audit import record
from .series_service import get_series

MAX_ROWS = 5000
VALUE_MIN, VALUE_MAX = 0.0, 10_000_000.0
VERSION_MIN, VERSION_MAX = 0, 2_147_483_647

# 处置分组（用于核对）；skipped 另带 action 明细
STATUS_INSERTED = 'inserted'
STATUS_UPDATED = 'updated'
STATUS_SKIPPED = 'skipped'
STATUS_CONFLICT = 'conflict'
STATUS_BAD = 'bad'
SUMMARY_STATUSES = (STATUS_INSERTED, STATUS_UPDATED, STATUS_SKIPPED,
                    STATUS_CONFLICT, STATUS_BAD)

HEADER_ALIASES = {
    'day': ('day', 'date', '日期', '观测日期', '需求日期', 'day_date'),
    'value': ('value', 'demand', 'qty', 'quantity', '需求量', '需求', '数量', '日需求'),
    'report_version': ('report_version', 'version', 'rev', '报表版本', '版本', '版本号'),
}
REASON = {
    'blank': '空行，无任何字段',
    'missing_day': '缺少日期列或日期为空',
    'missing_value': '缺少需求量列或需求量为空',
    'bad_day': '日期格式无法识别（要求 YYYY-MM-DD）',
    'bad_version': '报表版本必须是 0–2147483647 的整数',
    'bad_value': '需求量必须是非负有限数，且不超过 10000000',
    'dup_in_file': '文件内同 (日期, 版本) 重复且取值一致，仅首行参与导入',
    'conflict_in_file': '文件内同 (日期, 版本) 存在不同取值，整组隔离待供应方核对',
    'conflict_db': '与台账同版本观测取值不一致，按口径隔离，不覆盖现有记录',
    'same_version_same_value': '台账已存在同版本同取值观测，无需重复入库',
    'older_version': '报表版本不高于台账当前版本，旧版重放不回退数值',
    'duplicate_key': '同 (序列, 日期, 版本) 观测已存在，按重复提交处理',
    'race_conflict': '并发导入出现同版本异值，已隔离未写入',
}


# --------------------------------------------------------------------------- #
# CSV parsing
# --------------------------------------------------------------------------- #
def _normalize_header(name: str) -> str:
    return (name or '').strip().lstrip('﻿').lower().replace(' ', '').replace('-', '_')


def _map_headers(fieldnames):
    mapping = {}
    normalized = [_normalize_header(f) for f in (fieldnames or [])]
    for target, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            key = _normalize_header(alias)
            if key in normalized:
                mapping[target] = normalized.index(key)
                break
    return mapping


def parse_csv(content: str):
    """Return (rows, header_mapping). rows are (line_number, raw_string_list)."""
    if content is None or not content.strip('﻿').strip():
        raise HTTPException(400, '导入内容为空，请粘贴 CSV 文本或选择文件')
    text = content.strip('﻿').strip('\r\n')
    reader = csv.reader(io.StringIO(text))
    try:
        rows = list(reader)
    except csv.Error as exc:
        raise HTTPException(400, f'CSV 解析失败：{exc}')
    if not rows:
        raise HTTPException(400, '导入内容为空，请粘贴 CSV 文本或选择文件')
    header = _map_headers(rows[0])
    if 'day' not in header or 'value' not in header:
        raise HTTPException(400, '未识别到必需列：表头需包含日期列（day/date/日期）与需求量列（value/demand/需求量）')
    data = [(i + 2, raw) for i, raw in enumerate(rows[1:])]  # 行号含表头，从 2 起
    if len(data) > MAX_ROWS:
        raise HTTPException(400, f'单次导入最多 {MAX_ROWS} 行，当前 {len(data)} 行，请拆分后再导入')
    return data, header


def _cell(raw, idx):
    if idx is None or idx >= len(raw):
        return ''
    return (raw[idx] or '').strip()


# --------------------------------------------------------------------------- #
# Row validation (pure function; no DB access)
# --------------------------------------------------------------------------- #
def validate_rows(data, header):
    """Turn raw CSV rows into typed records or quarantined bad rows."""
    typed, bad = [], []
    for line_no, raw in data:
        base = {'line': line_no, 'day': None, 'value': None,
                'report_version': 0, 'raw_day': '', 'raw_value': '', 'raw_version': ''}
        if not any((c or '').strip() for c in raw):
            bad.append({**base, 'status': STATUS_BAD, 'action': STATUS_BAD,
                        'reason': REASON['blank']})
            continue
        raw_day = _cell(raw, header.get('day'))
        raw_value = _cell(raw, header.get('value'))
        raw_version = _cell(raw, header.get('report_version')) if 'report_version' in header else ''
        base.update(raw_day=raw_day, raw_value=raw_value, raw_version=raw_version)
        problems = []
        if not raw_day:
            problems.append(REASON['missing_day'])
        if not raw_value:
            problems.append(REASON['missing_value'])
        day = None
        if raw_day:
            try:
                day = date.fromisoformat(raw_day)
            except ValueError:
                problems.append(REASON['bad_day'])
        version = 0
        if raw_version:
            try:
                version = int(raw_version)
                if not VERSION_MIN <= version <= VERSION_MAX:
                    raise ValueError
            except ValueError:
                problems.append(REASON['bad_version'])
        value = None
        if raw_value:
            try:
                value = float(raw_value)
                if value != value or value in (float('inf'), float('-inf')) \
                        or not VALUE_MIN <= value <= VALUE_MAX:
                    raise ValueError
            except ValueError:
                problems.append(REASON['bad_value'])
        if problems:
            bad.append({**base, 'status': STATUS_BAD, 'action': STATUS_BAD,
                        'reason': '；'.join(problems)})
            continue
        typed.append({**base, 'day': day, 'value': value, 'report_version': version})
    return typed, bad


def _summarize(rows):
    summary = {status: 0 for status in SUMMARY_STATUSES}
    actions = {}
    for row in rows:
        summary[row['status']] += 1
        actions[row['action']] = actions.get(row['action'], 0) + 1
    summary['total'] = len(rows)
    summary['actions'] = actions
    return summary


# --------------------------------------------------------------------------- #
# Planning (pure given the current ledger snapshot)
# --------------------------------------------------------------------------- #
def plan_rows(typed, bad, current):
    """Classify every input row against the current ledger.

    ``current`` maps day -> object exposing report_version/value.
    Returns the full ordered row list with status/action/reason.
    """
    result = list(bad)
    # 1) 文件内同 (day, version) 分组：异值 => 整组冲突；同值重复 => 仅首行有效
    groups = {}
    for row in typed:
        groups.setdefault((row['day'], row['report_version']), []).append(row)
    for (_day, _version), members in groups.items():
        values = {r['value'] for r in members}
        if len(values) > 1:
            for r in members:
                result.append({**r, 'status': STATUS_CONFLICT, 'action': STATUS_CONFLICT,
                               'reason': REASON['conflict_in_file']})
            continue
        keeper = members[0]
        result.append({**keeper, 'status': None, 'action': None, 'reason': None,
                       'current_version': None, 'current_value': None})
        for r in members[1:]:
            result.append({**r, 'status': STATUS_SKIPPED, 'action': 'duplicate_in_file',
                           'reason': REASON['dup_in_file']})
    # 2) 有效行与台账比对
    planned = []
    for row in result:
        if row['status'] in (STATUS_BAD, STATUS_CONFLICT, STATUS_SKIPPED):
            planned.append(row)
            continue
        held = current.get(row['day'])
        if held is None:
            planned.append({**row, 'status': STATUS_INSERTED, 'action': STATUS_INSERTED,
                            'reason': None, 'current_version': None, 'current_value': None})
            continue
        cur_ver, cur_val = held.report_version, held.value
        meta = {'current_version': cur_ver, 'current_value': cur_val}
        if row['report_version'] < cur_ver:
            planned.append({**row, 'status': STATUS_SKIPPED, 'action': 'older_version',
                            'reason': REASON['older_version'], **meta})
        elif row['report_version'] == cur_ver:
            if row['value'] == cur_val:
                planned.append({**row, 'status': STATUS_SKIPPED, 'action': 'same_version_same_value',
                                'reason': REASON['same_version_same_value'], **meta})
            else:
                planned.append({**row, 'status': STATUS_CONFLICT, 'action': STATUS_CONFLICT,
                                'reason': REASON['conflict_db'], **meta})
        else:
            planned.append({**row, 'status': STATUS_UPDATED, 'action': STATUS_UPDATED,
                            'reason': None, **meta})
    planned.sort(key=lambda r: r['line'])
    return planned


def _serialize_row(row):
    return {
        'line': row['line'],
        'day': row['day'].isoformat() if isinstance(row['day'], date) else None,
        'value': row['value'],
        'report_version': row['report_version'],
        'status': row['status'],
        'action': row['action'],
        'reason': row.get('reason'),
        'current_version': row.get('current_version'),
        'current_value': row.get('current_value'),
        'raw_day': row.get('raw_day', ''),
        'raw_value': row.get('raw_value', ''),
        'raw_version': row.get('raw_version', ''),
    }


# --------------------------------------------------------------------------- #
# DB access helpers (thin wrappers; patched in offline tests)
# --------------------------------------------------------------------------- #
def _find_batch(db, idem_key):
    return db.scalar(select(ImportBatch).where(ImportBatch.idem_key == idem_key))


def _observations_for_days(db, series_id, days):
    if not days:
        return []
    return list(db.scalars(
        select(Observation).where(Observation.series_id == series_id,
                                  Observation.day.in_(days))
        .order_by(Observation.day).with_for_update()))


def _all_observations(db, series_id):
    return list(db.scalars(
        select(Observation).where(Observation.series_id == series_id)
        .order_by(Observation.day)))


def build_preview(db, series_id, content):
    get_series(db, series_id)
    data, header = parse_csv(content)
    typed, bad = validate_rows(data, header)
    current = {o.day: o for o in _all_observations(db, series_id)}
    rows = plan_rows(typed, bad, current)
    return {'series_id': series_id, 'summary': _summarize(rows),
            'rows': [_serialize_row(r) for r in rows]}


# --------------------------------------------------------------------------- #
# Commit
# --------------------------------------------------------------------------- #
def _public_payload(batch, rows, replayed=None):
    return {
        'batch': serialize(batch),
        'summary': batch.summary,
        'rows': [_serialize_row(r) for r in rows],
        # 幂等重放（同 idem_key 再次提交）显式标记；并发重算批次由 note 区分
        'replayed': batch.note is not None if replayed is None else replayed,
    }


def _persist(db, series, rows, idem_key, filename, user, note=None):
    batch = ImportBatch(
        series_id=series.id, idem_key=idem_key,
        filename=(filename or None), source_file=series.source_file,
        status='committed', total_rows=len(rows),
        summary=_summarize(rows), result={'rows': [_serialize_row(r) for r in rows]},
        created_by=user.id, note=note)
    db.add(batch)
    db.flush()
    for row in rows:
        if row['status'] == STATUS_INSERTED:
            db.add(Observation(series_id=series.id, day=row['day'], value=row['value'],
                               report_version=row['report_version'], batch_id=batch.id))
        elif row['status'] == STATUS_UPDATED:
            held = row['_held']
            held.value = row['value']
            held.report_version = row['report_version']
            held.batch_id = batch.id
    return batch


def commit_import(db, series_id, content, idem_key, filename, user):
    series = get_series(db, series_id)
    if not idem_key or not str(idem_key).strip():
        raise HTTPException(400, '缺少导入批次幂等键 idem_key')
    idem_key = str(idem_key).strip()[:80]

    # 幂等重放：同键直接返回首批处置结果，不新增任何观测
    existing = _find_batch(db, idem_key)
    if existing:
        return _public_payload(existing, _stored_rows(existing), replayed=True)

    data, header = parse_csv(content)
    typed, bad = validate_rows(data, header)
    days = sorted({r['day'] for r in typed})

    # 对涉及日期加行锁，序列化并发导入；锁顺序按日期排序，避免死锁
    locked = _observations_for_days(db, series_id, days)
    current = {o.day: o for o in locked}
    rows = plan_rows(typed, bad, current)
    # 把活动对象挂回更新行，持久化阶段直接改写
    held_by_day = {o.day: o for o in locked}
    for row in rows:
        row['_held'] = held_by_day.get(row['day']) if row['status'] == STATUS_UPDATED else None

    try:
        batch = _persist(db, series, rows, idem_key, filename, user)
        db.flush()
    except IntegrityError:
        # 并发下另一事务已占用同一 (序列,日期)：回滚写入，保留可追溯批次，
        # 携带首轮规划结果重新调和，保证不新增重复观测、失败的插入不变成覆盖。
        db.rollback()
        return _reconcile_race(db, series, content, idem_key, filename, user, rows)

    record(db, user, 'import_observations', f'series={series_id} batch={batch.id}')
    db.commit()
    db.refresh(batch)
    return _public_payload(batch, rows)


def _reconcile_race(db, series, content, idem_key, filename, user, original_rows):
    # 回滚后幂等键可能已被并发事务占用：优先返回已有批次
    winner = _find_batch(db, idem_key)
    if winner:
        return _public_payload(winner, _stored_rows(winner))
    data, header = parse_csv(content)
    typed, bad = validate_rows(data, header)
    days = sorted({r['day'] for r in typed})
    locked = _observations_for_days(db, series.id, days)
    current = {o.day: o for o in locked}
    replanned = plan_rows(typed, bad, current)
    held_by_day = {o.day: o for o in locked}
    # 首轮规划的意图（按行号对齐）：插入失败的行绝不能在调和阶段变成更新覆盖
    original_intent = {r['line']: r['status'] for r in original_rows}
    rows = []
    for row in replanned:
        held = held_by_day.get(row['day'])
        intent = original_intent.get(row['line'])
        if held is not None and intent == STATUS_INSERTED:
            # 我们原本要新增，却被并发事务抢先占用了该日期：不覆盖，按取值隔离或跳过
            if row['value'] != held.value:
                row.update(status=STATUS_CONFLICT, action=STATUS_CONFLICT,
                           reason=REASON['race_conflict'],
                           current_version=held.report_version, current_value=held.value)
            else:
                row.update(status=STATUS_SKIPPED, action='duplicate_in_file',
                           reason=REASON['duplicate_key'],
                           current_version=held.report_version, current_value=held.value)
        elif held is not None and row['report_version'] == held.report_version \
                and row['status'] in (STATUS_INSERTED, STATUS_UPDATED):
            if row['value'] != held.value:
                row.update(status=STATUS_CONFLICT, action=STATUS_CONFLICT,
                           reason=REASON['race_conflict'],
                           current_version=held.report_version, current_value=held.value)
            else:
                row.update(status=STATUS_SKIPPED, action='duplicate_in_file',
                           reason=REASON['duplicate_key'],
                           current_version=held.report_version, current_value=held.value)
        elif row['status'] == STATUS_CONFLICT and row.get('reason') == REASON['conflict_db']:
            # 重规划阶段已识别为同版异值：标明这是并发竞争导致的隔离
            row['reason'] = REASON['race_conflict']
        row['_held'] = held_by_day.get(row['day']) if row['status'] == STATUS_UPDATED else None
        rows.append(row)
    rows.sort(key=lambda r: r['line'])
    batch = _persist(db, series, rows, idem_key, filename, user,
                     note='并发导入后重算处置；首轮写入已回滚')
    db.flush()
    record(db, user, 'import_observations',
           f'series={series.id} batch={batch.id} (race)')
    db.commit()
    db.refresh(batch)
    return _public_payload(batch, rows)


def _stored_rows(batch):
    stored = (batch.result or {}).get('rows', [])
    rows = []
    for item in stored:
        row = dict(item)
        if isinstance(row.get('day'), str):
            try:
                row['day'] = date.fromisoformat(row['day'])
            except (TypeError, ValueError):
                row['day'] = None
        rows.append(row)
    return rows


def list_batches(db, series_id):
    get_series(db, series_id)
    batches = db.scalars(select(ImportBatch)
                         .where(ImportBatch.series_id == series_id)
                         .order_by(ImportBatch.id.desc())).all()
    return [serialize(b) for b in batches]


def get_batch(db, series_id, batch_id):
    get_series(db, series_id)
    batch = db.get(ImportBatch, batch_id)
    if not batch or batch.series_id != series_id:
        raise HTTPException(404, '导入批次不存在')
    return _public_payload(batch, _stored_rows(batch))
