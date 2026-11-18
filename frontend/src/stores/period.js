import{listPeriods,listBackfills}from'../api/period.js';
export const periods={items:[],backfills:[],async load(){this.items=await listPeriods();return this.items},async loadBackfills(period){this.backfills=await listBackfills(period);return this.backfills}};
