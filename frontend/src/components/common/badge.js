import{STATUSES,WATERMARK_LABELS,EVIDENCE_STATES,PERIOD_STATUSES,BACKFILL_STATUSES}from'../../constants/enums.js';import{esc}from'../../utils/html.js';export const badge=s=>'<span class="badge '+esc(s)+'">'+esc(STATUSES[s]||s)+'</span>';
export const label=(map,key)=>esc(map[key]||key||'—');
