import{api}from'./client.js';
export const listEvidence=()=>api('/evidence');
export const getEvidence=id=>api('/evidence/'+id);
export const restoreEvidence=id=>api('/evidence/'+id+'/restore');
export async function exportEvidence(id){
  const session=JSON.parse(localStorage.getItem('qr-session')||'null');
  const response=await fetch('/api/evidence/'+id+'/export',{headers:session?{Authorization:'Bearer '+session.token}:{}});
  if(!response.ok)throw new Error('证据导出失败');
  const text=await response.text();
  const u=URL.createObjectURL(new Blob([text],{type:'application/json'}));
  const a=document.createElement('a');a.href=u;a.download='review-'+id+'.json';a.click();
  setTimeout(()=>URL.revokeObjectURL(u),1000);
}
