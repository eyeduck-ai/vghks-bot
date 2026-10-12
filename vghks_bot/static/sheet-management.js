"use strict";
// The account's Google connection is independent of its hospital connection.
// Remote values, including patient names, are rendered only as text.
window.SheetManagement=(()=>{
 const q=id=>document.getElementById(id), make=(tag,text,cls)=>{const value=document.createElement(tag);if(text!==undefined)value.textContent=String(text);if(cls)value.className=cls;return value;};
 const actions={create_month:"建立月份",create_day:"新增刀日",repair:"檢查／修復結構",archive:"封存過去月份"};
 const statuses={preview:"待套用預覽",draft:"已保存操作草稿",blocked:"需要處理問題",pending:"等待核對",uncertain:"結果尚未確認",failed:"更新未完成",rejected:"Google 已拒絕，請修正後重新預覽",applied:"已完成",applied_changed:"完成後內容有變動",stale:"需要重新預覽",copied:"已複製並核對封存",unchanged:"結構已符合，無需更新"};
 let state=null,owner="",generation=0,busy=false,visible=false,action="create_month",activeId="",freshId="",finalizeId="",editRevision=0,initialized=false;
 let plans=new Map(),verified=new Set(),archiveMonths=new Set();
 const readOnly=()=>!!root?.read_only;
 const live=()=>state?.configured===true&&state.online===true&&state.stale===false;
 const displayTime=value=>value?String(value).slice(0,19).replace("T"," "):"尚無快取";
 const stamp=()=>account+"/"+(root?.context||"");
 function assertContext(revision,key){if(revision!==generation||key!==stamp())throw new DOMException("工作區已切換","AbortError");}
 async function invoke(path,values={}){const revision=generation,key=stamp();const value=await api(path,values);assertContext(revision,key);return value;}
 const call=(path,values={})=>invoke("/sheet-management/"+path,values);
 function message(text,error=false){q("sheetManagementMessage").textContent=text;q("sheetManagementMessage").className=error?"sheet-management-message error":"sheet-management-message";}
 function invalidate(){freshId="";verified.clear();}
 function offline(text){invalidate();if(state)state={...state,online:false,stale:true};message(text,true);renderConnection();renderPlans();}
 async function run(task){
  if(busy)return;
  const revision=generation;busy=true;controls();q("sheetManagementPage").setAttribute("aria-busy","true");
  try{return await task();}catch(error){if(error.name!=="AbortError"&&revision===generation)message(error.message||"操作未完成，輸入已保留。",true);}
  finally{if(revision===generation){busy=false;q("sheetManagementPage").removeAttribute("aria-busy");controls();}}
 }
 function button(label,perform,cls=""){const value=make("button",label,cls);value.type="button";value.addEventListener("click",()=>run(perform));return value;}
 function renderConnection(){
  const badge=q("sheetManagementConnection"),cached=!!state?.stale||!state?.online;
  badge.textContent=!state?.configured?"尚未設定 Google 連線":cached?(state?.cached_at?"離線／快取資料":"離線／尚無快取"):"Google 刀表已連線";
  badge.className="pill"+(cached?" offline":"");
  q("sheetManagementCache").textContent="最近快取："+displayTime(state?.cached_at);
  q("sheetManagementOffline").hidden=!cached;
  q("sheetManagementOffline").textContent=state?.cached_at?"目前顯示快取資料，不能套用變更。輸入與已保存操作會保留；連線恢復後，請重新預覽，或核對尚未確認的操作。":"尚未取得刀表，沒有可用快取，不能套用變更。輸入與已保存操作會保留；請重新連線後預覽或核對。";
  q("sheetManagementReadonly").hidden=!readOnly();
  q("sheetManagementSource").textContent=state?.spreadsheet_id?"刀表代碼："+state.spreadsheet_id:"請先保存服務帳戶與 Google 刀表連線設定。";
  q("sheetManagementPreviewButton").textContent=live()?"產生操作預覽":"保存操作草稿／嘗試預覽";
  controls();
 }
 function controls(){
  for(const id of ["sheetManagementReconnect","sheetManagementRefresh","sheetManagementPreviewButton","sheetManagementReportButton","sheetManagementGoogleSave","sheetManagementGoogleTest","sheetManagementGoogleRemove","sheetManagementFinalizeButton"]){
   const value=q(id);value.disabled=busy||readOnly();
  }
  q("sheetManagementSettings").disabled=busy;
  q("sheetManagementPreviewButton").disabled=busy||readOnly();
  q("sheetManagementReportButton").disabled=busy||readOnly()||!state?.configured;
  for(const value of q("sheetManagementPlans").querySelectorAll("button[data-plan-action]")){
   const plan=plans.get(value.dataset.planId),kind=value.dataset.planAction;
   value.disabled=busy||readOnly()||(kind==="apply"?!canApply(plan):kind==="finalize"?!canFinalize(plan):kind==="check"?!state?.configured:false);
  }
 }
 function issues(values){
  const list=make("ul",undefined,"sheet-management-issues");
  for(const item of values||[]){
   const text=typeof item==="string"?item:[item.sheet||item.ref,item.row!==undefined?"第 "+(Number(item.row)+1)+" 列":"",item.reason||item.message||item.code].filter(Boolean).join(" · ");
   list.append(make("li",text||"請核對刀表結構。"));
  }
  return list;
 }
 function renderMonths(){
  const area=q("sheetManagementMonths");area.replaceChildren();
  const months=state?.months||[];
  q("sheetManagementMonthsCount").textContent=months.length+" 個月份";
  for(const month of months){
   const detail=make("details",undefined,"sheet-management-month"),summary=make("summary");
   const count=month.patient_count===null||month.patient_count===undefined?"排程數待核對":month.patient_count+" 筆排程 · "+(month.days||[]).length+" 個刀日";
   summary.append(make("strong",month.month),make("span",count,"caption"));
   detail.append(summary);
   const dates=make("ul");for(const day of month.days||[])dates.append(make("li",[day.date,day.hospital,day.row!==undefined?"第 "+day.row+" 列":""].filter(Boolean).join(" · ")));
   detail.append(dates);area.append(detail);
  }
  if(!months.length)area.append(make("p",state?.cached_at?"最近讀取的刀表沒有月份分頁，可預覽建立月份。":"目前尚未取得刀表月份，沒有可用快取；請重新連線讀取。","caption"));
  const list=q("sheetManagementArchiveMonths");list.replaceChildren();
  const current=(root?.today||new Date().toISOString().slice(0,10)).slice(0,7).replace("-","");
  const eligible=months.filter(item=>String(item.month).replace("-","")<current);
  for(const month of eligible){
   const value=String(month.month).replace("-",""),label=make("label",undefined,"check"),input=make("input");input.type="checkbox";input.name="months";input.value=value;input.checked=archiveMonths.has(value);
   input.addEventListener("change",()=>{if(input.checked)archiveMonths.add(value);else archiveMonths.delete(value);edited();});
   label.append(input,make("span",month.month+" · "+(month.patient_count===null||month.patient_count===undefined?"排程數待核對":month.patient_count+" 筆")));list.append(label);
  }
  if(!eligible.length)list.append(make("p",state?.cached_at?"目前沒有可封存的過去月份。":"尚未取得月份資料；請連線後選擇可封存月份。","caption"));
  const structure=q("sheetManagementIssues");structure.replaceChildren();structure.append((state?.issues||[]).length?issues(state.issues):make("p",state?.cached_at?"目前保存的檢查結果沒有結構問題。":"連線後可檢查刀表結構。","caption"));
 }
 function remember(plan){if(!plan?.id)return;plans.set(plan.id,plan);activeId=plan.id;}
 function acceptStatus(value){
  state=value;for(const plan of value.plans||[])if(plan?.id)plans.set(plan.id,plan);
  renderConnection();renderMonths();renderPlans();
 }
 function planDescription(plan){const params=plan.params||{};return [params.month,params.date,params.hospital,...(params.months||[])].filter(Boolean).join(" · ");}
 function canApply(plan){return !!plan&&live()&&plan.status==="preview"&&plan.can_apply===true&&!plan.stale&&freshId===plan.id;}
 function canFinalize(plan){return !!plan&&live()&&plan.action==="archive"&&plan.status==="copied"&&plan.can_finalize===true&&verified.has(plan.id);}
 function renderPlans(){
  const area=q("sheetManagementPlans");area.replaceChildren();
  const values=[...plans.values()].sort((left,right)=>String(right.created_at||"").localeCompare(String(left.created_at||"")));
  for(const plan of values){
   const detail=make("details",undefined,"sheet-management-plan");detail.open=plan.id===activeId||["pending","uncertain","copied"].includes(plan.status);
   const summary=make("summary"),title=actions[plan.action]||"刀表操作";
   summary.append(make("strong",title),make("span",statuses[plan.status]||plan.status||"已保存","badge"),make("span",displayTime(plan.created_at),"caption"));detail.append(summary);
   detail.append(make("p",planDescription(plan),"caption"));if(plan.message)detail.append(make("p",plan.message));
   if(plan.status==="preview"&&!canApply(plan))detail.append(make("p","套用前請重新預覽，確認目前連線與刀表內容。","caption"));
   if(plan.status==="copied")detail.append(make("p","封存內容已核對，本機備份已保存。確認後才會移除原月份分頁；重新進入或連線後，請先核對封存結果。原月份與封存副本並存期間，同步暫停；請完成移除原月份後恢復。","sheet-management-archive-note"));
   if((plan.changes||[]).length){const list=make("ul");for(const change of plan.changes)list.append(make("li",typeof change==="string"?change:change.message||change.summary||change.type||"刀表結構更新"));detail.append(list);}
   if((plan.issues||[]).length)detail.append(issues(plan.issues));
   const row=make("div",undefined,"actions");
   const add=(label,kind,perform,cls="")=>{const value=button(label,perform,cls);value.dataset.planAction=kind;value.dataset.planId=plan.id;row.append(value);};
   if(["preview","draft","blocked","stale","rejected"].includes(plan.status))add("重新預覽","preview",()=>preview(plan.action,plan.params||{}));
   if(plan.status==="preview")add(plan.action==="archive"?"複製到封存表並核對":"套用這次預覽","apply",()=>apply(plan),"primary");
   if(["pending","uncertain","failed","copied","applied_changed","applied"].includes(plan.status))add(plan.status==="copied"?"核對封存結果":"核對更新結果","check",()=>checkPlan(plan));
   if(plan.status==="copied"&&plan.can_finalize)add("確認封存後移除原月份","finalize",()=>beginFinalize(plan),"danger");
   detail.append(row);area.append(detail);
  }
  if(!values.length)area.append(make("p","操作預覽會保存於目前工作區，重新開啟後可繼續核對。","caption"));
  const history=q("sheetManagementHistory");history.replaceChildren();
  for(const item of state?.history||[])history.append(make("p",[displayTime(item.at||item.created_at),actions[item.action]||item.action,statuses[item.status]||item.status,item.message].filter(Boolean).join(" · "),"caption"));
  controls();
 }
 function setAction(value){action=actions[value]?value:"create_month";for(const input of q("sheetManagementOperationForm").querySelectorAll('input[name="sheet_action"]'))input.checked=input.value===action;for(const panel of q("sheetManagementOperationForm").querySelectorAll("[data-sheet-fields]")){panel.hidden=panel.dataset.sheetFields!==action;panel.disabled=panel.hidden;}}
 function edited(){editRevision++;freshId="";renderPlans();}
 function parameters(){
  if(action==="create_month")return {month:q("sheetManagementMonth").value};
  if(action==="create_day")return {date:q("sheetManagementDay").value,hospital:q("sheetManagementHospital").value};
  if(action==="archive"){if(!archiveMonths.size)throw new Error("請勾選至少一個過去月份。");return {months:[...archiveMonths].sort()};}
  return {};
 }
 function restoreIntent(kind,params){
  setAction(kind);if(params.month)q("sheetManagementMonth").value=String(params.month).replace(/^(\d{4})(\d{2})$/,"$1-$2");
  if(params.date)q("sheetManagementDay").value=params.date;if(params.hospital)q("sheetManagementHospital").value=params.hospital;
  if(params.months){archiveMonths=new Set(params.months.map(value=>String(value).replace("-","")));renderMonths();}
 }
 async function preview(kind=action,params=parameters()){
  if(readOnly())throw new Error("目前資料庫為唯讀。");
  restoreIntent(kind,params);const revision=editRevision;invalidate();message("正在讀取刀表並保存操作預覽…");
  try{
   const plan=await call("preview",{action:kind,params});remember(plan);
   if(plan.stale===false&&!["draft","pending","uncertain"].includes(plan.status))state={...state,online:true,stale:false,cached_at:plan.cached_at||plan.created_at||state?.cached_at};
   else if(plan.stale)state={...state,online:false,stale:true};
   if(plan.status==="preview"&&plan.can_apply&&!plan.stale&&revision===editRevision)freshId=plan.id;
   renderConnection();renderPlans();message(plan.message||(["draft","stale"].includes(plan.status)?"操作意圖已保存；連線恢復後請重新預覽。":"操作預覽已保存，請核對後套用。"));
  }catch(error){if(error.name!=="AbortError")offline("無法取得最新刀表，輸入已保留；重新連線後請重新預覽。"+error.message);throw error;}
 }
 async function refresh(){
  if(readOnly())throw new Error("目前資料庫為唯讀，可檢閱已保存狀態。");
  invalidate();message("正在連接 Google 刀表…");
  try{const value=await call("refresh");acceptStatus(value);message(value.message||(live()?"已取得最新刀表。操作須重新預覽後套用。":"目前使用快取；輸入與已保存操作皆保留。"),!live());}
  catch(error){if(error.name!=="AbortError")offline("連線中斷；目前顯示快取，不能套用。"+error.message);throw error;}
 }
 async function apply(plan,finalize=false){
  if(readOnly())throw new Error("目前資料庫為唯讀。");
  if(finalize?!canFinalize(plan):!canApply(plan))throw new Error("請先重新連線並預覽，或核對已保存的封存結果。");
  freshId="";verified.delete(plan.id);message(finalize?"正在核對封存並移除原月份…":"正在套用並回讀核對…");renderPlans();
  try{const result=await call("apply",{id:plan.id});remember(result);if(result.stale){invalidate();state={...state,online:false,stale:true};}else if(result.status==="copied"&&result.can_finalize)verified.add(result.id);renderConnection();renderPlans();message(result.message||statuses[result.status]||"操作結果已保存。");
   if(result.status==="applied"&&plan.action==="archive"){for(const month of plan.params?.months||[])archiveMonths.delete(month);renderMonths();}
  }catch(error){if(error.name!=="AbortError"){remember({...plan,status:"uncertain",can_apply:false,can_finalize:false,message:"套用結果尚未確認；請重新連線後核對，勿重複送出。"});offline("更新結果尚未確認；已保存操作意圖，請重新連線後核對。"+error.message);}throw error;}
 }
 async function checkPlan(plan){
  if(!state?.configured||readOnly())throw new Error("請先設定 Google 連線，再核對更新結果。");
  message("正在核對已保存操作；不會重新送出原操作…");
  try{const value=await call("check",{id:plan.id});remember(value);if(value.stale===false){state={...state,online:true,stale:false,cached_at:value.cached_at||value.updated_at||state.cached_at};if(value.status==="copied"&&value.can_finalize)verified.add(value.id);}else if(value.stale){invalidate();state={...state,online:false,stale:true};}renderConnection();renderPlans();message(value.message||statuses[value.status]||"已完成核對。");}
  catch(error){if(error.name!=="AbortError")offline("核對未完成；操作仍保留。"+error.message);throw error;}
 }
 function beginFinalize(plan){
  if(!canFinalize(plan))throw new Error("請先重新連線並核對封存結果。");
  finalizeId=plan.id;q("sheetManagementFinalizeText").textContent="本機備份已保存，封存內容已回讀核對。這次操作才會移除原月份分頁："+(plan.params?.months||[]).join("、")+"。";
  q("sheetManagementFinalizeConfirm").checked=false;q("sheetManagementFinalizeDialog").showModal();
 }
 function reportTable(headers,rows){
  const wrap=make("div",undefined,"table-wrap"),table=make("table",undefined,"patient-table"),thead=make("thead"),head=make("tr"),body=make("tbody");
  for(const title of headers){const th=make("th",title);th.scope="col";head.append(th);}thead.append(head);
  for(const row of rows){const tr=make("tr");for(const value of row)tr.append(make("td",value??"—"));body.append(tr);}table.append(thead,body);wrap.append(table);return wrap;
 }
 async function report(){
  if(readOnly())throw new Error("目前資料庫為唯讀，可檢閱已保存狀態。");
  if(!q("sheetManagementReportForm").reportValidity())return;
  const start=q("sheetManagementReportStart").value,end=q("sheetManagementReportEnd").value;
  if(start>end)throw new Error("結束日期不可早於開始日期。");
  message("正在讀取 IOL 名單…");
  let value;
  try{value=await call("report",{start,end,hospital:q("sheetManagementReportHospital").value});}
  catch(error){if(error.name!=="AbortError"){offline("查詢未完成；請重新連線後再查詢，輸入已保留。");q("sheetManagementReportMeta").textContent=q("sheetManagementReport").children.length?"以下為先前查詢資料，尚未取得最新刀表。":"目前無法取得刀表，請重新連線後查詢。";}throw error;}
  if(value.stale)offline(value.cached_at?"IOL 名單來自快取；重新連線後請再次查詢。輸入已保留。":"目前無法取得刀表，尚無可用快取；重新連線後查詢。輸入已保留。");
  else{state={...state,online:true,stale:false,cached_at:value.cached_at||state?.cached_at};renderConnection();}
  const area=q("sheetManagementReport");area.replaceChildren();
  if(value.stale&&!value.cached_at){q("sheetManagementReportMeta").textContent="目前無法取得刀表，尚無可用快取；重新連線後查詢。";area.append(make("p","尚未取得 IOL 名單與型號統計，請重新連線後查詢。","caption"));return;}
  q("sheetManagementReportMeta").textContent=(value.stale?"快取資料":"即時資料")+" · "+(value.total??value.items?.length??0)+" 筆排程 · 最近快取 "+displayTime(value.cached_at);
  area.append(make("h3","IOL 型號與數量"),reportTable(["型號","數量"],(value.models||[]).map(item=>[item.model||"未填",item.count??0])),make("h3","病人名單"));
  const items=value.items||[];
  area.append(items.length?reportTable(["刀日","院區","病歷號","姓名","側別","術式","IOL","Target","Final","位置"],items.map(item=>[item.date,item.hospital,item.mrn,item.name,item.side,item.procedure,item.iol,item.iol_target,item.iol_final,typeof item.position==="string"?item.position:item.position?.ref||"—"])):make("p","此範圍沒有排程。","caption"));
  if(value.count_basis)area.append(make("p",value.count_basis,"caption"));if(value.excluded_cancelled)area.append(make("p","已排除 "+value.excluded_cancelled+" 筆劃除排程。","caption"));if(value.issues?.length)area.append(issues(value.issues));if(!value.stale)message("IOL 名單已更新。");
 }
 function credentialStatus(value){return value.configured?"已保存服務帳戶："+(value.email||"已設定"):"尚未匯入服務帳戶金鑰。";}
 async function settings(){const value=await invoke("/analysis/google");q("sheetManagementGoogleSheet").value=value.spreadsheet_id||"";q("sheetManagementGoogleKey").value="";q("sheetManagementGoogleStatus").textContent=credentialStatus(value);q("sheetManagementGoogleResult").textContent="";q("sheetManagementGoogleDialog").showModal();}
 async function saveSettings(test=false){
  if(readOnly()||!q("sheetManagementGoogleForm").reportValidity())return;
  const revision=generation,key=stamp();
  const values={spreadsheet_id:q("sheetManagementGoogleSheet").value},file=q("sheetManagementGoogleKey").files?.[0];
  if(file){if(file.size>1024*1024)throw new Error("金鑰檔案超過 1 MB，請選擇服務帳戶 JSON 金鑰。");try{values.key=JSON.parse(await file.text());}catch{throw new Error("無法讀取 JSON 金鑰，請重新選擇檔案。");}assertContext(revision,key);}
  q("sheetManagementGoogleResult").textContent="正在保存設定…";
  try{const value=await invoke("/analysis/google/save",values);q("sheetManagementGoogleKey").value="";q("sheetManagementGoogleStatus").textContent=credentialStatus(value);invalidate();if(value.spreadsheet_id!==state?.spreadsheet_id){plans.clear();archiveMonths.clear();activeId="";q("sheetManagementReport").replaceChildren();q("sheetManagementReportMeta").textContent="";state={};}state={...state,configured:value.configured,spreadsheet_id:value.spreadsheet_id,online:false,stale:true};renderConnection();renderMonths();renderPlans();
   q("sheetManagementGoogleResult").textContent="設定已保存。";
   if(test){const result=await invoke("/analysis/google/test");q("sheetManagementGoogleResult").textContent=[result.title,result.message].filter(Boolean).join(" · ");await refresh();}
  }catch(error){if(error.name!=="AbortError")q("sheetManagementGoogleResult").textContent=error.message;throw error;}
 }
 async function open(){
  visible=true;if(owner!==stamp()){reset();owner=stamp();visible=true;}
  if(!initialized){initialized=true;const today=root?.today||new Date().toISOString().slice(0,10);q("sheetManagementDay").value=q("sheetManagementReportStart").value=q("sheetManagementReportEnd").value=today;
   const parts=today.slice(0,7).split("-").map(Number),next=new Date(parts[0],parts[1],1);q("sheetManagementMonth").value=next.getFullYear()+"-"+String(next.getMonth()+1).padStart(2,"0");}
  return run(async()=>{invalidate();const value=await call("status");acceptStatus(value);if(value.configured&&!readOnly()&&navigator.onLine!==false)await refresh();});
 }
 function reset(){
  generation++;owner="";busy=false;visible=false;state=null;plans=new Map();verified=new Set();archiveMonths=new Set();freshId=activeId=finalizeId="";initialized=false;editRevision=0;
  q("sheetManagementPage").removeAttribute("aria-busy");q("sheetManagementReport").replaceChildren();q("sheetManagementReportMeta").textContent="";
  for(const id of ["sheetManagementMonth","sheetManagementDay","sheetManagementReportStart","sheetManagementReportEnd","sheetManagementReportHospital","sheetManagementGoogleKey","sheetManagementGoogleSheet"])q(id).value="";
  for(const id of ["sheetManagementGoogleStatus","sheetManagementGoogleResult","sheetManagementFinalizeText"])q(id).textContent="";
  q("sheetManagementHospital").value="高榮";q("sheetManagementFinalizeConfirm").checked=false;
  for(const id of ["sheetManagementGoogleDialog","sheetManagementFinalizeDialog"])if(q(id).open)q(id).close();
  setAction("create_month");message("");renderConnection();renderMonths();renderPlans();
 }
 for(const input of q("sheetManagementOperationForm").querySelectorAll('input[name="sheet_action"]'))input.addEventListener("change",()=>{setAction(input.value);edited();});
 for(const id of ["sheetManagementMonth","sheetManagementDay","sheetManagementHospital"])q(id).addEventListener("input",edited);
 q("sheetManagementOperationForm").addEventListener("submit",event=>{event.preventDefault();if(q("sheetManagementOperationForm").reportValidity())void run(()=>preview());});
 q("sheetManagementReportForm").addEventListener("submit",event=>{event.preventDefault();void run(report);});
 for(const id of ["sheetManagementReconnect","sheetManagementRefresh"])q(id).addEventListener("click",()=>run(refresh));
 q("sheetManagementSettings").addEventListener("click",()=>run(settings));
 q("sheetManagementGoogleForm").addEventListener("submit",event=>{event.preventDefault();void run(()=>saveSettings());});
 q("sheetManagementGoogleTest").addEventListener("click",()=>run(()=>saveSettings(true)));
 q("sheetManagementGoogleRemove").addEventListener("click",()=>run(async()=>{if(readOnly())return;const revision=generation,key=stamp();if(await confirmDelete("移除此工作區保存的 Google 服務帳戶金鑰？")){assertContext(revision,key);const value=await invoke("/analysis/google/remove");q("sheetManagementGoogleStatus").textContent=credentialStatus(value);state={...state,configured:false,online:false,stale:true};invalidate();renderConnection();renderPlans();}}));
 q("sheetManagementFinalizeForm").addEventListener("submit",event=>{event.preventDefault();if(q("sheetManagementFinalizeForm").reportValidity())void run(async()=>{const plan=plans.get(finalizeId);q("sheetManagementFinalizeDialog").close();await apply(plan,true);});});
 for(const value of document.querySelectorAll("[data-sheet-close]"))value.addEventListener("click",()=>q(value.dataset.sheetClose).close());
 window.addEventListener("offline",()=>{if(state)offline("網路已中斷；顯示快取資料，不能套用。輸入與操作意圖已保留。");});
 window.addEventListener("online",()=>{if(visible&&state?.stale)message("網路可能已恢復。請按「重新連線」，再重新預覽或核對已保存操作。");});
 setAction(action);renderConnection();
 return {open,reset,visibility(name){visible=name==="sheetManagement";},readonly(){renderConnection();}};
})();
