/** @typedef {{id:number,name:string,description:string,original_sql:string}} Sample */
/** @typedef {{id:number,name:string,sample_id:number,period:string,candidate_sql:string,status:string,status_reason:string|null}} Batch */
/** @typedef {{id:number,batch_id:number,period:string,state:string,watermark_seq:number,result:Object}} Evidence */
/** @typedef {{period:string,status:string,stable:boolean,state:string,label:string,reason:string,ingest_seq:number,stable_seq:number}} Period */
/** @typedef {{id:number,period:string,batch_id:number,evidence_id:number,target_seq:number,affected_customers:string[],status:string,attempts:number,reason:string|null,error:string|null}} Backfill */
export const entityNames={sample:'SQL 样本',batch:'复核批次',evidence:'执行证据',period:'结算账期',backfill:'补算批次'};
