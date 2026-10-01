"use strict";

window.ReviewNotesUI=(()=>{
 let taskId="", rows=new Map(), generation=0;

 function reset(){
  generation++;
  for(const entry of rows.values())clearTimeout(entry.timer);
  taskId="";rows=new Map();
 }
 function current(mrn){return rows.get(mrn);}
 function sequence(mrn){return current(mrn)?.sequence_no||"";}
 async function load(id,{force=false}={}){
  if(id===taskId&&!force)return;
  if(force&&!await flushAll())return;
  const revision=++generation, selectedAccount=account;
  const result=await api("/reviews/notes/read",{task_id:id});
  if(revision!==generation||selectedAccount!==account||id!==currentReview)return;
  const previous=rows;
  rows=new Map(result.patients.map(row=>[row.mrn,{
   ...row,savedText:row.text,open:previous.get(row.mrn)?.open||false,
   timer:null,flushPromise:null,error:"",status:row.text?"已儲存":""
  }]));
  taskId=id;
 }
 function updateStatus(mrn){
  if(reviewPatient!==mrn)return;
  const target=$("#reviewNoteStatus"),entry=current(mrn);
  if(!target||!entry)return;
  target.textContent=entry.error?"儲存失敗："+entry.error:entry.status;
  target.classList.toggle("error",!!entry.error);
  const retry=$("#reviewNoteRetry");if(retry)retry.hidden=!entry.error;
 }
 function flush(mrn){
  const entry=current(mrn);
  if(!entry||root.read_only)return Promise.resolve(true);
  clearTimeout(entry.timer);
  if(entry.flushPromise)return entry.flushPromise;
  const selectedTask=taskId,selectedAccount=account;
  const run=(async()=>{
   while(entry.text!==entry.savedText){
    const text=entry.text;
    entry.status="正在儲存…";entry.error="";updateStatus(mrn);
    try{
     const saved=await api("/reviews/notes/save",{task_id:selectedTask,mrn,text});
     if(selectedTask!==taskId||selectedAccount!==account)return false;
     entry.savedText=text;entry.updated_at=saved.updated_at;
    }catch(error){entry.status="";entry.error=error.message||"請重試";updateStatus(mrn);return false;}
   }
   entry.status="已儲存";entry.error="";updateStatus(mrn);return true;
  })();
  entry.flushPromise=run.finally(()=>{entry.flushPromise=null;});
  return entry.flushPromise;
 }
 async function flushAll(){
  const results=await Promise.all([...rows.keys()].map(flush));
  return results.every(Boolean);
 }
 function control(patient){
  let entry=current(patient.mrn);
  if(!entry){entry={mrn:patient.mrn,text:"",savedText:"",open:false,status:"",error:"",timer:null,flushPromise:null};rows.set(patient.mrn,entry);}
  const button=node("button",undefined,"review-note-toggle");button.type="button";
  const label=entry.text.trim()?"編輯病人備註":"新增病人備註";
  setIcon(button,"noteAdd",label);
  button.setAttribute("aria-expanded",String(entry.open));
  const panel=node("div",undefined,"review-note-panel");panel.hidden=!entry.open;
  const field=node("label","病人備註"),input=node("textarea");
  input.id="reviewNoteInput";input.rows=3;input.maxLength=2000;input.value=entry.text;
  input.placeholder="記錄這位病人的檢閱說明";input.disabled=!!root.read_only;
  field.htmlFor=input.id;field.append(input);
  const status=node("p",entry.error?"儲存失敗："+entry.error:entry.status,"caption");
  status.id="reviewNoteStatus";status.setAttribute("role","status");
  status.classList.toggle("error",!!entry.error);
  const retry=btn("重試儲存",()=>flush(patient.mrn),"quiet");retry.id="reviewNoteRetry";retry.hidden=!entry.error;retry.disabled=!!root.read_only;
  panel.append(field,status,retry);
  button.addEventListener("click",()=>{
   entry.open=!entry.open;panel.hidden=!entry.open;button.setAttribute("aria-expanded",String(entry.open));
   if(entry.open)input.focus();else void flush(patient.mrn);
  });
  input.addEventListener("input",()=>{
   entry.text=input.value;entry.status="待儲存";entry.error="";updateStatus(patient.mrn);
   clearTimeout(entry.timer);entry.timer=setTimeout(()=>{void flush(patient.mrn);},600);
   setIcon(button,"noteAdd",entry.text.trim()?"編輯病人備註":"新增病人備註");
   button.setAttribute("aria-expanded",String(entry.open));
  });
  input.addEventListener("blur",()=>{void flush(patient.mrn);});
  return {button,panel};
 }
 function noteTable(patients){
  const entries=patients.map(patient=>[
   patient.sequence_no||"未提供",patient.name||"姓名未提供",patient.mrn,
   node("span",patient.text,"review-print-text")
  ]);
  return table(["掛號序號","姓名","病歷號","備註"],entries);
 }
 async function openPrint(){
  if(!await flushAll())throw new Error("有備註尚未儲存，請先重試後再列印。");
  const result=await api("/reviews/notes/read",{task_id:currentReview});
  const patients=result.patients.filter(patient=>patient.text.trim());
  const title=reviewData?.task.name||"病歷檢閱";
  $("#reviewNotesContext").textContent=`${title} · ${patients.length} 位有備註的病人`;
  const preview=$("#reviewNotesPreview"),print=$("#reviewNotesPrint");
  preview.replaceChildren();print.replaceChildren(node("h1","病歷檢閱備註"),node("p",`${title} · ${patients.length} 位 · ${time(reviewData?.task.created_at)}`));
  if(patients.length){preview.append(noteTable(patients));print.append(noteTable(patients));}
  else preview.append(empty("本次檢閱尚無備註。"));
  $("#reviewNotesPrintButton").disabled=!patients.length;
  dialog("reviewNotesDialog");
 }
 $("#reviewExportNotes").addEventListener("click",()=>act(openPrint));
 $("#reviewNotesPrintButton").addEventListener("click",()=>window.print());
 return {reset,load,sequence,control,flush,flushAll,openPrint};
})();
