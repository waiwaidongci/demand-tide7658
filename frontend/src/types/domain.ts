export interface User {id:number;name:string;email:string;role:string}
export interface Series {id:number;code:string;name:string;city:string;product:string;unit:string;source_file:string;observation_count:number;total_demand:number}
export interface Observation {id:number;series_id:number;day:string;value:number;report_version:number;batch_id:number|null}
export interface ForecastPoint {day:string;predicted:number;lower:number;upper:number}
export interface Comparison {model:string;alpha:number|null;mae:number;wape:number|null}
export interface Run {id:number;name:string;series_id:number;horizon:number;season:number;status:string;error:string|null;created_at:string;result?:{selected:Comparison;comparisons:Comparison[];validation_days:number;training_days:number;interval_note:string};points?:ForecastPoint[];series?:Series}
export interface Release {id:number;run_id:number;note:string;published_at:string}
export interface Audit {id:number;actor:string;action:string;target:string;created_at:string}

export type ImportRowStatus='inserted'|'updated'|'skipped'|'conflict'|'bad';
export interface ImportRow {
  line:number;
  day:string|null;
  value:number|null;
  report_version:number|null;
  status:ImportRowStatus;
  action:string;
  reason:string|null;
  current_version:number|null;
  current_value:number|null;
  raw_day:string;raw_value:string;raw_version:string;
}
export interface ImportSummary {
  total:number;
  inserted:number;updated:number;skipped:number;conflict:number;bad:number;
  actions:Record<string,number>;
}
export interface ImportBatchMeta {
  id:number;series_id:number;idem_key:string;filename:string|null;source_file:string|null;
  status:string;total_rows:number;summary:ImportSummary;created_at:string;note:string|null;
}
export interface ImportPreview {
  series_id:number;
  summary:ImportSummary;
  rows:ImportRow[];
}
export interface ImportResult extends ImportPreview {
  batch:ImportBatchMeta;
  replayed:boolean;
}
