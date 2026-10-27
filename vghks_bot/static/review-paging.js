"use strict";

// Patient pages contain identities and status only. SOAP snapshots stay in a
// small account/task-scoped detail cache and never enter the selection queue.
window.ReviewPaging=(()=>{
 const pageSize=40,details=new Map(),pending=new Map();
 let owner="",pageData=null,detail=null,navigation=null,offset=0,filtersKey="",listSequence=0,detailSequence=0,selectionSequence=0,statusSignature="",detailSignature="",busy=false;
 const scope=()=>JSON.stringify([account,currentReview,epoch]);
 const taskSignature=(task,revisions={})=>JSON.stringify([task?.revision??task?.updated_at,task?.status,revisions.profile,revisions.review_note,revisions.patient_tags,revisions.preferences,revisions.records]);
 const filterKey=()=>JSON.stringify(reviewFilters());
 function reset(){owner="";pageData=detail=navigation=null;offset=0;filtersKey=statusSignature=detailSignature="";listSequence++;detailSequence++;selectionSequence++;busy=false;details.clear();pending.clear();}
 function ensure(){const key=scope();if(key!==owner){reset();owner=key;}return key;}
 function active(){return owner===scope()&&!!pageData;}
 function patient(){return active()&&detail?.mrn===reviewPatient?detail:null;}
 function request(path,values){
  const key=JSON.stringify([owner,path,values]);if(pending.has(key))return pending.get(key);
  const promise=api(path,values);pending.set(key,promise);
  return promise.finally(()=>{if(pending.get(key)===promise)pending.delete(key);});
 }
 function alive(key){return key===owner&&key===scope()&&page==="review";}
 function saveDetail(key,value){details.delete(key);details.set(key,value);while(details.size>8)details.delete(details.keys().next().value);}
 function applyNavigation(next){navigation=next;updateNavigation();}
 function updateNavigation(){
  if(!active())return;
  for(const button of $("#reviewDetail").querySelectorAll("[data-review-move]"))button.disabled=!navigation?.[Number(button.dataset.reviewMove)<0?"previous_mrn":"next_mrn"];
  const notice=$("#reviewDetail").querySelector(".review-page-context");if(!notice)return;
  const current=patient(),outside=!!current&&!pageData.patients.some(row=>row.mrn===current.mrn);
  notice.hidden=!outside;
  notice.textContent=outside?(navigation?.position==null?"目前檢閱的病人不符合此篩選條件；可繼續閱讀已選病歷。":"目前檢閱的病人在其他頁；換頁不會切換右側病歷。") : "";
 }
 function positionNotice(){const value=node("p","","caption review-page-context");value.hidden=true;return value;}
 function nav(delta){return navigation?.[delta<0?"previous_mrn":"next_mrn"]||"";}
 function renderPager(){
  const container=$("#reviewPager");if(!container||!pageData)return;
  container.hidden=$("#reviewPatients").hidden;
  const index=Math.floor(offset/pageSize),max=Math.max(1,Math.ceil(pageData.total/pageSize));
  let before=container.querySelector(".review-page-before"),after=container.querySelector(".review-page-after"),label=container.querySelector(".review-page-label");
  if(!before){
   before=btn("上一頁",()=>changePage(offset-pageSize),"quiet review-page-before");after=btn("下一頁",()=>changePage(offset+pageSize),"quiet review-page-after");
   label=node("span","","caption review-page-label");label.tabIndex=-1;label.setAttribute("role","status");container.replaceChildren(before,label,after);
  }
  before.disabled=busy||offset===0;after.disabled=busy||offset+pageSize>=pageData.total;
  const text=`第 ${index+1} / ${max} 頁`;if(label.textContent!==text)label.textContent=text;
  container.setAttribute("aria-busy",String(busy));
 }
 async function loadDetail(mrn,{revision,loading=false,notesPromise=null}={}){
  const key=ensure(),sequence=++detailSequence,filters=reviewFilters();
  const cacheKey=JSON.stringify([mrn,filters,revision||""]);
  if(loading)$("#reviewDetail").replaceChildren(ClinicalUI.loading("正在讀取病歷…"));
  try{
   const [value]=await Promise.all([revision&&details.has(cacheKey)?details.get(cacheKey):request("/reviews/patient",{id:currentReview,mrn,...filters}),notesPromise]);
   if(!alive(key)||sequence!==detailSequence||mrn!==reviewPatient)return;
   if(!value.patient||value.patient.mrn!==mrn||value.task_id!==currentReview)throw new Error("病歷資料範圍不符，請重新選擇病人。");
   const nextSignature=JSON.stringify([mrn,value.revision,filters]);
   const first=detail?.mrn!==mrn,changed=first||nextSignature!==detailSignature;
   detail=value.patient;detailSignature=nextSignature;
   saveDetail(JSON.stringify([mrn,filters,value.revision]),value);
   applyNavigation(value);
   if(changed){
    const panel=$("#reviewDetail"),scroll=loading?0:panel.scrollTop,focus=document.activeElement;
    const editing=panel.contains(focus)&&focus.id==="reviewNoteInput"?{start:focus.selectionStart,end:focus.selectionEnd}:null;
    const focusIdentity=panel.contains(focus)&&!editing?{tag:focus.tagName,id:focus.id,label:focus.getAttribute?.("aria-label"),text:focus.textContent}:null;
    renderReviewDetail();panel.scrollTop=scroll;
    if(editing){const input=$("#reviewNoteInput");input?.focus({preventScroll:true});input?.setSelectionRange?.(editing.start,editing.end);}
    else if(focusIdentity){const next=[...panel.querySelectorAll("button,a,input,summary")].find(element=>element.tagName===focusIdentity.tag&&(focusIdentity.id?element.id===focusIdentity.id:focusIdentity.label?element.getAttribute("aria-label")===focusIdentity.label:element.textContent===focusIdentity.text));if(next&&!next.disabled)next.focus({preventScroll:true});}
   }
   updateNavigation();
  }catch(error){
   if(alive(key)&&sequence===detailSequence&&mrn===reviewPatient){
    if(loading){const content=$("#reviewDetail");content.replaceChildren(node("p",error.message||"病歷讀取失敗。","error"),btn("重試讀取",()=>loadDetail(mrn,{loading:true})));}
    else say(error.message||"病歷讀取失敗，原內容已保留。",true);
   }
   throw error;
  }
 }
 async function load({resetPage=false,focusMrn="",offset:requestedOffset}={}){
  if(!currentReview)return;
  const key=ensure(),filters=reviewFilters(),nextFilters=JSON.stringify(filters),observedRevisions={...work?.revisions};
  if(filtersKey!==nextFilters){offset=0;detailSequence++;}
  if(resetPage)offset=0;
  if(Number.isInteger(requestedOffset))offset=Math.max(0,requestedOffset);
  filtersKey=nextFilters;
  const sequence=++listSequence;
  busy=true;renderPager();
  try{
   const value=await request("/reviews/patients",{id:currentReview,offset,limit:pageSize,...filters,...(focusMrn?{focus_mrn:focusMrn}:{}),...(reviewPatient?{selected_mrn:reviewPatient}:{})});
   if(!alive(key)||sequence!==listSequence)return;
   if(value.task?.id!==currentReview||!Array.isArray(value.patients))throw new Error("病人清單範圍不符，請重新開啟檢閱。");
   pageData=value;offset=value.offset||0;reviewData=value;
   reviewSignature=taskSignature(value.task);
   window.ToolWorkspace?.adoptReview(value.task);renderReviewHeader(value);
   if(!reviewPatient)reviewPatient=value.patients[0]?.mrn||"";
   renderReviewPatients(value.patients);
   $("#reviewSummary").textContent=`${value.total} / ${value.all_total} 位`+(value.incremental?` · 新增 ${value.incremental.new} · 沿用 ${value.incremental.reused} · 待處理 ${value.incremental.pending}`:"")+(value.scope_issue_count?` · ${value.scope_issue_count} 份病歷的 tag 判定不完整`:"");
   $("#reviewSummary").title=$("#reviewSummary").textContent;
   const mrns=[...new Set([...value.patients.map(row=>row.mrn),reviewPatient].filter(Boolean))];
   const notes=window.ReviewNotesUI?.load(currentReview,{mrns});
   const revision=reviewPatient?(value.patient_revisions?.[reviewPatient]||value.selected_revision):null;
   if(value.selected_navigation&&detail?.mrn===reviewPatient)applyNavigation(value.selected_navigation);
   if(reviewPatient&&(!patient()||!revision||JSON.stringify([reviewPatient,revision,filters])!==detailSignature))await loadDetail(reviewPatient,{revision,loading:!patient(),notesPromise:notes});
   else{if(notes)await notes;if(!alive(key)||sequence!==listSequence)return;updateNavigation();}
   if(!alive(key)||sequence!==listSequence)return;
   renderReviewPatients(value.patients);
   if(!reviewPatient){detail=null;detailSignature="";$("#reviewDetail").replaceChildren(empty("沒有符合的病人。"));}
   if(alive(key)&&sequence===listSequence)statusSignature=taskSignature(value.task,observedRevisions);
  }finally{if(alive(key)&&sequence===listSequence){busy=false;renderPager();}}
 }
 async function select(mrn,{locate=false}={}){
  const key=ensure(),previous=reviewPatient,selection=++selectionSequence;
  if(previous!==mrn&&!await window.ReviewNotesUI.flush(previous))throw new Error("有備註尚未儲存，請重試後再切換病人。");
  if(!alive(key)||selection!==selectionSequence)return;
  if(previous!==mrn){window.ScanBrowser?.reset();window.FileCompare?.context(account,mrn);}
  reviewPatient=mrn;
  for(const element of $$(".patient-select"))element.classList.toggle("active",element.dataset.mrn===mrn);
  if(locate||!pageData?.patients.some(row=>row.mrn===mrn))return load({focusMrn:mrn});
  const notes=window.ReviewNotesUI?.load(currentReview,{mrns:[mrn]});
  await loadDetail(mrn,{revision:pageData.patient_revisions?.[mrn],loading:previous!==mrn,notesPromise:notes});
 }
 async function changePage(next){
  if(busy||next<0||next>=(pageData?.total||0))return;
  const key=owner;
  if(!await window.ReviewNotesUI.flush(reviewPatient))throw new Error("有備註尚未儲存，請先重試。");
  if(!alive(key))return;
  await load({offset:next});if(alive(key)){$("#reviewPatients").scrollTop=0;$("#reviewPager").querySelector(".review-page-label")?.focus({preventScroll:true});}
 }
 async function move(delta){const mrn=nav(delta);if(mrn)await select(mrn,{locate:true});}
 async function poll(task,revisions){if(!active()||reviewSignature!==taskSignature(task)||taskSignature(task,revisions)!==statusSignature)await load();}
 return {reset,active,patient,load,select,move,nav,poll,positionNotice,taskSignature};
})();
