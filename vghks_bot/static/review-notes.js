"use strict";

window.ReviewNotesUI=(()=>{
 let taskId="", rows=new Map(), generation=0, clearing=false, clearOperation=null;

 function syncControls(){
  $("#reviewClearNotes").disabled=!!root?.read_only||clearing||!taskId||![...rows.values()].some(row=>row.text.trim());
  $("#reviewExportNotes").disabled=clearing||!taskId;
  for(const field of $$("#reviewNoteInput,#reviewNoteRetry"))field.disabled=!!root?.read_only||clearing;
 }

 function reset(){
  generation++;
  for(const entry of rows.values())clearTimeout(entry.timer);
  taskId="";rows=new Map();syncControls();
 }
 function current(mrn){return rows.get(mrn);}
 function sequence(mrn){return current(mrn)?.sequence_no||"";}
 async function load(id,{force=false}={}){
  if(clearOperation)await clearOperation;
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
  syncControls();
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
  if(clearOperation){try{await clearOperation;}catch(_){return false;}}
  const results=await Promise.all([...rows.keys()].map(flush));
  return results.every(Boolean);
 }
 function clearAll(){
  if(clearOperation)return clearOperation;
  if(root.read_only)return Promise.reject(new Error("目前是唯讀檢閱；請建立可編輯副本後操作。"));
  const selectedTask=taskId,selectedAccount=account,revision=generation;
  if(!selectedTask||selectedTask!==currentReview)return Promise.reject(new Error("請先開啟檢閱任務。"));
  const stillCurrent=()=>revision===generation&&selectedTask===taskId&&selectedTask===currentReview&&selectedAccount===account;
  const requireCurrent=()=>{if(!stillCurrent())throw new DOMException("檢閱任務已切換","AbortError");};
  clearing=true;syncControls();
  clearOperation=(async()=>{
   // Settle every existing save before deletion, with editing locked throughout.
   if(!(await Promise.all([...rows.keys()].map(flush))).every(Boolean))throw new Error("有備註尚未儲存，請先重試後再清除。");
   requireCurrent();
   const notes=await api("/reviews/notes/read",{task_id:selectedTask});requireCurrent();
   const count=notes.patients.filter(row=>row.text.trim()).length;
   if(!count){say("本次檢閱尚無備註。");return false;}
   const title=reviewData?.task.name||"病歷檢閱";
   if(!await confirmDelete(`清除「${title}」全部 ${count} 位病人的備註？範圍包含未顯示於搜尋或 TAG 篩選結果的病人。`,{title:"批次清除備註",label:"清除全部備註"}))return false;
   requireCurrent();
   const result=await api("/reviews/notes/clear",{task_id:selectedTask});
   if(!stillCurrent())return true;
   generation++;
   for(const row of rows.values()){
    clearTimeout(row.timer);row.timer=null;row.text=row.savedText="";row.updated_at="";row.error="";row.status="";
   }
   $("#reviewNotesPreview").replaceChildren(empty("本次檢閱尚無備註。"));
   $("#reviewNotesPrint").replaceChildren();$("#reviewNotesPrintButton").disabled=true;
   $("#reviewNotesContext").textContent=`${title} · 0 位有備註的病人`;
   const task=work?.tasks.find(row=>row.id===selectedTask);if(task)task.note_count=0;
   window.TaskActivityUI?.update();renderReviewDetail();
   say(`已清除 ${result.cleared_count} 位病人的備註。`);return true;
  })().finally(()=>{clearing=false;clearOperation=null;syncControls();});
  return clearOperation;
 }
 function control(patient){
  let entry=current(patient.mrn);
  if(!entry){entry={mrn:patient.mrn,text:"",savedText:"",open:false,status:"",error:"",timer:null,flushPromise:null};rows.set(patient.mrn,entry);}
  const button=node("button",undefined,"review-note-toggle");button.type="button";
  const label=entry.text.trim()?"編輯病人備註":"新增病人備註";
  setIcon(button,"noteAdd",label);
  button.setAttribute("aria-expanded",String(entry.open));
  const panel=node("div",undefined,"review-note-panel");panel.hidden=!entry.open;
  const field=node("label","本次檢閱備註"),input=node("textarea");
  input.id="reviewNoteInput";input.rows=3;input.maxLength=2000;input.value=entry.text;
  input.placeholder="儲存在本次檢閱；其他檢閱可由「查詢歷次備註」查詢";input.disabled=!!root.read_only||clearing;
  field.htmlFor=input.id;field.append(input);
  const status=node("p",entry.error?"儲存失敗："+entry.error:entry.status,"caption");
  status.id="reviewNoteStatus";status.setAttribute("role","status");
  status.classList.toggle("error",!!entry.error);
  const retry=btn("重試儲存",()=>flush(patient.mrn),"quiet");retry.id="reviewNoteRetry";retry.hidden=!entry.error;retry.disabled=!!root.read_only||clearing;
  const scope=node("p",`本次檢閱：${reviewData?.task.name||"病歷檢閱"} · ${time(reviewData?.task.created_at)}`,"caption");
  panel.append(field,scope,status,retry);
  button.addEventListener("click",()=>{
   entry.open=!entry.open;panel.hidden=!entry.open;button.setAttribute("aria-expanded",String(entry.open));
   if(entry.open)input.focus();else void flush(patient.mrn);
  });
  input.addEventListener("input",()=>{
   if(clearing||root.read_only)return;
   entry.text=input.value;entry.status="待儲存";entry.error="";updateStatus(patient.mrn);
   clearTimeout(entry.timer);entry.timer=setTimeout(()=>{void flush(patient.mrn);},600);
   setIcon(button,"noteAdd",entry.text.trim()?"編輯病人備註":"新增病人備註");
   button.setAttribute("aria-expanded",String(entry.open));
   syncControls();
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
 $("#reviewExportNotes").addEventListener("click",()=>act(openPrint).finally(syncControls));
 $("#reviewClearNotes").addEventListener("click",()=>act(clearAll).finally(syncControls));
 $("#reviewNotesPrintButton").addEventListener("click",()=>window.print());
 let historyPage=0,historySequence=0;
 async function findHistory(){
  if(!await flushAll())throw new Error("有備註尚未儲存，請先重試。");
  const sequence=++historySequence,mrn=$("#noteHistoryMRN").value.trim(),selectedAccount=account;
  const result=await api("/reviews/notes/search",{mrn,offset:historyPage*40,limit:40});
  if(sequence!==historySequence||selectedAccount!==account)return;
  $("#noteHistoryContext").textContent=`病歷號 ${result.mrn} · 共 ${result.total} 次檢閱有備註（目前帳號與資料庫）`;
  const cards=result.notes.map(note=>{
   const card=node("article",undefined,"note-history-entry"),heading=node("div",undefined,"dialog-heading");
   heading.append(node("strong",note.task_name||"病歷檢閱"),btn("開啟該次檢閱",async()=>{$("#noteHistoryDialog").close();await openReview(note.task_id);await selectReviewPatient(result.mrn);},"quiet"));
   card.append(heading,node("p",`檢閱 ${time(note.created_at)} · 備註更新 ${time(note.updated_at)}`,"caption"),node("p",note.text,"review-print-text"));return card;
  });
  $("#noteHistoryResults").replaceChildren(...(cards.length?cards:[empty("此病歷號目前沒有已保存的檢閱備註。") ]));
  paginate($("#noteHistoryPager"),historyPage,result.total,index=>{historyPage=index;return findHistory();});
 }
 async function openHistory(){
  historySequence++;historyPage=0;$("#noteHistoryMRN").value=reviewPatient||"";
  $("#noteHistoryResults").replaceChildren();$("#noteHistoryPager").replaceChildren();
  $("#noteHistoryContext").textContent="以病歷號查詢目前帳號、目前資料庫內的歷次檢閱備註。";
  dialog("noteHistoryDialog");$("#noteHistoryMRN").focus();
  if(reviewPatient)await findHistory();
 }
 $("#reviewFindNotes").addEventListener("click",()=>act(openHistory));
 $("#noteHistoryForm").addEventListener("submit",event=>{event.preventDefault();historyPage=0;void act(findHistory);});
 $("#taskFindNotes").addEventListener("click",()=>act(openHistory));
 return {reset,load,sequence,control,flush,flushAll,openPrint,clearAll,openHistory};
})();
