import{api,post}from'./client.js';
export const listPeriods=()=>api('/periods');
export const getPeriod=p=>api('/periods/'+encodeURIComponent(p));
export const createPeriod=data=>post('/periods',data);
export const closePeriod=p=>post('/periods/'+encodeURIComponent(p)+'/close',{});
export const listBackfills=period=>api('/backfills'+(period?'?period='+encodeURIComponent(period):''));
export const ingestBills=data=>post('/ingestions',data);
