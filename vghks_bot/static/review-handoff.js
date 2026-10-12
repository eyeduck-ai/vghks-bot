"use strict";
window.ReviewHandoff=(()=>{
 let context=null,busy=false,selectionScope="";
 const selectedPatients=new Map();
 function scope(){
  const key=JSON.stringify([account,currentReview]);
  if(key!==selectionScope){selectionScope=key;selectedPatients.clear();}
 }
 function renderSelection(){
  scope();
  const readonly=!!root.read_only;
  $("#reviewHandoffSelected").disabled=readonly||!currentReview||!selectedPatients.size;
  $("#reviewSelectionClear").disabled=!selectedPatients.size;
  $("#reviewSelectionCount").textContent=`已勾選 ${selectedPatients.size} 位（保留篩選前的勾選）`;
  for(const control of $$(".review-handoff-check")){control.checked=selectedPatients.has(control.dataset.mrn);control.disabled=readonly;}
 }
 function updatePatients(patients){
  scope();
  for(const patient of patients)if(selectedPatients.has(patient.mrn))selectedPatients.set(patient.mrn,patient);
  renderSelection();
 }
 function toggle(patient,checked){
  scope();
  if(root.read_only){renderSelection();return;}
  const mrn=String(patient.mrn||"").trim();
  if(mrn&&checked)selectedPatients.set(mrn,{...patient,mrn});else selectedPatients.delete(mrn);
  renderSelection();
 }
 function has(mrn){scope();return selectedPatients.has(mrn);}
 function patients(){scope();return [...selectedPatients.values()];}
 function clear(){selectedPatients.clear();renderSelection();}
 function reset(){selectionScope="";selectedPatients.clear();context=null;renderSelection();}
 function show(patients,individual){
  if(root.read_only)throw new Error("唯讀資料庫不能轉送病人。");
  const unique=[...new Map(patients.filter(patient=>String(patient.mrn||"").trim()).map(patient=>[String(patient.mrn).trim(),patient])).values()];
  if(!currentReview||!unique.length)throw new Error("請先勾選本次檢閱的病人。");
  context={account,task_id:currentReview,mrns:unique.map(patient=>String(patient.mrn).trim()),individual};
  const patient=unique[0];
  $("#handoffPatient").textContent=individual?(patient.name||"姓名未提供")+" · "+patient.mrn:`本次檢閱已勾選 ${unique.length} 位病人`;
  const source=$("#handoffSource");
  source.replaceChildren(new Option("本次檢閱的全部 SOAP",""),...(individual?patient.records||[]:[]).map(record=>
   new Option([record.date,record.section,record.case_no].filter(Boolean).join(" · "),record.id)));
  source.value="";source.disabled=!individual;
  $("#handoffStatus").textContent="加入後保留目前檢閱，來源 SOAP 版本一併保存。";
  dialog("handoffDialog");
 }
 function open(patient){show([patient],true);}
 function openMany(patients){show(patients,false);}
 function validate(source){
  if(root.read_only)throw new Error("唯讀資料庫不能轉送病人。");
  if(source.account!==account)throw new DOMException("帳號已切換","AbortError");
  if(source.task_id!==currentReview)throw new DOMException("檢閱任務已切換","AbortError");
 }
 async function send(openAfter=false){
  if(busy||!context)return;
  validate(context);
  if(!$("#handoffForm").reportValidity())return;
  const module=$("#handoffModule").value,record=$("#handoffSource").value,source={...context};
  if(openAfter&&!await window.ReviewNotesUI.flushAll())throw new Error("有備註尚未儲存，請重試後再開啟工具。");
  validate(source);
  busy=true;for(const id of ["handoffAdd","handoffOpen"])$("#"+id).disabled=true;
  $("#handoffStatus").textContent="正在加入待處理清單…";
  try{
   const result=await api("/tools/handoff",{module,review_task_id:source.task_id,mrns:source.mrns,...(source.individual&&record?{record_ids:[record]}:{})});
   validate(source);
   window.ToolWorkspace?.receivedHandoff(module,result);
   await refresh();validate(source);$("#handoffDialog").close();
   if(openAfter)await openModule(result.cohort.id,module,{focusMrn:source.mrns[0],selectedOnly:module==="cataract"});
   else say("已加入"+({retina:"視網膜比較",cataract:"白內障術前分析",surgery:"刀表更新"}[module])+"待處理清單");
   return result;
  }catch(error){if(source.account===account&&source.task_id===currentReview)$("#handoffStatus").textContent=error.message;throw error;}
  finally{busy=false;for(const id of ["handoffAdd","handoffOpen"])$("#"+id).disabled=!!root.read_only;}
 }
 $("#handoffForm").addEventListener("submit",event=>{event.preventDefault();act(()=>send(false));});
 $("#handoffOpen").addEventListener("click",()=>act(()=>send(true)));
 $("#reviewHandoffSelected").addEventListener("click",()=>act(()=>openMany(patients())));
 $("#reviewSelectionClear").addEventListener("click",clear);
 return {open,openMany,send,scope,renderSelection,updatePatients,toggle,has,patients,clear,reset};
})();
