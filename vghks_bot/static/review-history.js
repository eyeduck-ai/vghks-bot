"use strict";

window.ReviewHistoryUI=(()=>{
 const titles={numeric:"歷年數值類報告",orders:"歷年醫囑清單",visits:"歷次就診紀錄",
  case_numeric:"該次數值類報告",case_orders:"該次醫囑清單",order_report:"醫囑報告內容",visit_soap:"歷史 SOAP"};
 let context=null,stack=[],taskId="",sequence=0,signature="",orderQuery="",orderGroup="date",orderPage=0,visitPage=0;
 const panel=()=>$("#dataContent"),modal=()=>$("#dataDialog");
 const scans=new Set(["scans","case_scans"]);
 const openScans=next=>window.ScanBrowser.open({...next,account,
  patient_name:reviewPatientIdentity(next.mrn,next.review_task_id).name,
  request:api,allowFetch:()=>!!work?.online&&!root.read_only});
 const same=(a,b)=>a&&b&&a.review_task_id===b.review_task_id&&a.mrn===b.mrn&&a.resource===b.resource&&a.reference===b.reference;
 function reset(){context=null;stack=[];taskId="";signature="";sequence++;}
 function active(){return modal().open&&modal().dataset.view==="history"&&context;}
 function assetUrl(asset){return "/api/accounts/"+encodeURIComponent(account)+"/analysis/asset?id="+encodeURIComponent(asset.digest);}
 function action(label,callback){const button=btn(label,callback,"quiet");button.classList.add("history-action");return button;}
 async function load(next,{push=false,force=false}={}){
  if(push&&context)stack.push(context);
  if(!same(context,next)){orderQuery="";orderGroup=next.resource==="orders"?"name":"date";orderPage=visitPage=0;}
  context=next;taskId="";signature="";const request=++sequence;
  extensionTask="";
  window.FileCompare?.context(account,next.mrn,reviewPatientIdentity(next.mrn,next.review_task_id).name);
  dialog("dataDialog");modal().dataset.view="history";$("#dataTitle").textContent=titles[next.resource];
  setPatientDialogContext("dataDialog",next.mrn,"",next.review_task_id);
  panel().replaceChildren(node("p","正在讀取已存資料…","muted"));
  const result=await api("/reviews/history/read",next);
  if(request!==sequence||!active())return;
  render(result);
  if((force||!result.complete)&&work?.online&&!root.read_only){
   const status=node("div");
   panel().prepend(status);
   JobProgress.pending(status,titles[next.resource],"正在送出歷史資料查詢…");
   let started;
   try{started=await api("/tasks/start",{kind:"history",...next,force});}
   finally{status.remove();}
   if(request!==sequence||!active())return;
   taskId=started.task_id;await poll();
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
  content.append(controls);
 }
 function coverage(content,result){
  const value=result.coverage||{};
  if(context.resource==="numeric")content.append(node("p","歷年數值索引約自 "+value.numeric_start+" 起；更早門診可由歷次就診選取後補查。","caption"));
  if(context.resource==="orders")content.append(node("p","歷年醫囑索引約自 "+value.orders_start+" 起；更早門診可由歷次就診選取後補查。","caption"));
 }
 function render(result){
  if(!active())return;
  const content=panel();content.replaceChildren();navigation(content,result);coverage(content,result);
  const data=result.data;
  if(data===null){content.append(empty(work?.online?"尚無已存資料，正在準備取得。":"沒有已存資料；重新連線後可查詢。"));return;}
  if(context.resource==="numeric"||context.resource==="case_numeric"){
   appendNumericTables(content,data.tables||[]);
   if(context.resource==="numeric")content.append(action("從歷次就診選擇更早報告",()=>jump("visits")));
  }else if(context.resource==="orders"||context.resource==="case_orders"){
   renderOrders(content,data);
   if(context.resource==="orders")content.append(action("從歷次就診選擇更早醫囑",()=>jump("visits")));
  }else if(context.resource==="visits")renderVisits(content,data);
  else if(context.resource==="visit_soap"){
   content.append(node("p",[data.date,data.section,data.case_no].filter(Boolean).join(" · "),"caption"),SOAPView.create(data));
  }else renderOrderReport(content,data);
 }
 function renderOrders(content,rows){
  const controls=node("div",undefined,"history-filter"),search=node("input"),group=node("select"),results=node("div"),pager=node("div");
  search.type="search";search.placeholder="搜尋醫囑名稱";search.setAttribute("aria-label","搜尋醫囑名稱");search.value=orderQuery;
  group.setAttribute("aria-label","醫囑分類排序");group.append(new Option("依日期分類（新到舊）","date"),new Option("依名稱分類","name"));group.value=orderGroup;
  function draw(){
   let visible=rows.filter(row=>!orderQuery||row.name.toLocaleLowerCase().includes(orderQuery.toLocaleLowerCase()));
   if(orderGroup==="name")visible=[...visible].sort((a,b)=>a.name.localeCompare(b.name,"zh-Hant")||b.date.localeCompare(a.date));
   const pageRows=visible.slice(orderPage*40,orderPage*40+40),groups=new Map();
   for(const row of pageRows){const key=orderGroup==="name"?(row.name||"未命名醫囑"):(row.date.slice(0,7)||"日期未提供");if(!groups.has(key))groups.set(key,[]);groups.get(key).push(row);}
   results.replaceChildren();
   for(const [name,items] of groups){const section=node("section",undefined,"history-group");section.append(node("h3",name));
    section.append(table(["使用／開單日期","醫囑名稱","就診案例","報告"],items.map(row=>{
     const date=[row.execution_date||row.order_date||"日期未提供",row.execution_date&&row.order_date&&row.execution_date!==row.order_date?"開單 "+row.order_date:""].filter(Boolean).join(" · ");
     return [date,row.name||"未命名醫囑",row.case_no||"—",action("查看內容",()=>jump("order_report",row.id))];
    })));results.append(section);}
   if(!pageRows.length)results.append(empty("沒有符合的醫囑。"));
   paginate(pager,orderPage,visible.length,index=>{orderPage=index;draw();modal().scrollTop=0;panel().scrollTop=0;});
  }
  search.addEventListener("input",()=>{orderQuery=search.value;orderPage=0;draw();});
  group.addEventListener("change",()=>{orderGroup=group.value;orderPage=0;draw();});
  controls.append(search,group);content.append(controls,node("p",rows.length+" 筆醫囑 · 點選後才讀取報告內容","caption"),results,pager);draw();
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
  content.append(node("p",[order.date,order.name,order.case_no].filter(Boolean).join(" · "),"caption"));
  const openCompare=action("查看比較",()=>window.FileCompare.open());
  openCompare.classList.add("history-compare-open");
  const syncCompare=()=>{const count=window.FileCompare.count();openCompare.textContent=`查看比較（${count} / 6）`;openCompare.disabled=!count;};
  if(data.assets?.length){syncCompare();content.append(openCompare);}
  if(data.assets?.length)content.append(node("h3","報告附件"));
  for(const [index,asset] of (data.assets||[]).entries()){
   const wrapper=node("div",undefined,"history-asset"),link=node("a","在獨立檢視器開啟附件 "+(index+1));link.href=assetUrl(asset);link.target="_blank";link.rel="noopener";
   const file=node(asset.mime==="application/pdf"?"iframe":"img");
   file.src=asset.mime==="application/pdf"?ClinicalUI.pdfURL(link.href):link.href;
   file.title="醫囑報告 "+(index+1);file.loading="lazy";
   if(file.tagName==="IMG")file.alt=file.title;
   wrapper.append(file);
   const name=[order.date,order.name||"醫囑附件",index+1].filter(Boolean).join(" ");
   const controls=node("div",undefined,"attachment-toolbar");
   controls.append(ClinicalUI.comparisonChoice({account,mrn:context.mrn,digest:asset.digest,mime:asset.mime,
     name,source:"醫囑報告",date:order.date||""}),ClinicalUI.assetTools({url:link.href,asset,name,image:file}),link);
   controls.addEventListener("change",syncCompare);
   wrapper.append(controls);content.append(wrapper);
  }
  if(data.texts?.length||data.details?.length)content.append(ClinicalUI.textDownload(data,[order.date,order.name||"醫囑報告"].filter(Boolean).join(" ")));
  for(const report of data.texts||[]){content.append(node("h3","文字報告"));if(report.text)content.append(node("pre",report.text,"history-report-text"));if(Object.keys(report.fields||{}).length)content.append(table(["欄位","內容"],Object.entries(report.fields).map(([key,value])=>[key,String(value??"")])));}
  if(data.details?.length){content.append(node("h3","醫囑明細"));for(const detail of data.details)if(Object.keys(detail.fields||{}).length)content.append(table(["欄位","內容"],Object.entries(detail.fields).map(([key,value])=>[key,String(value??"")])));}
  if(data.issues?.length)content.append(node("p",data.issues.map(issue=>issue.branch+"："+issue.message).join("；"),"error"));
  if(!data.details?.length&&!data.texts?.length&&!data.assets?.length)content.append(empty("此醫囑沒有可顯示的報告內容。"));
 }
 async function poll(){
  if(!taskId||!active())return;
  const id=taskId,task=await api("/tasks/detail?id="+encodeURIComponent(id));
  if(id!==taskId||!active())return;
  const current=task.status+"/"+task.updated_at;
  if(current===signature)return;signature=current;
  const result=await api("/reviews/history/read",context);
  if(id!==taskId||!active())return;
  render(result);JobProgress.publish(task);
  if(!inProgress.has(task.status)){
   taskId="";
   if(["failed","paused","partial"].includes(task.status))panel().append(node("p",task.message||"部分資料未取得，可稍後再試。","error"));
  }
 }
 modal().addEventListener("close",reset);
 window.addEventListener("filecomparechange",()=>{const button=active()&&panel().querySelector(".history-compare-open");if(button){const count=window.FileCompare.count();button.textContent=`查看比較（${count} / 6）`;button.disabled=!count;}});
 return {open,openCurrent,openTask,poll,reset,dismiss:reset};
})();
