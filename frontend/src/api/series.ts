import {request} from './client';import type{Series,Observation,ImportPreview,ImportResult,ImportBatchMeta}from '../types/domain';
export const listSeries=()=>request<Series[]>('/series');
export const listObservations=(id:number)=>request<Observation[]>(`/series/${id}/observations`);
export const createSeries=(data:unknown)=>request<Series>('/series',{method:'POST',body:JSON.stringify(data)});
export const addObservation=(id:number,data:unknown)=>request<Observation>(`/series/${id}/observations`,{method:'POST',body:JSON.stringify(data)});
export const previewImport=(id:number,content:string)=>request<ImportPreview>(`/series/${id}/imports/preview`,{method:'POST',body:JSON.stringify({content})});
export const commitImport=(id:number,content:string,idem_key:string,filename:string|null)=>request<ImportResult>(`/series/${id}/imports`,{method:'POST',body:JSON.stringify({content,idem_key,filename})});
export const listImportBatches=(id:number)=>request<ImportBatchMeta[]>(`/series/${id}/imports`);
export const getImportBatch=(id:number,batchId:number)=>request<ImportResult>(`/series/${id}/imports/${batchId}`);
