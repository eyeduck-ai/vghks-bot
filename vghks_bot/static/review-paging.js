"use strict";

// The scrollbar represents the complete filtered list. Only nearby identities
// are retained; SOAP stays in a separate, small account/task-scoped cache.
window.ReviewPaging=(()=>{
 const pageSize=40,estimatedHeight=40,overscan=4,blocks=new Map(),details=new Map(),pending=new Map();
 const rowHeights=new Map(),heightDeltas=new Map(),observedRows=new Set(),pendingSizes=new Map();
 let owner="",pageData=null,detail=null,navigation=null,filtersKey="",collection="",listSequence=0,detailSequence=0,selectionSequence=0,statusSignature="",detailSignature="",busy=false;
 let scrollTimer=null,scrollJob=null,scrollWanted=false,scrollLoading=false,listError="",prefetchTimer=null,prefetchJob=null,prefetchSequence=0,boundRail=null;
 let resizeObserver=null,resizeFrame=null,railResized=false,railWidth=0;
 let contentWidth=184,textMeasure=null;
 const scope=()=>JSON.stringify([account,currentReview,epoch]);
 const taskSignature=(task,revisions={})=>JSON.stringify([task?.revision??task?.updated_at,task?.status,revisions.profile,revisions.review_note,revisions.patient_tags,revisions.preferences,revisions.records,revisions.registrations]);
 const visible=()=>page==="review"&&!document.hidden;
 function cancelPrefetch(){prefetchSequence++;clearTimeout(prefetchTimer);prefetchTimer=null;}
 function reset(){owner="";pageData=detail=navigation=null;filtersKey=collection=statusSignature=detailSignature=listError="";listSequence++;detailSequence++;selectionSequence++;busy=false;blocks.clear();details.clear();pending.clear();clearMeasurements();resetWidth();cancelPrefetch();clearTimeout(scrollTimer);scrollTimer=null;scrollWanted=false;}
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
 function clearMeasurements(){
  rowHeights.clear();heightDeltas.clear();pendingSizes.clear();
  for(const row of observedRows)resizeObserver.unobserve(row);
  observedRows.clear();
 }
 function resetWidth(){contentWidth=184;$("#reviewLayout").style.removeProperty?.("--review-patient-content-width");}
 function fitWidth(rail){
  if(rail.hidden||typeof getComputedStyle!=="function")return;
  const rows=[...rail.querySelectorAll(".review-patient-row")],sample=rows[0];if(!sample)return;
  textMeasure ||= document.createElement("canvas").getContext?.("2d");if(!textMeasure)return;
  const selectors=["strong",".review-sequence",".review-patient-id",".review-patient-dates"];
  const styles=selectors.map(selector=>getComputedStyle(rail.querySelector(selector)||sample.querySelector(".patient-select")));
  const fonts=styles.map(style=>`${style.fontWeight} ${style.fontSize} ${style.fontFamily}`);
  function textWidth(element,index){
   if(!element)return 0;
   textMeasure.font=fonts[index];const style=styles[index];
   return textMeasure.measureText(element.textContent).width+(parseFloat(style.paddingLeft)||0)+(parseFloat(style.paddingRight)||0);
  }
  const buttonStyle=getComputedStyle(sample.querySelector(".patient-select")),checkStyle=getComputedStyle(sample.querySelector(".review-patient-check"));
  const gap=parseFloat(buttonStyle.columnGap)||0;
  const chrome=(parseFloat(checkStyle.width)||24)+(parseFloat(buttonStyle.paddingLeft)||0)+(parseFloat(buttonStyle.paddingRight)||0)+Math.max(0,rail.offsetWidth-rail.clientWidth)+1;
  for(const row of rows){
   const name=textWidth(row.querySelector(selectors[0]),0),sequence=Math.min(72,textWidth(row.querySelector(selectors[1]),1));
   const identity=textWidth(row.querySelector(selectors[2]),2),date=Math.min(76,textWidth(row.querySelector(selectors[3]),3));
   contentWidth=Math.max(contentWidth,Math.min(248,Math.ceil(chrome+gap+Math.max(name+sequence,identity+date))));
  }
  const layout=$("#reviewLayout"),value=contentWidth+"px";
  if(layout.style.getPropertyValue("--review-patient-content-width")!==value)layout.style.setProperty("--review-patient-content-width",value);
 }
 function heightAt(index){return rowHeights.get(index)??estimatedHeight;}
 // A sparse Fenwick tree holds only differences from the estimated height.
 // Positions stay cheap to look up even after many pages have been measured.
 function rowTop(index){
  let position=index*estimatedHeight;
  for(let cursor=index;cursor>0;cursor-=cursor&-cursor)position+=heightDeltas.get(cursor)||0;
  return position;
 }
 function measure(index,height){
  const difference=height-heightAt(index);
  if(!difference)return false;
  rowHeights.set(index,height);
  for(let cursor=index+1;cursor<=pageData.total;cursor+=cursor&-cursor){
   heightDeltas.set(cursor,(heightDeltas.get(cursor)||0)+difference);
  }
  return true;
 }
 function indexAt(position,total){
  let low=0,high=Math.max(0,total-1);
  while(low<high){
   const middle=Math.ceil((low+high)/2);
   if(rowTop(middle)<=position)low=middle;else high=middle-1;
  }
  return low;
 }
 function range(position){
  const rail=$("#reviewPatients"),total=pageData?.total||0,height=rail.clientHeight||580;
  const scrollTop=Math.max(0,Math.min(position??rail.scrollTop,rowTop(total)-height));
  const top=indexAt(scrollTop,total),bottom=scrollTop+height;
  const next=indexAt(bottom,total),last=total?next+Number(rowTop(next)<bottom):0;
  return {top,last,start:Math.max(0,top-overscan),end:Math.min(total,last+overscan),total,scrollTop,atEnd:scrollTop>0&&bottom>=rowTop(total)-1};
 }
 function anchorAtViewport(){
  const {top,scrollTop,atEnd}=range();
  return {index:top,mrn:at(top)?.mrn,within:scrollTop-rowTop(top),height:heightAt(top),atEnd};
 }
 function scrollFor({index,within}){
  const row=Math.max(0,Math.min(index,(pageData?.total||0)-1));
  return rowTop(row)+Math.max(0,Math.min(within,heightAt(row)-1));
 }
 function sameAnchor(left,right){return left.index===right.index&&left.mrn===right.mrn&&Math.abs(left.within-right.within)<1;}
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
  const value=range();
  const waiting=busy||scrollLoading||!listError&&required().some(offset=>!blocks.has(offset));
  label.textContent=value.total?(value.top+1)+"–"+value.last+" / "+value.total+(waiting?" · 載入中":listError?" · 讀取失敗":""):"0 位病人";
  container.title=listError||"目前可視範圍／篩選後病人總數；捲動清單不會切換右側病歷。";
  container.setAttribute("aria-busy",String(waiting));$("#reviewPatients").setAttribute("aria-busy",String(waiting));
 }
 function renderWindow(position){
  if(!active())return;
  const area=range(position),positions={},rows=[];
  for(let index=area.start;index<area.end;index++){const row=at(index);if(row&&positions[row.mrn]===undefined){positions[row.mrn]=index;rows.push(row);}}
  const focused=document.activeElement,rail=$("#reviewPatients"),focusRow=rail.contains(focused)?focused?.closest?.(".review-patient-row"):null;
  const pinIndex=Number(focusRow?.dataset.reviewIndex),pin=focusRow&&pinIndex>=0&&pinIndex<area.total&&!rows.some(row=>row.mrn===focusRow.dataset.mrn)?{mrn:focusRow.dataset.mrn,top:rowTop(pinIndex)}:null;
  reviewData={...pageData,patients:rows};
  renderReviewPatients(rows,{...area,positions,pin,heightAt,before:rowTop(area.start),after:rowTop(area.total)-rowTop(area.end)});
  fitWidth(rail);
  fitReviewPatientTags(rail);
  observeMountedRows(rail);renderPager();updateNavigation();
 }
 function renderAnchored(anchor){
  // A new order or width invalidates old sizes. Keep the anchor's estimate
  // until this same patient's row can be measured in the new layout.
  if(anchor.height>estimatedHeight&&!rowHeights.has(anchor.index)&&at(anchor.index)?.mrn===anchor.mrn){
   measure(anchor.index,anchor.height);
  }
  renderWindow(scrollFor(anchor));
 }
 function validate(value){if(value.task?.id!==currentReview||!Array.isArray(value.patients)||!Number.isInteger(value.offset)||!Number.isInteger(value.total)||value.patients.length>pageSize)throw new Error("病人清單範圍不符，請重新開啟檢閱。");}
 function adopt(value,observedRevisions,{publish=true}={}){
  validate(value);
  const next=value.collection_revision||taskSignature(value.task,observedRevisions);
  if(collection&&collection!==next){blocks.clear();details.clear();clearMeasurements();cancelPrefetch();}
  else if(pageData&&pageData.total!==value.total)clearMeasurements();
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
    const oldCollection=collection,anchor=anchorAtViewport();
    let target=anchor;
    if(value.collection_revision&&oldCollection&&value.collection_revision!==oldCollection&&anchor.mrn){
     // A newly matching patient may have been inserted before this viewport
     // while its neighboring block was being read. Relocate the visible MRN,
     // rather than treating the old numeric offset as the same patient.
     const focused=await readBlock(Math.floor(anchor.index/pageSize)*pageSize,reviewFilters(),{focus_mrn:anchor.mrn});
     if(!alive(key)||sequence!==listSequence)return;
     target=anchorAtViewport();
     const index=focused.patients.findIndex(row=>row.mrn===anchor.mrn);
     adopt(focused,{...work?.revisions});
     if(index>=0&&focused.focus_found!==false&&sameAnchor(anchor,target))target={...anchor,index:focused.offset+index};
    }else adopt(value,{...work?.revisions});
    if(oldCollection!==collection)summary({...pageData,patients:[...blocks.get(pageData.offset)?.patients||[]]});
    renderAnchored(target);
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
 function observeMountedRows(rail){
  if(!resizeObserver)return;
  const mounted=new Set([...rail.children].filter(row=>row.dataset.mrn&&row.style.position!=="absolute"));
  for(const row of observedRows)if(!mounted.has(row)){resizeObserver.unobserve(row);observedRows.delete(row);}
  for(const row of mounted)if(!observedRows.has(row)){resizeObserver.observe(row,{box:"border-box"});observedRows.add(row);}
 }
 function applyMeasurements(){
  resizeFrame=null;
  const rail=boundRail,resized=railResized;railResized=false;
  if(!active()||!visible()||rail.hidden){pendingSizes.clear();return;}
  const anchor=anchorAtViewport(),width=rail.clientWidth;
  const widthChanged=width>0&&railWidth>0&&width!==railWidth;
  if(width>0)railWidth=width;
  let changed=widthChanged;
  if(widthChanged)clearMeasurements();
  else for(const [row,height] of pendingSizes){
   const index=Number(row.dataset.reviewIndex);
   if(observedRows.has(row)&&at(index)?.mrn===row.dataset.mrn&&measure(index,height))changed=true;
  }
  pendingSizes.clear();
  if(changed||resized){
   if(anchor.atEnd)renderWindow(Infinity);else renderAnchored(anchor);
   scheduleScroll();
  }
 }
 function queueMeasurements(entries,rail){
  if(rail!==boundRail||!active()||!visible()||rail.hidden)return;
  for(const entry of entries){
   if(entry.target===rail){railResized=true;continue;}
   if(!observedRows.has(entry.target))continue;
   const height=entry.borderBoxSize?.[0]?.blockSize??entry.target.getBoundingClientRect().height;
   if(Number.isFinite(height)&&height>0)pendingSizes.set(entry.target,height);
  }
  if(resizeFrame!==null||!railResized&&!pendingSizes.size)return;
  if(typeof requestAnimationFrame==="function")resizeFrame=requestAnimationFrame(applyMeasurements);
  else applyMeasurements();
 }
 function bind(){
  const rail=$("#reviewPatients");if(rail===boundRail)return;
  resizeObserver?.disconnect();observedRows.clear();pendingSizes.clear();boundRail=rail;railWidth=rail.clientWidth||0;
  rail.addEventListener("scroll",()=>{if(!active()||!visible())return;listError="";renderWindow();scheduleScroll();},{passive:true});
  rail.addEventListener("focusout",()=>{if(active())setTimeout(()=>{if(active())renderWindow();},0);});
  if(typeof ResizeObserver!=="undefined"){
   resizeObserver=new ResizeObserver(entries=>queueMeasurements(entries,rail));
   resizeObserver.observe(rail);
  }
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
  const resetList=filtersKey!==nextFilters||resetPage;
  if(resetList){blocks.clear();collection="";details.clear();clearMeasurements();resetWidth();rail.scrollTop=0;detailSequence++;selectionSequence++;}
  const anchor=anchorAtViewport(),jump=Number.isInteger(requestedOffset),requestIndex=jump?Math.max(0,requestedOffset):anchor.index;
  filtersKey=nextFilters;listError="";cancelPrefetch();const sequence=++listSequence,requestSelected=reviewPatient;busy=true;renderPager();
  try{
   const focus=focusMrn||(!resetList&&!jump?anchor.mrn:"")||"",offset=Math.floor(requestIndex/pageSize)*pageSize;
   const value=await readBlock(offset,filters,focus?{focus_mrn:focus}:{});
   if(!alive(key)||sequence!==listSequence)return;
   let target=anchorAtViewport();
   const stationary=sameAnchor(anchor,target);
   adopt(value,observedRevisions);reviewSignature=taskSignature(value.task);
   if(focus&&value.focus_found!==false&&(focusMrn||stationary)){
    const index=value.patients.findIndex(row=>row.mrn===focus);
    if(index>=0)target=focusMrn?{index:value.offset+index,within:0}:{...target,index:value.offset+index};
   }else if(jump&&stationary)target={index:requestIndex,within:0};
   if(!reviewPatient)reviewPatient=value.patients[0]?.mrn||"";
   summary(value);renderAnchored(target);
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
