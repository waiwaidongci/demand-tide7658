import type{ImportRowStatus,ImportSummary}from '../types/domain';
// 与后端 import_service 的处置口径保持一致
export const importStatusMeta:Record<ImportRowStatus,{label:string;kind:'ok'|'change'|'skip'|'warn'|'bad'}>={
  inserted:{label:'新增',kind:'ok'},
  updated:{label:'更正',kind:'change'},
  skipped:{label:'跳过',kind:'skip'},
  conflict:{label:'冲突隔离',kind:'warn'},
  bad:{label:'坏行隔离',kind:'bad'},
};
export const dispositionOrder:ImportRowStatus[]=['inserted','updated','skipped','conflict','bad'];
export function checkTotal(s:ImportSummary):boolean{
  return s.total===s.inserted+s.updated+s.skipped+s.conflict+s.bad;
}
