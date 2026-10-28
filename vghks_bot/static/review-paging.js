"use strict";

// The scrollbar represents the complete filtered list. Only nearby identities
// are retained; SOAP stays in a separate, small account/task-scoped cache.
window.ReviewPaging=(()=>{
 const pageSize=40,rowHeight=72,overscan=4,blocks=new Map(),details=new Map(),pending=new Map();
 let owner="",pageData=null,detail=null,navigation=null,filtersKey="",collection="",listSequence=0,detailSequence=0,selectionSequence=0,statusSignature="",detailSignature="",busy=false;
 let scrollTimer=null,scrollJob=null,scrollWanted=false,scrollLoading=false,listError="",prefetchTimer=null,prefetchJob=null,prefetchSequence=0,boundRail=null;
 const scope=()=>JSON.stringify([account,currentReview,epoch]);
 const taskSignature=(task,revisions={})=>JSON.stringify([task?.revision??task?.updated_at,task?.status,revisions.profile,revisions.review_note,revisions.patient_tags,revisions.preferences,revisions.records,revisions.registrations]);
 const visible=()=>page==="review"&&!document.hidden;
 function cancelPrefetch(){prefetchSequence++;clearTimeout(prefetchTimer);prefetchTimer=null;}
 function reset(){owner="";pageData=detail=navigation=null;filtersKey=collection=statusSignature=detailSignature=listError="";listSequence++;detailSequence++;selectionSequence++;busy=false;blocks.clear();details.clear();pending.clear();cancelPrefetch();clearTimeout(scrollTimer);scrollTimer=null;scrollWanted=false;}
 function ensure(){const key=scope();if(key!==owner){reset();owner=key;}bind();return key;}
 function active(){return owner===scope()&&!!pageData;}
 function patient(){return active()&&detail?.mrn===reviewPatient?detail:null;}
 function request(path,values){
  const key=JSON.stringify([owner,path,values]);if(pending.has(key))return pending.get(key);
  const promise=api(path,values);pending.set(key,promise);
  return promise.finally(()=>{if(pending.get(key)===promise)pending.delete(key);});
 }
 function alive(key){return key===owner&&key===scope()&&page==="review";}
 function saveDetail(key,value){details.delete(key);details.set(key,value);while(details.size>8)details.delete(details.keys().next().value);}
 function at(index){for(const value of blocks.values()){const row=value.patients[index-value.offset];if(index>=value.offset&&row)return row;}return null;}
 function find(mrn){for(const value of blocks.values()){const index=value.patients.findIndex(row=>row.mrn===mrn);if(index>=0)return {row:value.patients[index],index:value.offset+index,revision:value.patient_revisions?.[mrn]||value.patients[index].data_revision};}return null;}
 function range(){const total=pageData?.total||0,top=Math.max(0,Math.min(Math.floor($("#reviewPatients").scrollTop/rowHeight),Math.max(0,total-1))),count=Math.ceil(($("#reviewPatients").clientHeight||580)/rowHeight);return {top,start:Math.max(0,top-overscan),end:Math.min(total,top+count+overscan),total};}
 function required(){const value=range(),result=[];for(let index=Math.floor(value.start/pageSize)*pageSize;index<value.end;index+=pageSize)result.push(index);return result;}
 function trimBlocks(){const protect=new Set(required());while(blocks.size>3){const key=[...blocks.keys()].find(key=>!protect.has(key))??blocks.keys().next().value;blocks.delete(key);}}
 function applyNavigation(next){navigation=next;updateNavigation();}
 function updateNavigation(){
  if(!active())return;
  for(const button of $("#reviewDetail").querySelectorAll("[data-review-move]"))button.disabled=!navigation?.[Number(button.dataset.reviewMove)<0?"previous_mrn":"next_mrn"];
  const notice=$("#reviewDetail").querySelector(".review-page-context");if(!notice)return;
  const current=patient(),area=range(),index=current?find(current.mrn)?.index:null,outside=!!current&&(index==null||index<area.start||index>=area.end);
  notice.hidden=!outside;
  notice.textContent=outside?(navigation?.position==null?"目前檢閱的病人不符合此篩選條件；可繼續閱讀已選病歷。":"目前檢閱的病人在清單可視範圍外；捲動清單不會切換右側病歷。") : "";
 }
 function positionNotice(){const value=node("p","","caption review-page-context");value.hidden=true;return value;}
 function nav(delta){return navigation?.[delta<0?"previous_mrn":"next_mrn"]||"";}
 function renderPager(){
  const container=$("#reviewPager");if(!container||!pageData)return;
  container.hidden=$("#reviewPatients").hidden;
  let label=container.querySelector(".review-page-label");
  if(!label){label=node("span","","caption review-page-label");container.replaceChildren(label);}
  const value=range(),last=Math.min(value.total,value.top+Math.ceil(($("#reviewPatients").clientHeight||580)/rowHeight));
  const waiting=busy||scrollLoading||!listError&&required().some(offset=>!blocks.has(offset));
  label.textContent=value.total?(value.top+1)+"–"+last+" / "+value.total+(waiting?" · 載入中":listError?" · 讀取失敗":""):"0 位病人";
  container.title=listError||"目前可視範圍／篩選後病人總數；捲動清單不會切換右側病歷。";
  container.setAttribute("aria-busy",String(waiting));$("#reviewPatients").setAttribute("aria-busy",String(waiting));
 }
 function renderWindow(){
  if(!active())return;
  const area=range(),positions={},rows=[];
  for(let index=area.start;index<area.end;index++){const row=at(index);if(row&&positions[row.mrn]===undefined){positions[row.mrn]=index;rows.push(row);}}
  const focused=document.activeElement,rail=$("#reviewPatients"),focusRow=rail.contains(focused)?focused?.closest?.(".review-patient-row"):null;
  const pinIndex=Number(focusRow?.dataset.reviewIndex),pin=focusRow&&pinIndex>=0&&pinIndex<area.total&&!rows.some(row=>row.mrn===focusRow.dataset.mrn)?{mrn:focusRow.dataset.mrn,index:pinIndex}:null;
  reviewData={...pageData,patients:rows};
  renderReviewPatients(rows,{...area,rowHeight,positions,pin});renderPager();updateNavigation();
 }
 function validate(value){if(value.task?.id!==currentReview||!Array.isArray(value.patients)||!Number.isInteger(value.offset)||!Number.isInteger(value.total)||value.patients.length>pageSize)throw new Error("病人清單範圍不符，請重新開啟檢閱。");}
 function adopt(value,observedRevisions,{publish=true}={}){
  validate(value);
  const next=value.collection_revision||taskSignature(value.task,observedRevisions);
  if(collection&&collection!==next){blocks.clear();details.clear();cancelPrefetch();}
  collection=next;if(publish){pageData={...value,patients:[]};reviewData=pageData;}blocks.delete(value.offset);blocks.set(value.offset,value);trimBlocks();
 }
 function readBlock(offset,filters,extra={}){return request("/reviews/patients",{id:currentReview,offset,limit:pageSize,...filters,...(reviewPatient?{selected_mrn:reviewPatient}:{}),...extra});}
 function summary(value){
  window.ToolWorkspace?.adoptReview(value.task);renderReviewHeader(value);
  $("#reviewSummary").textContent=value.total+" / "+value.all_total+" 位"+(value.incremental?" · 新增 "+value.incremental.new+" · 沿用 "+value.incremental.reused+" · 待處理 "+value.incremental.pending:"")+(value.scope_issue_count?" · "+value.scope_issue_count+" 份病歷的 tag 判定不完整":"");
  $("#reviewSummary").title=$("#reviewSummary").textContent;
 }
 async function fillWindow(key,sequence){
  // One lane follows the latest scroll position instead of queuing every
  // intermediate thumb position. A request already sent is allowed to finish.
  for(let pass=0;pass<3&&alive(key)&&sequence===listSequence;pass++){
   const missing=required().filter(offset=>!blocks.has(offset));if(!missing.length)return;
   for(const offset of missing){
    const value=await readBlock(offset,reviewFilters());
    if(!alive(key)||sequence!==listSequence)return;
    const oldCollection=collection,rail=$("#reviewPatients"),anchor=at(range().top),anchorScroll=rail.scrollTop;
    if(value.collection_revision&&oldCollection&&value.collection_revision!==oldCollection&&anchor){
     // A newly matching patient may have been inserted before this viewport
     // while its neighboring block was being read. Relocate the visible MRN,
     // rather than treating the old numeric offset as the same patient.
     const focused=await readBlock(Math.floor(range().top/pageSize)*pageSize,reviewFilters(),{focus_mrn:anchor.mrn});
     if(!alive(key)||sequence!==listSequence)return;
     adopt(focused,{...work?.revisions});
     if(rail.scrollTop===anchorScroll&&focused.focus_found!==false){const index=focused.patients.findIndex(row=>row.mrn===anchor.mrn);if(index>=0)rail.scrollTop=(focused.offset+index)*rowHeight+anchorScroll%rowHeight;}
    }else adopt(value,{...work?.revisions});
    if(oldCollection!==collection)summary({...pageData,patients:[...blocks.get(pageData.offset)?.patients||[]]});
    renderWindow();
   }
  }
 }
 async function loadViewport(){
  if(!active()||!visible())return;
  scrollWanted=true;if(scrollJob||busy)return scrollJob;
  const key=owner,sequence=listSequence;scrollLoading=true;renderPager();
  scrollJob=(async()=>{do{scrollWanted=false;await fillWindow(key,sequence);}while(scrollWanted&&alive(key)&&sequence===listSequence&&visible());})().catch(error=>{if(alive(key)&&sequence===listSequence){listError=error.message||"本機病人清單讀取失敗，請重新捲動以重試。";say(listError,true);}}).finally(()=>{scrollLoading=false;scrollJob=null;if(active())renderPager();if(scrollWanted&&active()&&visible())scheduleScroll();});
  return scrollJob;
 }
 function scheduleScroll(){clearTimeout(scrollTimer);scrollTimer=setTimeout(()=>{scrollTimer=null;loadViewport();},65);}
 function bind(){
  const rail=$("#reviewPatients");if(rail===boundRail)return;boundRail=rail;
  rail.addEventListener("scroll",()=>{if(!active()||!visible())return;listError="";renderWindow();scheduleScroll();},{passive:true});
  rail.addEventListener("focusout",()=>{if(active())setTimeout(()=>{if(active())renderWindow();},0);});
  if(typeof ResizeObserver!=="undefined")new ResizeObserver(()=>{if(active()){renderWindow();scheduleScroll();}}).observe(rail);
 }
 async function loadDetail(mrn,{revision,loading=false,notesPromise=null}={}){
  const key=ensure(),sequence=++detailSequence,filters=reviewFilters();
  const cacheKey=JSON.stringify([mrn,filters,revision||""]);
  if(loading)$("#reviewDetail").replaceChildren(ClinicalUI.loading("正在讀取病歷…"));
  try{
   const [value]=await Promise.all([revision&&details.has(cacheKey)?details.get(cacheKey):request("/reviews/patient",{id:currentReview,mrn,...filters,local_only:true}),notesPromise]);
   if(!alive(key)||sequence!==detailSequence||mrn!==reviewPatient)return;
   if(!value.patient||value.patient.mrn!==mrn||value.task_id!==currentReview)throw new Error("病歷資料範圍不符，請重新選擇病人。");
   const nextSignature=JSON.stringify([mrn,value.revision,filters]),first=detail?.mrn!==mrn,changed=first||nextSignature!==detailSignature;
   detail=value.patient;detailSignature=nextSignature;saveDetail(JSON.stringify([mrn,filters,value.revision]),value);applyNavigation(value);
   if(changed){
    const panel=$("#reviewDetail"),scroll=loading?0:panel.scrollTop,focus=document.activeElement;
    const editing=panel.contains(focus)&&focus.id==="reviewNoteInput"?{start:focus.selectionStart,end:focus.selectionEnd}:null;
    const focusIdentity=panel.contains(focus)&&!editing?{tag:focus.tagName,id:focus.id,label:focus.getAttribute?.("aria-label"),text:focus.textContent}:null;
    renderReviewDetail();panel.scrollTop=scroll;
    if(editing){const input=$("#reviewNoteInput");input?.focus({preventScroll:true});input?.setSelectionRange?.(editing.start,editing.end);}
    else if(focusIdentity){const next=[...panel.querySelectorAll("button,a,input,summary")].find(element=>element.tagName===focusIdentity.tag&&(focusIdentity.id?element.id===focusIdentity.id:focusIdentity.label?element.getAttribute("aria-label")===focusIdentity.label:element.textContent===focusIdentity.text));if(next&&!next.disabled)next.focus({preventScroll:true});}
   }
   updateNavigation();schedulePrefetch();
  }catch(error){
   if(alive(key)&&sequence===detailSequence&&mrn===reviewPatient){
    if(loading)$("#reviewDetail").replaceChildren(node("p",error.message||"病歷讀取失敗。","error"),btn("重試讀取",()=>loadDetail(mrn,{loading:true})));
    else say(error.message||"病歷讀取失敗，原內容已保留。",true);
   }
   throw error;
  }
 }
 function schedulePrefetch(){
  cancelPrefetch();if(!active()||!visible()||!patient()||navigation?.position==null)return;
  const sequence=prefetchSequence;
  prefetchTimer=setTimeout(()=>{
   prefetchTimer=null;if(sequence!==prefetchSequence||!visible())return;
   if(prefetchJob){prefetchJob.finally(()=>{if(sequence===prefetchSequence)schedulePrefetch();});return;}
   prefetchJob=prefetch(sequence).catch(()=>{}).finally(()=>{prefetchJob=null;});
  },180);
 }
 async function prefetch(sequence){
  const key=owner,filters=reviewFilters(),filter=filtersKey,generation=collection,mrn=reviewPatient,position=navigation?.position,total=navigation?.total||0;
  const valid=()=>alive(key)&&visible()&&sequence===prefetchSequence&&filter===filtersKey&&generation===collection&&mrn===reviewPatient;
  for(const index of [position+1,position+2,position-1]){
   if(!valid())return;if(index<0||index>=total)continue;
   let row=at(index);
   if(!row){
    const value=await readBlock(Math.floor(index/pageSize)*pageSize,filters);
    if(!valid())return;validate(value);
    if(value.collection_revision&&value.collection_revision!==generation)return;
    adopt(value,{...work?.revisions},{publish:false});row=at(index);
   }
   if(!row||row.status!=="ready"||row.processing||!row.record_count)continue;
   const revision=find(row.mrn)?.revision;if(!revision)continue;
   const cacheKey=JSON.stringify([row.mrn,filters,revision]);if(details.has(cacheKey))continue;
   const value=await request("/reviews/patient",{id:currentReview,mrn:row.mrn,...filters,local_only:true,prefetch:true});
   if(!valid())return;
   if(value.task_id!==currentReview||value.mrn!==row.mrn||value.revision!==revision||!value.prefetchable||value.patient?.mrn!==row.mrn||value.patient.status!=="ready"||value.patient.processing||!value.patient.records?.length)continue;
   saveDetail(cacheKey,value);
  }
 }
 async function load({resetPage=false,focusMrn="",offset:requestedOffset}={}){
  if(!currentReview)return;
  const key=ensure(),filters=reviewFilters(),nextFilters=JSON.stringify(filters),observedRevisions={...work?.revisions},rail=$("#reviewPatients");
  const changedFilter=filtersKey!==nextFilters,anchor=!changedFilter&&!resetPage&&!focusMrn&&!Number.isInteger(requestedOffset)?at(Math.floor(rail.scrollTop/rowHeight)):null,fraction=rail.scrollTop%rowHeight;
  if(changedFilter||resetPage){blocks.clear();collection="";details.clear();rail.scrollTop=0;detailSequence++;selectionSequence++;}
  if(Number.isInteger(requestedOffset))rail.scrollTop=Math.max(0,requestedOffset)*rowHeight;
  filtersKey=nextFilters;listError="";cancelPrefetch();const sequence=++listSequence,requestScroll=rail.scrollTop,requestSelected=reviewPatient;busy=true;renderPager();
  try{
   const focus=focusMrn||anchor?.mrn||"",offset=Math.floor(rail.scrollTop/rowHeight/pageSize)*pageSize;
   const value=await readBlock(offset,filters,focus?{focus_mrn:focus}:{});
   if(!alive(key)||sequence!==listSequence)return;
   adopt(value,observedRevisions);reviewSignature=taskSignature(value.task);
   if(focus&&value.focus_found!==false&&(focusMrn||rail.scrollTop===requestScroll)){const index=value.patients.findIndex(row=>row.mrn===focus);if(index>=0)rail.scrollTop=(value.offset+index)*rowHeight+(focusMrn?0:fraction);}
   else rail.scrollTop=Math.min(rail.scrollTop,Math.max(0,value.total*rowHeight-(rail.clientHeight||580)));
   if(!reviewPatient)reviewPatient=value.patients[0]?.mrn||"";
   summary(value);renderWindow();
   const notes=window.ReviewNotesUI?.load(currentReview,{mrns:reviewPatient?[reviewPatient]:[]});
   const revision=reviewPatient?(value.patient_revisions?.[reviewPatient]||(reviewPatient===requestSelected?value.selected_revision:null)):null;
   if(value.selected_navigation&&reviewPatient===requestSelected&&detail?.mrn===reviewPatient)applyNavigation(value.selected_navigation);
   if(reviewPatient&&(!patient()||!revision||JSON.stringify([reviewPatient,revision,filters])!==detailSignature))await loadDetail(reviewPatient,{revision,loading:!patient(),notesPromise:notes});
   else{if(notes)await notes;if(!alive(key)||sequence!==listSequence)return;updateNavigation();schedulePrefetch();}
   if(!alive(key)||sequence!==listSequence)return;
   await fillWindow(key,sequence);if(!alive(key)||sequence!==listSequence)return;renderWindow();
   if(!reviewPatient){detail=null;detailSignature="";$("#reviewDetail").replaceChildren(empty("沒有符合的病人。"));}
   statusSignature=taskSignature(value.task,observedRevisions);
  }finally{if(alive(key)&&sequence===listSequence){busy=false;renderPager();if(scrollWanted)scheduleScroll();}}
 }
 async function select(mrn,{locate=false}={}){
  const key=ensure(),previous=reviewPatient,selection=++selectionSequence;cancelPrefetch();
  if(previous!==mrn&&!await window.ReviewNotesUI.flush(previous))throw new Error("有備註尚未儲存，請重試後再切換病人。");
  if(!alive(key)||selection!==selectionSequence)return;
  if(previous!==mrn){window.ScanBrowser?.reset();window.FileCompare?.context(account,mrn);}
  reviewPatient=mrn;
  for(const element of $$(".patient-select"))element.classList.toggle("active",element.dataset.mrn===mrn);
  const target=find(mrn);if(locate||!target)return load({focusMrn:mrn});
  const notes=window.ReviewNotesUI?.load(currentReview,{mrns:[mrn]});
  await loadDetail(mrn,{revision:target.revision,loading:previous!==mrn,notesPromise:notes});
 }
 async function move(delta){const mrn=nav(delta);if(mrn)await select(mrn,{locate:true});}
 async function poll(task,revisions){if(!active()||reviewSignature!==taskSignature(task)||taskSignature(task,revisions)!==statusSignature)await load();}
 function visibility(){if(!visible()){cancelPrefetch();clearTimeout(scrollTimer);scrollTimer=null;scrollWanted=false;}else if(active()){renderWindow();scheduleScroll();schedulePrefetch();}}
 document.addEventListener?.("visibilitychange",visibility);
 return {reset,active,patient,load,select,move,nav,poll,positionNotice,taskSignature,visibility};
})();
