"use strict";

window.ReviewHistoryUI=(()=>{
 const titles={numeric:"歷年數值類報告",orders:"歷年醫囑清單",visits:"歷次就診紀錄",
  case_numeric:"該次數值類報告",case_orders:"該次醫囑清單",order_report:"醫囑報告內容",visit_soap:"歷史 SOAP"};
 let context=null,stack=[],taskId="",sequence=0,signature="",renderSignature="",polling=null,loadingStarted=0,orderQuery="",orderGroup="date",orderCategory="all",orderPage=0,visitPage=0,frame=null,orderView=null,reportView=null;
 const panel=()=>$("#dataContent"),modal=()=>$("#dataDialog");
 const scans=new Set(["scans","case_scans"]);
 const openScans=next=>window.ScanBrowser.open({...next,account,
  patient_name:reviewPatientIdentity(next.mrn,next.review_task_id).name,
  request:api,allowFetch:()=>!!work?.online&&!root.read_only});
 const same=(a,b)=>a&&b&&a.review_task_id===b.review_task_id&&a.cohort_id===b.cohort_id&&a.mrn===b.mrn&&a.resource===b.resource&&a.reference===b.reference;
 function clearView(){reportView?.gallery.dispose();frame=orderView=reportView=null;}
 function reset(){clearView();context=null;stack=[];taskId="";signature=renderSignature="";loadingStarted=0;sequence++;}
 function active(){return modal().open&&modal().dataset.view==="history"&&context;}
 function assetUrl(asset){return "/api/accounts/"+encodeURIComponent(account)+"/analysis/asset?id="+encodeURIComponent(asset.digest);}
 function action(label,callback){const button=btn(label,callback,"quiet");button.classList.add("history-action");return button;}
 function showError(error){
  const content=panel();content.querySelector(".clinical-loading")?.remove();
  const message=error.message||"報告讀取失敗，請稍後再試。",previous=content.querySelector(".history-load-error");
  if(previous)previous.textContent=message;else content.append(node("p",message,"error history-load-error"));
 }
 async function load(next,{push=false,force=false}={}){
  if(push&&context)stack.push(context);
  const retaining=same(context,next)&&frame;
  if(!same(context,next)){clearView();orderQuery="";orderGroup=next.resource==="orders"?"name":"date";orderCategory="all";orderPage=visitPage=0;}
  context=next;taskId="";signature=renderSignature="";loadingStarted=Date.now();const request=++sequence;
  extensionTask="";
  window.FileCompare?.context(account,next.mrn,reviewPatientIdentity(next.mrn,next.review_task_id).name);
  dialog("dataDialog");modal().dataset.view="history";$("#dataTitle").textContent=titles[next.resource];
  setPatientDialogContext("dataDialog",next.mrn,"",next.review_task_id);
  if(!retaining)panel().replaceChildren(ClinicalUI.loading("正在讀取已存報告…",{startedAt:loadingStarted}));
  try{
   const result=await api("/reviews/history/read",next);
   if(request!==sequence||!active())return;
   const fetching=(force||!result.complete)&&work?.online&&!root.read_only;
   render(result,{loading:fetching});
   if(fetching){
    const status=node("div");
    panel().prepend(status);
    JobProgress.pending(status,titles[next.resource],"正在送出歷史資料查詢…");
    let started;
    try{started=await api("/tasks/start",{kind:"history",...next,force});}
    finally{status.remove();}
    if(request!==sequence||!active())return;
    taskId=started.task_id;await poll();
   }
  }catch(error){
   if(request===sequence&&active())showError(error);
   throw error;
  }
 }
 function open(resource,mrn,reference=""){stack=[];const next={review_task_id:currentReview,mrn,resource,reference};return scans.has(resource)?openScans(next):load(next);}
 function openCurrent(resource,mrn,records){
  const available=(records||[]).filter(record=>record.id);
  if(!available.length){say("本次檢閱沒有可查詢的門診 SOAP。",true);return;}
  if(available.length===1)return open(resource,mrn,available[0].id);
  reset();dialog("dataDialog");modal().dataset.view="history";$("#dataTitle").textContent="選擇該次門診";
  setPatientDialogContext("dataDialog",mrn);
  const content=panel();content.replaceChildren(node("p","本次檢閱含多筆門診 SOAP，請選擇要查詢的就診。","caption"));
  const choices=node("div",undefined,"history-current-choices");
  for(const record of available){
   const label=[record.date,record.section||record.section_code,record.case_no].filter(Boolean).join(" · ");
   choices.append(action(label,()=>{if(scans.has(resource))modal().close();return open(resource,mrn,record.id);}));
  }
  content.append(choices);
 }
 function openTask(task){stack=[];const next={...(task.review_task_id?{review_task_id:task.review_task_id}:{cohort_id:task.cohort_id}),mrn:task.mrn,
  resource:task.resource==="scan_asset"?"scans":task.resource,reference:task.resource==="scan_asset"?"":task.reference||""};
  return scans.has(next.resource)?openScans(next):load(next);}
 function jump(resource,reference=""){const next={...context,resource,reference};return scans.has(resource)?openScans(next):load(next,{push:true});}
 function goBack(){const previous=stack.pop();if(previous)return load(previous);}
 function navigation(content,result){
  const controls=node("div",undefined,"history-controls");
  if(stack.length)controls.append(action("← 返回清單",goBack));
  controls.append(node("span",result.updated_at?"已存資料 · "+time(result.updated_at):"尚無已存資料","caption"));
  if(work?.online&&!root.read_only)controls.append(action("更新資料",()=>load(context,{force:true})));
  if(!result.complete&&work?.online&&!root.read_only)controls.append(action("重試未完成",()=>load(context)));
  content.append(controls);
 }
 function coverage(content,result){
  const value=result.coverage||{};
  if(context.resource==="numeric")content.append(node("p","歷年數值索引約自 "+value.numeric_start+" 起；更早門診可由歷次就診選取後補查。","caption"));
  if(context.resource==="orders")content.append(node("p","歷年醫囑索引約自 "+value.orders_start+" 起；更早門診可由歷次就診選取後補查。","caption"));
 }
 function render(result,{loading=false}={}){
  if(!active())return;
  const next=JSON.stringify([result.updated_at,result.data_revision,result.complete,result.issues,result.data,loading]);
  if(next===renderSignature)return;
  renderSignature=next;
  panel().querySelector(".history-load-error")?.remove();
  if(!frame){const content=panel();content.replaceChildren();frame={navigation:node("div"),coverage:node("div"),issues:node("p",undefined,"error history-index-issues"),body:node("div",undefined,"history-report-content"),waiting:node("div")};content.append(frame.navigation,frame.coverage,frame.issues,frame.body,frame.waiting);}
  frame.navigation.replaceChildren();navigation(frame.navigation,result);frame.coverage.replaceChildren();coverage(frame.coverage,result);
  frame.issues.textContent=result.issues?.length?`已顯示可確認的資料；另有 ${result.issues.length} 項未能確認，可按「重試未完成」重試。`:"";frame.issues.hidden=!result.issues?.length;
  const content=frame.body;
  const data=result.data;
  if(data===null){
   if(!reportView){content.replaceChildren();if(!loading)content.append(empty(result.complete?"沒有可顯示的資料。":root.read_only?"沒有已存資料；此資料庫為唯讀。":work?.online?"尚未取得資料，可按「重試未完成」重試。":"沒有已存資料；重新連線後可查詢。"));}
   waiting(loading);return;
  }
  if(context.resource==="numeric"||context.resource==="case_numeric"){
   content.replaceChildren();
   appendNumericTables(content,data.tables||[]);
   if(context.resource==="numeric")content.append(action("從歷次就診選擇更早報告",()=>jump("visits")));
  }else if(context.resource==="orders"||context.resource==="case_orders"){
   if(!orderView){content.replaceChildren();orderView=renderOrders(content);if(context.resource==="orders")content.append(action("從歷次就診選擇更早醫囑",()=>jump("visits")));}
   orderView.update(data);
  }else if(context.resource==="visits"){content.replaceChildren();renderVisits(content,data);}
  else if(context.resource==="visit_soap"){
   content.replaceChildren();
   content.append(node("p",[data.date,data.section,data.case_no].filter(Boolean).join(" · "),"caption"),SOAPView.create(data));
  }else renderOrderReport(content,data);
  waiting(loading);
 }
 function waiting(loading){
  if(!loading){frame.waiting.replaceChildren();return;}
  if(!frame.waiting.querySelector(".clinical-loading")){const value=ClinicalUI.loading("正在取得其他資料，已存結果可先檢閱…",{startedAt:loadingStarted});value.classList.add("is-compact");frame.waiting.append(value);}
 }
 function renderOrders(content){
  let rows=[];const controls=node("div",undefined,"history-filter"),categories=node("div",undefined,"history-order-classification"),search=node("input"),group=node("select"),results=node("div"),pager=node("div"),summary=node("p",undefined,"caption");
  search.type="search";search.placeholder="搜尋醫囑名稱或日期";search.setAttribute("aria-label","搜尋醫囑名稱或日期");search.value=orderQuery;
  group.setAttribute("aria-label","醫囑分類排序");group.append(new Option("依日期分類（新到舊）","date"),new Option("依名稱分類","name"));group.value=orderGroup;
  function draw(){
   categories.replaceChildren();
   for(const [key,label] of [["all","全部"],["ophthalmic","眼科檢查"],["other","其他"]]){const count=rows.filter(row=>key==="all"||!!row.ophthalmic===(key==="ophthalmic")).length,choose=action(`${label}（${count}）`,()=>{orderCategory=key;orderPage=0;draw();});choose.setAttribute("aria-pressed",String(orderCategory===key));categories.append(choose);}
   let visible=rows.filter(row=>(orderCategory==="all"||!!row.ophthalmic===(orderCategory==="ophthalmic"))&&(!orderQuery||[row.name,row.date,row.order_date,row.execution_date].filter(Boolean).join(" ").toLocaleLowerCase().includes(orderQuery.toLocaleLowerCase())));
   visible=[...visible].sort((a,b)=>Number(!!b.ophthalmic)-Number(!!a.ophthalmic)||(orderGroup==="name"?String(a.name||"").localeCompare(String(b.name||""),"zh-Hant")||String(b.date||"").localeCompare(String(a.date||"")):String(b.date||"").localeCompare(String(a.date||""))));
   orderPage=Math.max(0,Math.min(orderPage,Math.ceil(visible.length/40)-1));
   const pageRows=visible.slice(orderPage*40,orderPage*40+40),groups=new Map();
   for(const row of pageRows){const category=row.ophthalmic?"眼科檢查":"其他",key=orderGroup==="name"?(row.name||"未命名醫囑"):(String(row.date||"").slice(0,7)||"日期未提供");if(!groups.has(category))groups.set(category,new Map());const classified=groups.get(category);if(!classified.has(key))classified.set(key,[]);classified.get(key).push(row);}
   results.replaceChildren();
   for(const [category,classified] of groups){const categorySection=node("section",undefined,"history-order-category");categorySection.append(node("h3",category));
    for(const [name,items] of classified){const section=node("section",undefined,"history-group");section.append(node("h4",name));
     section.append(table(["使用／開單日期","醫囑名稱","就診案例","報告"],items.map(row=>{
     const date=[row.execution_date||row.order_date||"日期未提供",row.execution_date&&row.order_date&&row.execution_date!==row.order_date?"開單 "+row.order_date:""].filter(Boolean).join(" · ");
     return [date,row.name||"未命名醫囑",row.case_no||"—",action("查看內容",()=>jump("order_report",row.id))];
     })));categorySection.append(section);}
    results.append(categorySection);
   }
   if(!pageRows.length)results.append(empty("沒有符合的醫囑。"));
   paginate(pager,orderPage,visible.length,index=>{orderPage=index;draw();modal().scrollTop=0;panel().scrollTop=0;});
  }
  search.addEventListener("input",()=>{orderQuery=search.value;orderPage=0;draw();});
  group.addEventListener("change",()=>{orderGroup=group.value;orderPage=0;draw();});
  controls.append(search,group);content.append(controls,categories,summary,results,pager);
  return {update(next){rows=next;summary.textContent=rows.length+" 筆醫囑 · 點選後才讀取報告內容";draw();}};
 }
 function renderVisits(content,rows){
  const results=node("div"),pager=node("div");
  function draw(){
   results.replaceChildren(table(["日期","類型／科別","案例號","醫師","檢閱"],rows.slice(visitPage*40,visitPage*40+40).map(row=>{
    const actions=node("div",undefined,"history-visit-actions");
    if(row.soap_available){
     actions.append(action("SOAP",()=>jump("visit_soap",row.id)),action("數值",()=>jump("case_numeric",row.id)),action("醫囑",()=>jump("case_orders",row.id)),action("掃描",()=>jump("case_scans",row.id)));
    }else actions.append(node("span","此處無法開啟 SOAP","caption"));
    return [row.date||"日期未提供",[row.case_type==="O"?"門診":["A","I"].includes(row.case_type)?"住院":row.case_type==="E"?"急診":row.case_type||"未分類",row.section].filter(Boolean).join(" · "),row.case_no||"—",row.doctor||"—",actions];
   })));
   if(!rows.length)results.replaceChildren(empty("沒有可顯示的就診紀錄。"));
   paginate(pager,visitPage,rows.length,index=>{visitPage=index;draw();modal().scrollTop=0;panel().scrollTop=0;});
  }
  content.append(node("p",rows.length+" 筆歷次就診 · 門診可按需查看該次 SOAP、數值與醫囑","caption"),results,pager);draw();
 }
 function renderOrderReport(content,data){
  const order=data.order||{};
  if(!reportView){
   content.replaceChildren();
   const meta=node("p",undefined,"caption"),text=node("div"),issues=node("p",undefined,"error"),blank=empty("此醫囑沒有可顯示的報告內容。");
   const gallery=window.ReportGallery.create({urlFor:assetUrl,identity:{account,mrn:context.mrn}});
   reportView={meta,text,issues,blank,gallery,textSignature:""};content.append(meta,gallery.element,text,issues,blank);
  }
  reportView.meta.textContent=[order.date,order.name,order.case_no].filter(Boolean).join(" · ");
  reportView.gallery.update(data);
  const textSignature=JSON.stringify([order,data.texts,data.details]);
  if(textSignature!==reportView.textSignature){
   reportView.textSignature=textSignature;const text=reportView.text;text.replaceChildren();
   if(data.texts?.length||data.details?.length)text.append(ClinicalUI.textDownload(data,[order.date,order.name||"醫囑報告"].filter(Boolean).join(" ")));
   for(const report of data.texts||[]){text.append(node("h3","文字報告"));if(report.text)text.append(node("pre",report.text,"history-report-text"));if(Object.keys(report.fields||{}).length)text.append(table(["欄位","內容"],Object.entries(report.fields).map(([key,value])=>[key,String(value??"")])));}
   if(data.details?.length){text.append(node("h3","醫囑明細"));for(const detail of data.details)if(Object.keys(detail.fields||{}).length)text.append(table(["欄位","內容"],Object.entries(detail.fields).map(([key,value])=>[key,String(value??"")])));}
  }
  reportView.issues.textContent=(data.issues||[]).map(issue=>issue.branch+"："+issue.message).join("；");reportView.issues.hidden=!data.issues?.length;
  reportView.blank.hidden=!!(data.details?.length||data.texts?.length||data.assets?.length||data.attachment_items?.length);
 }
 function poll(){
  if(polling?.sequence===sequence)return polling.promise;
  const current=sequence,promise=pollOnce();polling={sequence:current,promise};
  return promise.finally(()=>{if(polling?.promise===promise)polling=null;});
 }
 async function pollOnce(){
  if(!taskId||!active())return;
  const id=taskId,request=sequence;
  try{
   const task=await api("/tasks/detail?summary=1&id="+encodeURIComponent(id));
   if(request!==sequence||id!==taskId||!active())return;
   JobProgress.publish(task);
   const current=JSON.stringify([task.status,task.revision??task.updated_at]);
   if(current===signature)return;
   const result=await api("/reviews/history/read",context);
   if(request!==sequence||id!==taskId||!active())return;
   signature=current;
   render(result,{loading:inProgress.has(task.status)});
   if(!inProgress.has(task.status)){
    taskId="";
    if(["failed","paused","partial"].includes(task.status))panel().append(node("p",task.message||"部分資料未取得，可稍後再試。","error"));
   }
  }catch(error){
   if(request===sequence&&id===taskId&&active()){renderSignature="";showError(error);}
   throw error;
  }
 }
 modal().addEventListener("close",reset);
 return {open,openCurrent,openTask,poll,reset,dismiss:reset};
})();
