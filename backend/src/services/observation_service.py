from sqlalchemy import select
from fastapi import HTTPException
from src.models import Observation
from src.utils.serializers import serialize
from .series_service import get_series

def observations(db,series_id):
    get_series(db,series_id)
    return [serialize(o) for o in db.scalars(select(Observation).where(Observation.series_id==series_id).order_by(Observation.day))]

def add_observation(db,series_id,data):
    """Version-aware upsert of a single observation.

    Same version + same value is a no-op (kept for backwards-compatible
    clients); same version + different value is rejected so a correction must
    carry a newer report version; a newer version overwrites the stored value;
    an older version never rolls a value back.
    """
    get_series(db,series_id)
    payload=data.model_dump()
    held=db.scalar(select(Observation).where(Observation.series_id==series_id,
                                             Observation.day==payload['day']))
    if held is None:
        row=Observation(series_id=series_id,**payload)
        db.add(row)
        return row
    if payload['report_version']<held.report_version:
        raise HTTPException(409,f"报表版本 {payload['report_version']} 低于台账版本 {held.report_version}，旧版不能回退数值")
    if payload['report_version']==held.report_version and payload['value']!=held.value:
        raise HTTPException(409,f"该日期已有同版本（v{held.report_version}）且取值不同的观测，更正请使用更高报表版本")
    if payload['report_version']==held.report_version:
        return held  # 同版同值，幂等无操作
    held.value=payload['value']
    held.report_version=payload['report_version']
    return held
