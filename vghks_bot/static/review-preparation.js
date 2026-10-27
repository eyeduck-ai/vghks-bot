"use strict";

window.ReviewPreparationUI=(()=>{
 const activeStates=new Set(["queued","waiting","running"]);
 const names={idle:"尚未啟動",queued:"等待準備",waiting:"等待空閒",running:"準備中",paused:"已暫停",completed:"本輪已完成",partial:"部分完成"};
 const resourceNames={soap:"本次 SOAP",numeric:"歷年數值",orders:"醫囑索引",profile:"病人基本資料",reports:"眼科報告",attachments:"圖片與 PDF"};
 let owner="",task=null,total=0,summary=null,globalSummary=null,sequence=0,readRequest=null,dialogContext=null,panel=null,signature="",observedGlobal="";
 const known=new Map();
 const scope=()=>JSON.stringify([account,currentReview,epoch]);
 const accountScope=()=>JSON.stringify([account,epoch]);
 let accountOwner="";
 function reset(){
  owner="";task=null;total=0;summary=null;sequence++;readRequest=null;signature=observedGlobal="";
  if(panel?.open)panel.close();dialogContext=null;
  const container=$("#reviewPreparation");if(container)container.replaceChildren();
 }
 function synchronize(){
  const nextAccount=accountScope();
  if(accountOwner!==nextAccount){accountOwner=nextAccount;globalSummary=null;known.clear();reset();}
  if(owner!==scope()){reset();owner=scope();}
 }
 function remember(value){
  if(value?.preparation_id){known.set(value.preparation_id,value);while(known.size>8)known.delete(known.keys().next().value);}
 }
 function publish(value){
  if(!value?.preparation_id||value.state==="idle")return;
  remember(value);
  window.JobProgress?.publish({id:value.preparation_id,kind:"review_preparation",source:"preparation",name:value.name||"背景準備病歷",
   status:activeStates.has(value.state)?(value.state==="running"?"running":"queued"):value.state,
   created_at:value.created_at,updated_at:value.updated_at,message:value.reason||"",progress:value.progress},account);
 }
 function relevant(){return globalSummary&&globalSummary.review_task_id!==currentReview&&activeStates.has(globalSummary.state)?globalSummary:summary;}
 function render(){
  const container=$("#reviewPreparation");if(!container||!task||owner!==scope())return;
  const value=relevant(),other=value?.review_task_id&&value.review_task_id!==currentReview;
  const next=JSON.stringify([owner,root?.read_only,work?.online,value,total]);if(signature===next)return;signature=next;
  const focused=document.activeElement,action=container.contains(focused)?focused.dataset.preparationAction:"";
  const status=node("span",other?"另一份檢閱正在背景準備":`背景準備 · ${names[value?.state]||"尚未啟動"}`,"review-preparation-status");
  const details=node("span","","caption");
  if(value?.scope_count)details.textContent=`${value.scope_count} 位 · ${value.progress?.done||0} / ${value.progress?.total||0} 項已準備`+(value.failed_count?` · ${value.failed_count} 項未完成`:"")+(value.blocked_count?` · ${value.blocked_count} 項待處理`:"");
  const settings=btn("背景準備",()=>open(),"quiet");settings.dataset.preparationAction="settings";settings.setAttribute("aria-haspopup","dialog");
  container.replaceChildren(status,details,settings);
  const control=(label,operation)=>{const button=btn(label,()=>perform(operation,value),"quiet");button.dataset.preparationAction=operation;button.disabled=!!root?.read_only||(operation!=="pause"&&!work?.online);container.append(button);};
  if(activeStates.has(value?.state))control("暫停準備","pause");
  else if(value?.state==="paused")control("繼續準備","resume");
  else if(value?.state==="partial"){
   control("重試未完成","retry");container.append(node("span","重試會重新取得未完成及已清除的資料。","caption"));
  }
  if(value?.reason&&value.state!=="completed"){const reason=node("span",value.reason,"caption review-preparation-reason");container.append(reason);}
  if(action)[...container.querySelectorAll("button")].find(button=>button.dataset.preparationAction===action)?.focus({preventScroll:true});
 }
 async function read(){
  synchronize();if(!task||!account)return;
  const key=owner,id=currentReview;if(readRequest?.key===key)return readRequest.promise;
  const serial=++sequence;
  const promise=api("/reviews/preparation/read",{review_task_id:id}).then(value=>{
   if(key!==owner||key!==scope()||serial!==sequence||value.review_task_id!==id)return;
   summary=value;remember(value);render();return value;
  }).catch(error=>{if(key!==owner||key!==scope()||serial!==sequence)throw new DOMException("檢閱範圍已切換","AbortError");throw error;})
   .finally(()=>{if(readRequest?.promise===promise)readRequest=null;});
  readRequest={key,promise};return promise;
 }
 function context(next,values={}){
  synchronize();if(!next?.id||next.id!==currentReview)return;
  const first=!task;task=next;if(Number.isFinite(values.total))total=values.total;
  const key=owner;
  render();if(first)void read().catch(error=>{if(key===owner&&key===scope()&&error.name!=="AbortError")say(error.message||"背景準備狀態讀取失敗。",true);});
 }
 function poll(value){
  synchronize();const next=value?.review_preparation;
  if(next){globalSummary=next;remember(next);publish(next);if(next.review_task_id===currentReview)summary=next;}
  if(task&&next?.preparation_id&&next.review_task_id!==currentReview&&observedGlobal!==next.preparation_id){
   observedGlobal=next.preparation_id;const key=owner;
   void read().catch(error=>{if(key===owner&&key===scope()&&error.name!=="AbortError")say(error.message||"背景準備狀態讀取失敗。",true);});
  }
  render();
 }
 function field(labelText,control){const label=node("label",labelText);label.append(control);return label;}
 function buildPanel(){
  if(panel)return;
  panel=node("dialog",undefined,"review-preparation-dialog");panel.id="reviewPreparationDialog";panel.setAttribute("aria-labelledby","reviewPreparationTitle");
  const form=node("form");form.id="reviewPreparationForm";
  const heading=node("div",undefined,"dialog-heading"),title=node("h2","背景準備此清單");title.id="reviewPreparationTitle";
  const close=btn("×",()=>panel.close(),"quiet");close.setAttribute("aria-label","關閉背景準備");heading.append(title,close);
  const patientScope=node("select");patientScope.id="reviewPreparationScope";
  const intro=node("p","先使用已保存的資料，只補缺漏；前景查詢優先，沿用原有抓取間隔與病人核對。","caption");
  const choices=node("div",undefined,"review-preparation-choices");
  for(const [name,label] of Object.entries(resourceNames)){
   const input=node("input");input.type="checkbox";input.id="prepare-"+name;input.name=name;
   const item=node("label",undefined,"check");item.append(input,node("span",label));choices.append(item);
   input.addEventListener("change",()=>{
    if(name==="attachments"&&input.checked){form.elements.reports.checked=true;form.elements.orders.checked=true;}
    if(name==="reports"){if(input.checked)form.elements.orders.checked=true;else form.elements.attachments.checked=false;}
    if(name==="orders"&&!input.checked){form.elements.reports.checked=false;form.elements.attachments.checked=false;}
    budgets();
   });
  }
  const limits=node("div",undefined,"review-preparation-limits");
  for(const [name,label,max,value] of [["max_reports","每位最多眼科報告",5,3],["max_attachments","每位最多附件",20,10]]){
   const input=node("input");input.type="number";input.name=name;input.min="1";input.max=String(max);input.value=String(value);input.required=true;
   limits.append(field(label,input));
  }
  const budget=node("p","","caption");budget.id="reviewPreparationBudget";
  const result=node("div");result.id="reviewPreparationResult";result.setAttribute("role","status");
  const actions=node("div",undefined,"actions"),cancel=btn("關閉",()=>panel.close());cancel.autofocus=true;
  const submit=node("button","開始背景準備","primary");submit.type="submit";submit.id="reviewPreparationSubmit";actions.append(cancel,submit);
  const currentActions=node("div",undefined,"actions review-preparation-current-actions");currentActions.id="reviewPreparationActions";
  form.append(heading,intro,field("準備範圍",patientScope),choices,limits,budget,result,currentActions,actions);panel.append(form);document.body.append(panel);
  form.addEventListener("submit",event=>{event.preventDefault();void act(submitPreparation,submit);});
  patientScope.addEventListener("change",budgets);
  for(const input of limits.querySelectorAll("input"))input.addEventListener("input",budgets);
  panel.addEventListener("close",()=>{dialogContext=null;});
 }
 function budgets(){
  if(!panel||!dialogContext)return;
  const form=$("#reviewPreparationForm"),elements=form.elements;
  elements.max_reports.disabled=!elements.reports.checked;elements.max_attachments.disabled=!elements.attachments.checked;
  const count=$("#reviewPreparationScope").value==="selected"?dialogContext.mrns.length:dialogContext.total;
  $("#reviewPreparationBudget").textContent=`本輪準備 ${count} 位病人`+(elements.reports.checked?`，每位最多 ${elements.max_reports.value} 份眼科報告`:"")+(elements.attachments.checked?`、${elements.max_attachments.value} 份附件`:"")+"。超出上限的內容仍可手動開啟；取得索引可能包含其他科別。"+(elements.attachments.checked?"新附件取得失敗也計入額度，已存附件直接重用。":"");
 }
 async function open(){
  synchronize();if(!task)return;buildPanel();
  const key=owner,id=currentReview;
  await read();if(key!==owner||key!==scope()||id!==currentReview)return;
  const selected=(window.ReviewHandoff?.patients()||[]).map(patient=>patient.mrn);
  dialogContext={key,taskId:id,mrns:selected,total:total||summary?.scope_count||0};
  const select=$("#reviewPreparationScope");select.replaceChildren(new Option(`整份檢閱名單（${dialogContext.total} 位）`,"all"),new Option(`已勾選病人（${selected.length} 位）`,"selected"));
  select.options[1].disabled=!selected.length;select.value=selected.length?"selected":"all";
  const options=summary?.options||{},form=$("#reviewPreparationForm");
  for(const name of Object.keys(resourceNames))form.elements[name].checked=options[name]??name==="soap";
  form.elements.max_reports.value=String(options.max_reports||3);form.elements.max_attachments.value=String(options.max_attachments||10);
  $("#reviewPreparationSubmit").disabled=!!root?.read_only||!work?.online||!dialogContext.total;
  $("#reviewPreparationResult").replaceChildren();
  if(root?.read_only)$("#reviewPreparationResult").append(node("p","唯讀資料庫可查看準備狀態，無法開始準備。","caption"));
  else if(!work?.online)$("#reviewPreparationResult").append(node("p","請先連線院內系統；已保存資料仍可閱讀。","caption"));
  if(summary?.reason)$("#reviewPreparationResult").append(node("p",summary.reason,"caption"));
  for(const error of (summary?.errors||[]).slice(0,20))$("#reviewPreparationResult").append(node("p",[error.mrn,resourceNames[error.resource]||"",error.message].filter(Boolean).join(" · "),"caption"));
  const currentActions=$("#reviewPreparationActions");currentActions.replaceChildren();
  const operation=activeStates.has(summary?.state)?"pause":summary?.state==="paused"?"resume":summary?.state==="partial"?"retry":"";
  if(operation){
   const control=btn({pause:"暫停目前準備",resume:"繼續目前準備",retry:"重試未完成"}[operation],async()=>{
    const saved=dialogContext;await perform(operation,summary);if(saved===dialogContext&&saved?.key===scope())panel.close();
   },"quiet");control.disabled=!!root?.read_only||(operation!=="pause"&&!work?.online);currentActions.append(control);
   if(operation==="retry")currentActions.append(node("span","會重新取得未完成及已清除的資料。","caption"));
  }
  budgets();if(!panel.open)panel.showModal();
 }
 async function submitPreparation(){
  const saved=dialogContext;
  if(!saved||saved.key!==scope())throw new DOMException("檢閱範圍已切換","AbortError");
  const form=$("#reviewPreparationForm");if(!form.reportValidity())return;
  const options=Object.fromEntries(Object.keys(resourceNames).map(name=>[name,form.elements[name].checked]));
  options.max_reports=Number(form.elements.max_reports.value);options.max_attachments=Number(form.elements.max_attachments.value);
  if(!Object.keys(resourceNames).some(name=>options[name]))throw new Error("請至少選擇一項準備內容。");
  const values={review_task_id:saved.taskId,action:"start",options};
  if($("#reviewPreparationScope").value==="selected"){if(!saved.mrns.length)throw new Error("請先勾選病人。");values.mrns=saved.mrns;}
  await send(values,saved.key);
  if(saved===dialogContext&&saved.key===scope())panel.close();
 }
 async function send(values,key=scope()){
  if(root?.read_only)throw new Error("唯讀資料庫無法變更背景準備。");
  if(values.action!=="pause"&&!work?.online)throw new Error("請先連線院內系統，再繼續背景準備。");
  const ownerAccount=account,ownerEpoch=epoch;
  let result;
  try{result=await api("/reviews/preparation/action",values);}catch(error){
   if(ownerAccount!==account||ownerEpoch!==epoch||key!==scope())throw new DOMException("檢閱範圍已切換","AbortError");throw error;
  }
  if(ownerAccount!==account||ownerEpoch!==epoch||key!==scope())throw new DOMException("檢閱範圍已切換","AbortError");
  if(result.review_task_id!==values.review_task_id)throw new Error("背景準備範圍不符。");
  remember(result);globalSummary=result;if(result.review_task_id===currentReview)summary=result;
  publish(result);signature="";render();
  // Bootstrap the existing status poll's active cadence without adding a timer.
  if(typeof refresh==="function")void Promise.resolve().then(()=>refresh({light:true})).catch(error=>{if(ownerAccount===account&&ownerEpoch===epoch&&key===scope()&&error.name!=="AbortError")say(error.message||"背景準備狀態更新失敗。",true);});
  return result;
 }
 async function perform(action,value=summary){
  if(!value?.review_task_id)return;
  await send({review_task_id:value.review_task_id,action});
 }
 async function progressAction(run,action){
  synchronize();const value=known.get(run.id)||(globalSummary?.preparation_id===run.id?globalSummary:null);
  if(!value)throw new Error("請回到原檢閱查看背景準備狀態。");
  if(action==="debug")return openLookupDiagnostics(value.preparation_id);
  if(action==="stop")return perform("pause",value);
  if(value.state==="partial"){
   if(currentReview!==value.review_task_id)await openReview(value.review_task_id);
   await open();return;
  }
  return perform("resume",value);
 }
 return {context,poll,reset,open,read,progressAction};
})();
