from fastapi import APIRouter,Depends
from src.config.database import session
from src.middlewares.auth import current_user,require
from src.schemas.series import SeriesInput
from src.schemas.observation import ObservationInput
from src.schemas.import_batch import ImportPreviewInput,ImportCommitInput
from src.services.series_service import list_series
from src.services.observation_service import observations,add_observation
from src.services import import_service
from src.controllers.series_controller import create
from src.utils.serializers import serialize
from src.utils.audit import record
router=APIRouter(prefix='/series',tags=['series'])
@router.get('')
def listing(db=Depends(session),user=Depends(current_user)): return list_series(db)
@router.post('',status_code=201)
def creating(data:SeriesInput,db=Depends(session),user=Depends(require('admin','analyst'))): return create(db,data,user)
@router.get('/{series_id}/observations')
def points(series_id:int,db=Depends(session),user=Depends(current_user)): return observations(db,series_id)
@router.post('/{series_id}/observations',status_code=201)
def adding(series_id:int,data:ObservationInput,db=Depends(session),user=Depends(require('admin','analyst'))):
    row=add_observation(db,series_id,data); db.flush(); record(db,user,'add_observation',series_id); db.commit(); return serialize(row)
@router.post('/{series_id}/imports/preview')
def preview(series_id:int,data:ImportPreviewInput,db=Depends(session),user=Depends(require('admin','analyst'))):
    return import_service.build_preview(db,series_id,data.content)
@router.post('/{series_id}/imports',status_code=201)
def commit(series_id:int,data:ImportCommitInput,db=Depends(session),user=Depends(require('admin','analyst'))):
    return import_service.commit_import(db,series_id,data.content,data.idem_key,data.filename,user)
@router.get('/{series_id}/imports')
def batches(series_id:int,db=Depends(session),user=Depends(current_user)):
    return import_service.list_batches(db,series_id)
@router.get('/{series_id}/imports/{batch_id}')
def batch_detail(series_id:int,batch_id:int,db=Depends(session),user=Depends(current_user)):
    return import_service.get_batch(db,series_id,batch_id)
