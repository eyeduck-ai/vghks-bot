"use strict";
const $ = selector => document.querySelector(selector);
const $$ = (selector, parent=document) => [...parent.querySelectorAll(selector)];
const node = (tag, text, cls) => {const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
const iconPaths={search:["M21 21l-5-5","M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14"],folder:["M3 7V4h6l2 3h10v13H3Z","M3 10h18"],patients:["M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2","M9 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8","M17 4a4 4 0 0 1 0 7M22 21v-2a4 4 0 0 0-3-4"],settings:["M4 7h16M4 17h16","M9 4v6M15 14v6"],close:["M6 6l12 12M6 18 18 6"],plus:["M12 5v14M5 12h14"],refresh:["M20 8a8 8 0 1 0 0 8","M20 3v5h-5"],left:["M15 5l-7 7 7 7"],right:["M9 5l7 7-7 7"],calendar:["M4 5h16v16H4ZM4 10h16M8 3v4M16 3v4"],tag:["M3 3h8l10 10-8 8L3 11ZM7 7h.01"],chart:["M4 3v17h17M8 14l4-5 4 3 5-7"],diagnostic:["M14 2H4v20h16V8ZM14 2v6h6M8 12h8M8 16h5"],trash:["M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"]};
function setIcon(button,name,label,only=true){
 const svg=document.createElementNS("http://www.w3.org/2000/svg","svg");svg.setAttribute("viewBox","0 0 24 24");svg.setAttribute("fill","none");svg.setAttribute("stroke","currentColor");svg.setAttribute("stroke-width","1.7");svg.setAttribute("stroke-linecap","round");svg.setAttribute("stroke-linejoin","round");svg.setAttribute("aria-hidden","true");svg.classList.add("icon");
 for(const value of iconPaths[name]||iconPaths.settings){const path=document.createElementNS(svg.namespaceURI,"path");path.setAttribute("d",value);svg.append(path);}
 button.replaceChildren(svg,node("span",label,only?"sr-only":""));button.title=label;button.setAttribute("aria-label",label);button.classList.toggle("icon-button",only);button.classList.toggle("icon-text",!only);return button;
}
const actionIcons={"上一位":"left","下一位":"right","掛號紀錄":"calendar","加入／移除 tag":"tag","查看診斷":"diagnostic","本次數值報告":"chart","重試":"refresh","移除":"trash"};
const btn = (text, callback, cls) => {const n=node("button",text,cls);n.type="button";if(actionIcons[text])setIcon(n,actionIcons[text],text);n.addEventListener("click",()=>act(callback));if(root?.read_only&&/^(續跑|暫停|改名|移除|使用工具|加入|更新|刪除|保存)/.test(text))n.disabled=true;return n;};
const labels={queued:"排隊中",running:"執行中",completed:"完成",partial:"部分完成",paused:"待續跑",cancelled:"已暫停",interrupted:"待續跑",failed:"失敗",cancelling:"暫停中",ready:"已取得",pending:"待處理",no_visit:"查無就診",unknown:"需確認",missing:"SOAP 尚無內容",error:"未取得",future:"尚未到診",deleted:"已刪除",no_source:"未指定來源"};
const inProgress=new Set(["queued","running","cancelling"]);
labels.forbidden="權限不足";
let root, account="", epoch=0, work, listing, selected=new Set(), manual=[], listTab="own", listPage=0, page="patients";
let activeListRun="", resolveTask="", resolved=[], selectedSet=null, currentReview="", reviewData=null, reviewPatient="", reviewSignature="";
let libraryData, libraryPage=0, librarySelected=new Set(), tagPage=0, extensionTask="", extensionContext=null;
let libraryRecordSequence=0, libraryReturnFocus=null;
let toastTimer, draftTimer, pollBusy=false, searchSequence=0, stopped=false;
let collapsedDays=new Set(), resolveSignature="", uncheckedResolved=new Set();
let suppressCompletedReview=false;
let reviewSelected=new Set(), reviewSelectionTask="";

function say(message,error=false){const n=$(error?"#notice":"#toast");n.textContent=message;n.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>n.hidden=true,error?9000:3500);}
function applyReadonly(){window.DatabaseUI?.readonly();}
async function act(callback){$("#notice").hidden=true;const revision=epoch,control=document.activeElement?.tagName==="BUTTON"?document.activeElement:null;if(control?.dataset.busy)return;const wasDisabled=control?.disabled;if(control){control.dataset.busy="true";control.disabled=true;}try{return await callback();}catch(e){if(e.name!=="AbortError")say(e.message||"操作未完成",true);}finally{if(control){delete control.dataset.busy;if(control.isConnected&&revision===epoch)control.disabled=!!wasDisabled;}}}
async function request(path, body, scoped=true){
  const key=account, revision=epoch;
  if(scoped&&!key)throw new Error("請先選擇帳號。");
  const url=scoped?"/api/accounts/"+key+path:"/api"+path;
  const response=await fetch(url,{method:body===undefined?"GET":"POST",credentials:"same-origin",cache:"no-store",headers:body===undefined?{}:{"Content-Type":"application/json","X-CSRF-Token":root?.csrf||"","X-Database-Context":root?.context||""},body:body===undefined?undefined:JSON.stringify(body)});
  const data=await response.json();
  if(scoped&&(key!==account||revision!==epoch))throw new DOMException("帳號已切換","AbortError");
  if(!response.ok)throw new Error(data.error||"操作未完成。");
  return data;
}
const api=(path,body)=>request(path,body,true), ra=(path,body)=>request(path,body,false);
const time=value=>(value||"").slice(0,19).replace("T"," ");
const info=()=>root.accounts.find(a=>a.id===account);
function fillSelect(select,items,placeholder="全部",selectedValue=select.value){select.replaceChildren(new Option(placeholder,""),...items.map(i=>new Option(i.name||i.label||i.id,i.id)));if(items.some(i=>i.id===selectedValue))select.value=selectedValue;}
function badges(tags,manualTag=false){const wrap=node("span");for(const tag of tags||[])wrap.append(node("span",(manualTag?"手動 · ":"")+(tag.name||tag.category_name),"badge"+(manualTag?" manual":"")));return wrap;}
function categoryOptions(){const cats=work?.categories||[];for(const id of ["reviewTag","libraryTag","tagFilter"])fillSelect($("#"+id),cats,"全部 tag");}
function empty(text){return node("p",text,"empty");}
function table(headers,rows){const wrap=node("div",undefined,"table-wrap"),table=node("table",undefined,"patient-table"),head=node("thead"),tr=node("tr"),body=node("tbody");for(const h of headers){const th=node("th",h);th.scope="col";tr.append(th);}head.append(tr);for(const cells of rows){const r=node("tr");for(const value of cells){const c=node("td");c.append(value instanceof Node?value:node("span",value??"—"));r.append(c);}body.append(r);}table.append(head,body);wrap.append(table);return wrap;}
function patientName(p){const n=node("span");n.append(node("strong",p.name||"姓名未提供"),node("span",p.mrn,"mrn"));return n;}
function check(label,checked,change){const n=node("input");n.type="checkbox";n.checked=checked;n.setAttribute("aria-label",label);n.addEventListener("change",()=>change(n.checked));return n;}
function paginate(container,index,total,change){container.replaceChildren();const max=Math.max(1,Math.ceil(total/40));const before=btn("上一頁",()=>change(index-1)),after=btn("下一頁",()=>change(index+1));before.disabled=index===0;after.disabled=index+1>=max;container.append(before,node("span",`第 ${index+1} / ${max} 頁`),after);}
function dialog(id){Choices.sync();if(!$("#"+id).open)$("#"+id).showModal();}
function confirmDelete(text){$("#confirmText").textContent=text;const d=$("#confirmDialog");d.returnValue="";d.showModal();return new Promise(resolve=>d.addEventListener("close",()=>resolve(d.returnValue==="delete"),{once:true}));}

function renderAccounts(){
 const keep=$("#loginAccount").value;
 fillSelect($("#loginAccount"),root.accounts.map(a=>({id:a.id,name:(a.label||a.username)+" · "+a.campus})),"新增帳號",keep);
 $("#accountSwitch").replaceChildren(...root.accounts.map(a=>new Option((a.label||a.username)+" · "+a.campus,a.id)),new Option("＋ 新增帳號","__new"));
 $("#accountSwitch").value=account;
}
function loginFields(key=""){
 const a=root.accounts.find(a=>a.id===key);$("#loginAccount").value=key;$("#loginUsername").value=a?.username||"";$("#loginUsername").readOnly=!!a;
 $("#loginLabel").value=a?.label||"";$("#loginCampus").value=a?.campus||"高榮";$("#loginPassword").value="";$("#loginPassword").placeholder=a?.remembered?"已保存；留白使用保存的密碼":"請輸入院內密碼";
 $("#rememberLogin").checked=a?!!a.remembered:true;$("#offlineButton").disabled=!a;$("#forgetPassword").disabled=!a?.remembered;$("#loginError").hidden=true;
}
function showEntry(key=""){
 if(account&&work&&!root.read_only){clearTimeout(draftTimer);api("/draft/save",draftValue()).catch(()=>{});}
 clearTimeout(draftTimer);window.SurgerySystem?.reset();epoch++;account="";$("#moduleFrame").removeAttribute("src");for(const d of $$("dialog[open]"))d.close();$("#shell").hidden=true;$("#entry").hidden=false;loginFields(key);
}
async function login(key, automatic=false){
 const revision=epoch;
 const control=$("#loginSubmit");control.disabled=true;control.textContent="正在登入…";$("#loginError").hidden=true;
 try {
  const body=automatic?{id:key}:{id:key||undefined,username:$("#loginUsername").value,password:$("#loginPassword").value,label:$("#loginLabel").value,campus:$("#loginCampus").value,remember:$("#rememberLogin").checked};
  const value=await ra("/accounts/login",body);if(revision!==epoch)return;$("#loginPassword").value="";root=await ra("/bootstrap");if(revision!==epoch)return;renderAccounts();await enter(value.account.id);
 }catch(e){$("#loginError").textContent=e.message;$("#loginError").hidden=false;}
 finally{control.disabled=false;control.textContent="登入";}
}
async function enter(key){
 collapsedDays.clear();uncheckedResolved.clear();resolveSignature="";
 epoch++;account=key;window.Approvals?.reset();window.ToolWorkspace?.reset();window.Earnings?.reset();window.SurgerySystem?.reset();work=null;listing=null;selected=new Set();manual=[];listPage=libraryPage=tagPage=0;listTab="own";selectedSet=null;currentReview="";reviewData=null;reviewSignature="";resolveTask=extensionTask=activeListRun="";librarySelected.clear();
 $("#moduleFrame").removeAttribute("src");for(const d of $$("dialog[open]"))d.close();
 for(const id of ["reviewQuery","listQuery","libraryQuery","libraryStart","libraryEnd","tagQuery"])$("#"+id).value="";
 for(const id of ["libraryTag","tagFilter","reviewTag","libraryTask"])$("#"+id).value="";
 $("#entry").hidden=true;$("#shell").hidden=false;$("#accountSwitch").value=key;$("#startDate").value=$("#endDate").value=root.today;$("#dateMode").value="single";dateMode();
 for(const id of ["taskList","libraryRecords","tagPatients","reviewPatients","reviewDetail","dataContent","librarySoapContent","savedSets"])$("#"+id)?.replaceChildren();
 $("#listJob").textContent="";$("#connectionStatus").textContent="載入中";showPage("patients");renderList();renderSets();
 await refresh();const draft=work.draft;
 if(draft){$("#startDate").value=draft.start||root.today;$("#endDate").value=draft.end||root.today;$("#dateMode").value=draft.mode||"single";dateMode();selected=new Set(draft.selected||[]);manual=draft.manual||[];listTab=draft.tab||"own";}
 await readList();showPage("patients");renderSets();applyReadonly();
}
function showPage(name){page=name;Choices.sync();window.ToolWorkspace?.visibility(name);window.SurgerySystem?.visibility(name);for(const section of $$("main>.page"))section.hidden=section.id!==name+"Page";for(const b of $$("[data-page]"))b.classList.toggle("active",b.dataset.page===name);}
async function navigate(name){showPage(name);if(name==="library")await loadLibrary();if(name==="tags")await loadTags();if(name==="tasks")await refresh();if(name==="approvals"){await window.Approvals.open();}if(name==="earnings")await window.Earnings.open();if(name==="surgery")await window.SurgerySystem.open();}
const draftValue=()=>({start:$("#startDate").value,end:$("#endDate").value,mode:$("#dateMode").value,selected:[...selected],manual,tab:listTab});
function draftSave(){if(root.read_only)return;clearTimeout(draftTimer);const key=account;draftTimer=setTimeout(()=>{if(key!==account)return;act(()=>api("/draft/save",draftValue()));},250);}
function dateMode(){Choices.sync();const single=$("#dateMode").value==="single";$("#endDateField").hidden=single;if(single)$("#endDate").value=$("#startDate").value;$("#endDate").min=$("#startDate").value;}
function moveDay(day){const d=new Date(day+"T12:00:00Z");return d.toISOString().slice(0,10);}
function relativeDay(base,offset){const d=new Date(base+"T12:00:00Z");d.setUTCDate(d.getUTCDate()+offset);return moveDay(d.toISOString().slice(0,10));}
const ranges=()=>({start:$("#startDate").value,end:$("#dateMode").value==="single"?$("#startDate").value:$("#endDate").value});
function own(r){return r.ownership==="DEDICATED";}
const allRows=()=>listing?.days.flatMap(d=>d.rows)||[];
function visibleRows(){const q=$("#listQuery").value.trim().toLocaleLowerCase();return allRows().filter(r=>(listTab==="own"?own(r):!own(r))&&(!q||(r.mrn+" "+r.name).toLocaleLowerCase().includes(q)));}
function selectedMRNs(){return [...new Set([...allRows().filter(r=>selected.has(r.id)).map(r=>r.mrn),...manual.map(p=>p.mrn)])];}
async function browse(force=false){
 dateMode();if(!$("#dateForm").reportValidity())return;
 selected.clear();manual=[];listPage=0;activeListRun="";await readList();
 collapsedDays.clear();
 if(work.online){const value=await api("/lists/browse",{account_ids:[account],ranges:{[account]:ranges()},force});activeListRun=value.run_ids[0];$("#listJob").textContent="正在取得門診清單…";}else say("離線顯示已保存的門診清單。");
 for(const r of allRows())if(own(r)&&r.mrn)selected.add(r.id);renderList();draftSave();
}
async function readList(){listing=await api("/lists",{account_id:account,...ranges()});renderList();}
function renderList(){
 const rows=visibleRows(),mine=allRows().filter(own).length;$("#ownTab").textContent=`我的門診（${mine}）`;$("#sharedTab").textContent=`其他／共用門診（${allRows().length-mine}）`;
 $("#ownTab").setAttribute("aria-selected",listTab==="own");$("#sharedTab").setAttribute("aria-selected",listTab!=="own");
 const chosen=rows.filter(r=>selected.has(r.id)).length;$("#selectAll").checked=!!rows.length&&chosen===rows.length;$("#selectAll").indeterminate=chosen>0&&chosen<rows.length;$("#selectAll").disabled=!rows.length;
 $("#listSummary").textContent=`${rows.length} 筆掛號 · 已選 ${chosen} 筆`;
 const content=$("#registrationGroups");content.replaceChildren();listPage=Math.min(listPage,Math.max(0,Math.ceil(rows.length/40)-1));const groups=new Map();
 for(const row of rows.slice(listPage*40,listPage*40+40)){if(!groups.has(row.day))groups.set(row.day,[]);groups.get(row.day).push(row);}
 for(const [day,items] of groups){
  const detail=node("details",undefined,"registration-day"),groupKey=listTab+day;detail.open=!collapsedDays.has(groupKey);detail.addEventListener("toggle",()=>{if(detail.isConnected){if(detail.open)collapsedDays.delete(groupKey);else collapsedDays.add(groupKey);}});const summary=node("summary",day);const allDay=rows.filter(r=>r.day===day);summary.append(node("span",`${allDay.length} 筆 · 已選 ${allDay.filter(r=>selected.has(r.id)).length}`,"day-count"));detail.append(summary);
  const selectLine=node("div",undefined,"date-selection"),label=node("label",undefined,"check");label.append(check("選取 "+day+" 全日病人",allDay.every(r=>selected.has(r.id)),v=>{for(const r of allDay){if(v)selected.add(r.id);else selected.delete(r.id);}renderList();draftSave();}),node("span","選取此日期（跨頁）"));selectLine.append(label);detail.append(selectLine);
  detail.append(table(["選取","病人／病歷號","性別／年齡","科別／診間","狀態"],items.map(r=>[
   check("選取 "+r.mrn+" "+r.day,selected.has(r.id),v=>{if(v)selected.add(r.id);else selected.delete(r.id);renderList();draftSave();}),patientName(r),(r.sex||"—")+" · "+(r.age||"—"),(r.section_code||"未註明")+" / "+(r.room||"—"),
   own(r)?(r.day>root.today?"未來掛號":r.status):r.doctor_card?"其他醫師 · "+r.doctor_card:r.doctor_label_present?"共用門診":"歸屬未確認"
  ])));content.append(detail);
 }
 if(!rows.length)content.append(empty(listing?.days.some(d=>d.cached)?"此範圍沒有符合的掛號。":"選擇日期查看門診清單，或手動加入病人。"));
 paginate($("#listPagination"),listPage,rows.length,n=>{listPage=n;renderList();});
 $("#manualMembersSection").hidden=!manual.length;$("#manualCount").textContent=manual.length+" 位";
 $("#manualMembers").replaceChildren(table(["病人／病歷號","性別／生日","操作"],manual.map(p=>[patientName(p),(p.sex||"—")+" · "+(p.birthday||p.age||"—"),btn("移除",()=>{manual=manual.filter(m=>m.mrn!==p.mrn);renderList();draftSave();},"quiet")])));
 const n=selectedMRNs().length;$("#selectionCount").textContent=`已選 ${n} 位病人`;
 for(const id of ["saveCollection","useTools","selectedTags"])$("#"+id).disabled=!n;for(const button of $$("[data-use-tool]"))button.disabled=!n;
}
async function saveCurrentSet(){return api("/sets/save",{registrations:allRows().filter(r=>selected.has(r.id)).map(r=>({day:r.day,id:r.id})),mrns:manual.map(p=>p.mrn).join("\n"),source_range:ranges()});}
function renderSets(){const container=$("#savedSets");container.replaceChildren();for(const group of work?.sets||[]){const r=node("div",undefined,"saved-row"),text=node("div");text.append(node("strong",group.name),node("span",`${group.members.length} 位 · ${time(group.updated_at)}`,"caption"));const actions=node("div",undefined,"actions");actions.append(btn("使用工具",()=>openTools(group)),btn("改名",()=>renameSet(group),"quiet"),btn("移除",async()=>{if(await confirmDelete("移除此病人集合？病歷資料會保留。")){await api("/sets/delete",{id:group.id});await refresh();}},"quiet"));r.append(text,actions);container.append(r);}if(!work?.sets.length)container.append(empty("尚無保存的病人集合。"));}
function renameSet(group){$("#dataTitle").textContent="集合名稱";const form=node("form"),input=node("input"),save=node("button","保存","primary");input.value=group.name;input.required=true;input.maxLength=100;input.setAttribute("aria-label","集合名稱");save.type="submit";form.append(input,save);form.addEventListener("submit",event=>{event.preventDefault();act(async()=>{await api("/sets/save",{id:group.id,name:input.value});$("#dataDialog").close();await refresh();});});$("#dataContent").replaceChildren(form);dialog("dataDialog");}
function toolDepartments(){const pref=work.preferences,previous=$("#department").value;$("#department").replaceChildren(...pref.departments.map(d=>new Option(d.name,d.id)));$("#department").value=pref.departments.some(d=>d.id===previous)?previous:pref.default_department;Choices.sync();window.ToolWorkspace?.refreshSettings();}
async function openTools(group){return window.ToolWorkspace.open("review",group);}
function toolOptions(){
 const registration=$("#soapMode").value==="registration",all=$("#departmentFilter").value==="all";
 $("#departmentFilterField").hidden=registration;$("#departmentField").hidden=registration||all;$("#cutoffField").hidden=$("#cutoffMode").value!=="date";
 $("#cutoffDate").disabled=$("#toolReviewOptions").hidden||$("#cutoffField").hidden;$("#cutoffDate").required=!$("#cutoffDate").disabled;
 $("#soapModeHelp").textContent=registration?"依選取的掛號日期與科別取得 SOAP；未來掛號尚未到診。":all?"取得最新門診 SOAP，不限科別，包含其他醫師的紀錄。":"取得最新同科門診 SOAP，包含其他醫師的紀錄。";
 $("#reviewRefreshHelp").textContent={cache:"優先使用已存 SOAP，缺漏時補抓。",refresh:"更新就診索引並補抓新紀錄，重用已存 SOAP。",force:"重新下載 SOAP；舊版本保留。"}[$("#reviewRefresh").value];Choices.sync();
}
async function openModule(cohort,module="retina"){window.ToolWorkspace?.activate(module);if(await window.ToolWorkspace?.adoptModule(cohort)===false)return;$("#moduleTitle").textContent={retina:"視網膜比較",cataract:"白內障術前比較",surgery:"刀表更新"}[module]||"檢查比較";$("#moduleFrame").src="/tools?account="+encodeURIComponent(account)+"&cohort="+encodeURIComponent(cohort)+"&module="+encodeURIComponent(module);showPage("module");}
function renderTasks(history){
 const container=$("#taskList");container.replaceChildren();const tasks=[...(work.tasks||[]).filter(t=>!t.kind.startsWith("approval_")&&!t.kind.startsWith("earnings_")).map(t=>({...t,bot:true})),...(history?.runs||[]).filter(r=>r.kind!=="bot")].sort((a,b)=>(b.created_at||"").localeCompare(a.created_at||""));
 $("#taskCount").textContent=tasks.filter(t=>inProgress.has(t.status)).length||"";
 for(const task of tasks){const row=node("div",undefined,"task-row"),desc=node("div"),actions=node("div",undefined,"actions");desc.append(node("h3",task.name||(task.kind==="analysis"?"歷年檢查比較":"門診清單")),node("p",`${labels[task.status]||task.status} · ${time(task.created_at)} · ${task.message||""}`));
  if(task.bot&&task.kind==="review")actions.append(btn("檢閱",()=>openReview(task.id)));
  else if(task.bot&&task.kind==="surgery_schedule")actions.append(btn("查看排程",async()=>{showPage("surgery");await window.SurgerySystem.open(task.id);}));
  else if(task.bot&&task.kind==="resolve")actions.append(btn("核對病人",async()=>{resolveTask=task.id;resolveSignature="";dialog("manualDialog");await refreshResolve();}));
  else if(task.bot)actions.append(btn("查看",()=>showExtensionTask(task.id,task.kind)));
  else if(task.kind==="analysis")actions.append(btn("檢閱結果",()=>openModule(task.cohort_id,task.modules?.[0]||"retina")));
  if(inProgress.has(task.status))actions.append(btn("暫停",async()=>{await api(task.bot?"/tasks/stop":"/stop",{id:task.id});await refresh();},"quiet"));
  else if(["paused","partial","failed","cancelled","interrupted"].includes(task.status)&&task.kind!=="list")actions.append(btn("續跑",async()=>{if(task.bot)await api("/tasks/start",{resume:task.id});else await api("/analysis/start",{resume:task.id});await refresh();},"primary"));
  if(["failed","partial","paused","interrupted"].includes(task.status))actions.append(btn("查看診斷",()=>showDiagnostics({task_id:task.id})));
  desc.append(JobProgress.create(task));row.append(desc,actions);container.append(row);
 }if(!tasks.length)container.append(empty("選擇病人集合後，從「使用進階工具」開始。"));
}
async function refresh(){
 if(!account)return;const [value,history]=await Promise.all([api("/workbench"),api("/history")]);work=value;window.Approvals?.badge(value.approval_counts,value.approval_monitor_enabled);categoryOptions();renderSets();renderTasks(history);$("#connectionStatus").textContent=work.online?"已登入":"離線檢閱";$("#connectionStatus").classList.toggle("offline",!work.online);$("#dataDirectory").textContent=info()?.campus+" · "+(info()?.label||info()?.username);
 if(activeListRun){const run=history.runs.find(r=>r.id===activeListRun);if(run){JobProgress.show("#listJob",run);if(!inProgress.has(run.status)){activeListRun="";await readList();for(const r of allRows())if(own(r)&&r.mrn)selected.add(r.id);renderList();draftSave();}}}
 if(resolveTask&&$("#manualDialog").open)await refreshResolve();
 await window.ToolWorkspace?.poll();
 if(extensionTask&&$("#dataDialog").open)await showExtensionTask(extensionTask,extensionContext?.kind,false);
 if(currentReview&&page==="review"){const task=work.tasks.find(t=>t.id===currentReview),signature=task?.updated_at+"/"+task?.status;if(signature!==reviewSignature){reviewSignature=signature;await loadReview();}}
}
async function refreshResolve(){const task=await api("/tasks/detail?id="+encodeURIComponent(resolveTask));resolved=task.items.filter(p=>p.status==="resolved");$("#resolveStatus").textContent=`${labels[task.status]||task.status} · 已取得 ${resolved.length} / ${task.total} 位`;
 $("#resolveStatus").append(JobProgress.create(task));const signature=JSON.stringify([task.id,task.status,task.items]);if(signature===resolveSignature)return;resolveSignature=signature;
 $("#resolvedPatients").replaceChildren(table(["核對","病人／輸入","基本資料"],task.items.map(p=>p.status==="resolved"?[check("核對 "+p.mrn,!uncheckedResolved.has(task.id+p.mrn),value=>{if(value)uncheckedResolved.delete(task.id+p.mrn);else uncheckedResolved.add(task.id+p.mrn);}),patientName(p),(p.sex||"—")+" · "+(p.birthday||p.age||"—")]:["—",p.input,resolutionFailure(p,task)])));
 $("#confirmResolved").disabled=!resolved.length;$("#resolveRetry").hidden=!task.items.some(p=>p.status==="error")||inProgress.has(task.status);
 if(!inProgress.has(task.status))resolveTask=task.id;
}
function resolutionFailure(p,task){const n=node("div");n.append(node("span",p.message||"查詢失敗"));const retry=btn("重試此筆",async()=>{await api("/tasks/start",{resume:task.id,retry_only:[p.input]});await refreshResolve();});retry.disabled=inProgress.has(task.status);n.append(retry);return n;}
async function openReview(id){suppressCompletedReview=work.tasks.some(t=>t.id===id&&t.status==="completed");window.ToolWorkspace?.activate("review");currentReview=id;reviewSignature="";reviewPatient="";$("#reviewQuery").value="";$("#reviewTag").value="";showPage("review");await loadReview();}
const reviewFilters=()=>({q:$("#reviewQuery").value,search_mode:$("#reviewSearchMode").value,tag:$("#reviewTag").value});
async function loadReview(){const sequence=++searchSequence;const value=await api("/reviews/results",{id:currentReview,...reviewFilters()});if(sequence!==searchSequence||page!=="review"||value.task.id!==currentReview)return;reviewData=value;window.ToolWorkspace?.adoptReview(value.task);
 $("#reviewTitle").textContent=value.task.name;$("#reviewContext").textContent=`${labels[value.task.status]||value.task.status} · 建立於 ${time(value.task.created_at)}`;
 if(suppressCompletedReview&&value.task.status==="completed")$("#reviewProgress").replaceChildren();else JobProgress.show("#reviewProgress",value.task);$("#reviewSummary").textContent=`${value.total} / ${value.all_total} 位`;
 if(reviewSelectionTask!==currentReview){reviewSelectionTask=currentReview;reviewSelected=new Set(value.task.members.map(p=>p.mrn));}
 const patients=$("#reviewPatients");patients.replaceChildren();if(!value.patients.some(p=>p.mrn===reviewPatient))reviewPatient=value.patients[0]?.mrn||"";
 const allLabel=node("label",undefined,"check review-check");allLabel.append(check("選取全部篩選病人",value.patients.length>0&&value.patients.every(p=>reviewSelected.has(p.mrn)),v=>{for(const p of value.patients){if(v)reviewSelected.add(p.mrn);else reviewSelected.delete(p.mrn);}act(loadReview);}),node("span","選取全部篩選病人"));patients.append(allLabel);
 for(const p of value.patients){const b=btn("",()=>{reviewPatient=p.mrn;renderReviewDetail();for(const n of $$(".patient-select"))n.classList.toggle("active",n.dataset.mrn===p.mrn);},"patient-select"+(p.mrn===reviewPatient?" active":""));b.dataset.mrn=p.mrn;b.append(node("strong",p.name||p.mrn),node("span",`${p.mrn} · ${labels[p.status]||p.status}`,"caption"));if(p.records?.length)b.append(node("span",p.records.map(r=>r.date).filter((v,i,a)=>a.indexOf(v)===i).join("、"),"caption"));const tags=new Map((p.records||[]).flatMap(r=>r.matches||[]).map(t=>[t.category,t]));b.append(badges([...tags.values()]),badges(p.manual_tags,true));const row=node("div",undefined,"review-patient-row");row.append(check("送往工具 "+p.mrn,reviewSelected.has(p.mrn),v=>{if(v)reviewSelected.add(p.mrn);else reviewSelected.delete(p.mrn);updateReviewSelection();}),b);patients.append(row);}
 if(!value.patients.length)patients.append(empty("沒有符合的病人。"));updateReviewSelection();renderReviewDetail();
}
function updateReviewSelection(){if(!reviewData)return;const count=reviewData.patients.filter(p=>reviewSelected.has(p.mrn)).length,all=$('#reviewPatients input[aria-label="選取全部篩選病人"]');if(all){all.checked=count>0&&count===reviewData.total;all.indeterminate=count>0&&count<reviewData.total;}$("#reviewSummary").textContent=`${reviewData.total} / ${reviewData.all_total} 位 · 已選 ${count}`+(reviewData.scope_issue_count?` · ${reviewData.scope_issue_count} 份病歷的 tag 判定不完整`:"");$("#reviewToTools").disabled=!count;}
function soapNode(record){return SOAPView.create(record);}
function renderReviewDetail(){const panel=$("#reviewDetail");panel.replaceChildren();const p=reviewData?.patients.find(p=>p.mrn===reviewPatient);if(!p){panel.append(empty("選擇左側病人檢閱 SOAP。"));return;}
 const heading=node("div",undefined,"detail-meta"),name=node("div"),nav=node("div",undefined,"actions");name.append(node("h2",p.name||"姓名未提供"),node("p",[p.mrn,p.sex,p.age].filter(Boolean).join(" · "),"muted"));const index=reviewData.patients.indexOf(p);for(const [label,d] of [["上一位",-1],["下一位",1]]){const b=btn(label,()=>{reviewPatient=reviewData.patients[index+d].mrn;renderReviewDetail();for(const n of $$(".patient-select"))n.classList.toggle("active",n.dataset.mrn===reviewPatient);});b.disabled=!reviewData.patients[index+d];nav.append(b);}heading.append(name,nav);panel.append(heading);
 const actions=node("div",undefined,"detail-actions");actions.append(btn("掛號紀錄",()=>extension("registrations",p.mrn)),btn("加入／移除 tag",()=>openPatientTags([p.mrn])));panel.append(actions,badges(p.manual_tags,true));
 if(["error","partial","forbidden"].includes(p.status)&&!inProgress.has(reviewData.task.status)){actions.append(btn("重試此病人",async()=>{await api("/tasks/start",{resume:currentReview,retry_only:[p.mrn]});await loadReview();}));}actions.append(btn("查看診斷",()=>showDiagnostics({mrn:p.mrn,task_id:currentReview})));
 if(p.message)panel.append(node("p",p.message,"attempts"));if(p.index_checked_at)panel.append(node("p","索引核對 "+time(p.index_checked_at),"caption"));
 if(p.attempts?.some(a=>a.status!=="ready")){const d=node("details",undefined,"attempts"),list=node("ul");d.append(node("summary","較新紀錄／未取得項目"));for(const a of p.attempts.filter(a=>a.status!=="ready"))list.append(node("li",`${a.date} · ${a.case_no} · ${a.status==="empty"?"SOAP 空白":labels[a.status]||a.status} ${a.message||""}`));d.append(list);panel.append(d);}
 for(const record of p.records||[]){const section=node("section",undefined,"soap-record"),head=node("div",undefined,"section-heading"),soap=soapNode(record),jumps=node("div",undefined,"actions tag-jumps");head.append(node("h3",record.date+" · "+record.section),btn("本次數值報告",()=>extension("numeric",p.mrn,record.id)));for(const [index,mark] of [...soap.querySelectorAll("mark")].entries())jumps.append(btn((record.highlights?.length?"搜尋命中 ":"tag ")+(index+1),()=>SOAPView.reveal(mark),"quiet"));section.append(head,node("p",[record.doctor,record.case_no,record.cached?"使用本機版本":"已保存",time(record.updated_at)].filter(Boolean).join(" · "),"caption"),badges(record.matches),jumps,soap);panel.append(section);}
 if(!p.records?.length)panel.append(empty(labels[p.status]||"尚未取得 SOAP"));
}
async function showDiagnostics(values){
 extensionTask="";const result=await api("/reviews/diagnostics",values);$("#dataTitle").textContent="抓取診斷";
 const content=$("#dataContent"),text=JSON.stringify(result,null,2);content.replaceChildren(node("p","已自動保存在目前資料庫，搬移或備份時一併保留。","caption"));
 content.append(btn("匯出診斷 JSON",()=>{const url=URL.createObjectURL(new Blob([text],{type:"application/json;charset=utf-8"})),a=node("a");a.href=url;a.download="VGHKS-diagnostics-"+(values.mrn||values.task_id)+".json";document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}));
 if(!result.items.length)content.append(empty(result.saved_attempts.length?"此任務僅有舊版錯誤摘要；新版重試後會補上詳細診斷。":"尚無失敗診斷。舊版未保存的詳細資料需重新抓取才能取得。"));
 content.append(node("pre",text));dialog("dataDialog");
}
async function extension(kind,mrn,recordId="",force=false){extensionContext={kind,mrn,record_id:recordId};$("#dataTitle").textContent=kind==="numeric"?"本次數值報告":"掛號紀錄";$("#dataContent").replaceChildren(node("p","正在讀取…","muted"));dialog("dataDialog");const {data}=await api("/extensions/read",extensionContext);if(data)renderExtension(data,kind);if(!work.online){if(!data)$("#dataContent").replaceChildren(empty("沒有已保存資料，請登入後再查詢。"));return;}const task=await api("/tasks/start",{...extensionContext,force});extensionTask=task.task_id;await showExtensionTask(extensionTask,kind,false);}
async function showExtensionTask(id,kind,open=true){const task=await api("/tasks/detail?id="+encodeURIComponent(id));extensionTask=id;extensionContext={kind:kind||task.kind,mrn:task.mrn,record_id:task.record_id||""};if(open){$("#dataTitle").textContent=task.name;dialog("dataDialog");}if(task.items.length)renderExtension(task.items[0],kind||task.kind);else $("#dataContent").replaceChildren(node("p",task.message||labels[task.status],"muted"));$("#dataContent").prepend(JobProgress.create(task));if(!inProgress.has(task.status)){extensionTask="";if(task.status!=="completed")$("#dataContent").append(btn("查看診斷",()=>showDiagnostics({task_id:id,mrn:task.mrn})));}}
function renderExtension(data,kind){const c=$("#dataContent");c.replaceChildren(node("p",`${data.cached?"本機資料 · ":""}最後取得 ${time(data.updated_at)}`,"caption"));c.append(btn("更新資料",()=>extension(kind,extensionContext.mrn,extensionContext.record_id,true)));if(data.status==="deleted"){c.append(empty("資料已刪除。"));return;}
 if(kind==="numeric"){for(const t of data.payload?.tables||[]){c.append(node("h3",t.title||t.name||"數值報告"),table(t.headers||[],t.rows||[]));}if(!data.payload?.tables?.length)c.append(empty("此就診沒有可顯示的數值表。"));}
 else{const day=data.queried_on||data.updated_at?.slice(0,10)||root.today;const rows=[...(data.payload||[])].sort((a,b)=>(b.visit_date||"").localeCompare(a.visit_date||""));c.append(node("p","查詢基準日 "+day,"caption"));for(const [name,subset] of [["今天及未來",rows.filter(r=>r.visit_date&&r.visit_date>=day)],["過去",rows.filter(r=>r.visit_date&&r.visit_date<day)],["日期不明",rows.filter(r=>!r.visit_date)]]){if(name==="日期不明"&&!subset.length)continue;c.append(node("h3",name),table(["日期","科別","診間／序號","掛號狀態"],subset.map(r=>[r.visit_date||"未註明",r.section_name||r.section_code,(r.room||"—")+" / "+(r.sequence_no||"—"),r.cancelled_at?(r.cancelled_at==="已取消"?"已取消":"已取消 · "+r.cancelled_at):r.status||"狀態未確認"])));if(!subset.length)c.append(node("p","沒有紀錄","caption"));}}

}
const libraryFilters=()=>({q:$("#libraryQuery").value,tag:$("#libraryTag").value,start:$("#libraryStart").value,end:$("#libraryEnd").value,task_id:$("#libraryTask").value});
async function loadLibrary(){const seq=++searchSequence;const result=await api("/library/search",{...libraryFilters(),offset:libraryPage*40,limit:40});if(seq!==searchSequence)return;libraryData=result;$("#librarySummary").textContent=`${result.patients} 位病人 · ${result.total} 筆就診`+(result.scope_issue_count?` · ${result.scope_issue_count} 份病歷的 tag 判定不完整`:"");
 fillSelect($("#libraryTask"),(work.tasks||[]).filter(t=>t.kind==="review").map(t=>({id:t.id,name:t.name+" · "+time(t.created_at)})),"所有任務");
 const c=$("#libraryRecords");c.replaceChildren(...result.records.map(libraryRow));
 if(!result.records.length)c.append(empty("沒有符合的已保存病歷。"));
 paginate($("#libraryPagination"),libraryPage,result.total,async n=>{libraryPage=n;await loadLibrary();});
}
function libraryRow(record){
 const row=node("article",undefined,"library-record"),select=node("label",undefined,"library-record-selection");
 select.append(check("選取病歷 "+record.mrn+" "+record.date,librarySelected.has(record.id),value=>{
  if(value)librarySelected.add(record.id);else librarySelected.delete(record.id);
  row.classList.toggle("is-selected",value);
 }));
 row.classList.toggle("is-selected",librarySelected.has(record.id));
 const open=node("button",undefined,"library-record-open"),heading=node("span",undefined,"library-record-heading");
 open.type="button";open.dataset.recordId=record.id;open.setAttribute("aria-haspopup","dialog");
 open.setAttribute("aria-controls","librarySoapDialog");
 open.setAttribute("aria-label",`檢閱 SOAP：${record.name||"姓名未提供"}，${record.mrn}，${record.date}，${record.section||record.section_code||"科別未提供"}`);
 heading.append(node("strong",record.name||"姓名未提供","library-record-name"),node("span",record.mrn,"library-record-mrn"),node("span",record.date,"library-record-date"),node("span",record.section||record.section_code||"科別未提供","library-record-department"));
 const tags=node("span",undefined,"library-record-tags");tags.append(badges(record.matches),badges(record.manual_tags,true));
 if(record.tag_scope_issues?.length)tags.append(node("span","tag 待判定","badge"));
 heading.append(tags);open.append(heading);
 const preview=node("span",undefined,"library-record-preview");
 for(const part of record.ap_preview||[]){
  const line=node("span",undefined,"library-ap-line");
  line.append(node("strong",part.label+" "),document.createTextNode(part.empty?"（空白）":part.text.replace(/\s+/g," ")));
  preview.append(line);
 }
 if(!preview.childElementCount)preview.append(node("span","未提供可辨識的 A／P 分段","library-ap-unknown"));
 open.append(preview);open.addEventListener("click",()=>openRecord(record.id,open));
 row.append(select,open);return row;
}
async function openRecord(id,opener){
 const revision=++libraryRecordSequence,d=$("#librarySoapDialog"),c=$("#librarySoapContent");
 libraryReturnFocus={control:opener,id,epoch};$("#librarySoapTitle").textContent="SOAP";
 c.replaceChildren(node("p","正在讀取…","muted"));dialog("librarySoapDialog");
 try{
  const record=await api("/library/record",{id});
  if(revision!==libraryRecordSequence||!d.open)return;
  $("#librarySoapTitle").textContent=(record.name||"姓名未提供")+" · "+record.mrn+" · "+record.date+" · "+(record.section||record.section_code||"科別未提供");
  const meta=node("div",undefined,"library-soap-meta");
  meta.append(badges(record.matches),badges(record.manual_tags,true),btn("加入／移除 tag",()=>openPatientTags([record.mrn])));
  c.replaceChildren(meta,soapNode(record));
  if(record.versions?.length>1){
   const versions=node("details");versions.append(node("summary",record.versions.length+" 個內容版本"));
   for(const version of record.versions)versions.append(node("h3",time(version.saved_at)),soapNode(version));
   c.append(versions);
  }
 }catch(error){if(error.name!=="AbortError"&&revision===libraryRecordSequence&&d.open)c.replaceChildren(node("p",error.message||"病歷讀取失敗。","error"));}
}
const librarySoapDialog=$("#librarySoapDialog");
librarySoapDialog.addEventListener("close",()=>{
 libraryRecordSequence++;$("#librarySoapContent").replaceChildren();
 const focus=libraryReturnFocus;libraryReturnFocus=null;
 if(focus?.epoch===epoch){
  const control=$$(".library-record-open").find(button=>button.dataset.recordId===focus.id)||focus.control;
  if(control?.isConnected)control.focus({preventScroll:true});
 }
});
if(!("closedBy" in HTMLDialogElement.prototype)){
 let startedOutside=false;
 const outside=event=>{const r=librarySoapDialog.getBoundingClientRect();return event.target===librarySoapDialog&&(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom);};
 librarySoapDialog.addEventListener("pointerdown",event=>{startedOutside=outside(event);});
 librarySoapDialog.addEventListener("click",event=>{if(startedOutside&&outside(event))librarySoapDialog.close();startedOutside=false;});
}
const tagFilters=()=>({q:$("#tagQuery").value,tag:$("#tagFilter").value,source:$("#tagSource").value});
async function loadTags(){const data=await api("/patient-tags/search",{...tagFilters(),offset:tagPage*40,limit:40});$("#tagSummary").textContent=data.total+" 位病人 · 相同病歷號合併";$("#tagPatients").replaceChildren(table(["病人／病歷號","SOAP 自動 tag","手動 tag","操作"],data.patients.map(p=>[patientName(p),badges(p.auto_tags),badges(p.manual_tags,true),btn("編輯 tag",()=>openPatientTags([p.mrn]))])));paginate($("#tagPagination"),tagPage,data.total,async n=>{tagPage=n;await loadTags();});}
function openPatientTags(mrns=[]){$("#tagMRNs").value=[...new Set(mrns)].join("\n");$("#newTagName").value="";$("#tagChoices").replaceChildren(...(work.categories||[]).map(t=>{const label=node("label",undefined,"check"),input=check("tag "+t.name,false,()=>{});input.value=t.id;label.append(input,node("span",t.name));return label;}));dialog("patientTagDialog");}
function tagRuleRow(tag={id:crypto.randomUUID().replaceAll("-",""),name:"",keywords:[],parser:"none",scope:"all"}){
 const row=node("div",undefined,"rule-row");row.dataset.id=tag.id;row.dataset.parser=tag.parser||"none";
 const name=node("label","tag 名稱"),input=node("input");input.name="tag-name";input.value=tag.name;input.required=true;input.maxLength=40;name.append(input);
 const terms=node("label","自動辨識關鍵字（選填）"),area=node("textarea");area.value=tag.keywords.join("\n");area.rows=2;terms.append(area);
 const scope=node("fieldset",undefined,"choice-field tag-scope"),options=node("div",undefined,"choice-options");scope.append(node("legend","辨識範圍"));
 for(const [value,label] of [["all","全文"],["s","S"],["o","O"],["ap","A+P"],["medications","藥囑"],["orders","醫囑"]]){
  const item=node("label"),radio=node("input");radio.type="radio";radio.name="tag-scope-"+tag.id;radio.value=value;radio.checked=value===(tag.scope||"all");item.append(radio,node("span",label));options.append(item);
 }
 scope.append(options);row.append(name,terms,btn("移除",()=>row.remove(),"quiet"),scope);return row;
}
function departmentRow(group={id:crypto.randomUUID().replaceAll("-",""),name:"",codes:[],names:[]}){const row=node("div",undefined,"department-rule");row.dataset.id=group.id;const title=node("label","科別群組名稱"),name=node("input");name.value=group.name;name.required=true;name.maxLength=60;name.addEventListener("input",syncDefaultDepartment);title.append(name);const two=node("div",undefined,"two");for(const [key,text] of [["codes","科別代碼（每行一個）"],["names","科別名称別名（每行一個）"]]){const label=node("label",text),area=node("textarea");area.value=group[key].join("\n");area.rows=4;area.dataset.field=key;label.append(area);two.append(label);}row.append(title,two,btn("移除此科別",()=>{row.remove();syncDefaultDepartment();},"quiet"));return row;}
function syncDefaultDepartment(){const current=$("#defaultDepartment").value;$("#defaultDepartment").replaceChildren(...$$(".department-rule").map(r=>new Option(r.querySelector("input").value||"未命名",r.dataset.id)));if([...$("#defaultDepartment").options].some(o=>o.value===current))$("#defaultDepartment").value=current;}
function openRules(){window.ToolWorkspace?.beginSettings();$("#ruleTags").replaceChildren(...work.categories.map(tagRuleRow));$("#departmentRules").replaceChildren(...work.preferences.departments.map(departmentRow));syncDefaultDepartment();$("#defaultDepartment").value=work.preferences.default_department;$("#tagSettingsSummary").textContent=work.categories.map(t=>t.name).join("、");dialog("rulesDialog");}


$("#loginAccount").addEventListener("change",()=>loginFields($("#loginAccount").value));
$("#loginForm").addEventListener("submit",e=>{e.preventDefault();login($("#loginAccount").value);});
$("#offlineButton").addEventListener("click",()=>act(async()=>{const key=$("#loginAccount").value;await ra("/accounts/offline",{id:key});await enter(key);}));
$("#forgetPassword").addEventListener("click",()=>act(async()=>{const id=$("#loginAccount").value;await ra("/accounts/forget",{id});root=await ra("/bootstrap");renderAccounts();loginFields(id);say("已移除資料庫保存的密碼。");}));
$("#accountSwitch").addEventListener("change",()=>act(async()=>{const key=$("#accountSwitch").value;showEntry(key==="__new"?"":key);root=await ra("/bootstrap");renderAccounts();if(key!=="__new"){const a=root.accounts.find(a=>a.id===key);if(root.read_only||a?.online){await ra("/accounts/offline",{id:key});await enter(key);}else if(a?.remembered)await login(key,true);}}));
for(const b of $$("[data-page]"))b.addEventListener("click",()=>act(()=>navigate(b.dataset.page)));
for(const b of $$("[data-close]"))b.addEventListener("click",()=>$("#"+b.dataset.close).close());
$("#logout").addEventListener("click",()=>act(async()=>{const key=account;clearTimeout(draftTimer);if(!root.read_only){await api("/draft/save",draftValue());await ra("/accounts/logout",{id:key});}showEntry(key);}));
$("#reconnect").addEventListener("click",()=>showEntry(account));
$("#exit").addEventListener("click",()=>act(async()=>{clearTimeout(draftTimer);if(!root.read_only)await api("/draft/save",draftValue());await ra("/shutdown",{});stopped=true;document.body.replaceChildren(empty("VGHKS-bot 已結束，資料與任務進度已保留。"));}));
$("#dateMode").addEventListener("change",dateMode);$("#startDate").addEventListener("change",dateMode);
$("#dateForm").addEventListener("submit",e=>{e.preventDefault();act(()=>browse());});$("#refreshList").addEventListener("click",()=>act(()=>browse(true)));
for(const b of $$("[data-day]"))b.addEventListener("click",()=>act(async()=>{$("#dateMode").value="single";$("#startDate").value=relativeDay(root.today,Number(b.dataset.day));await browse();}));
for(const [id,n] of [["previousDay",-1],["nextDay",1]])$("#"+id).addEventListener("click",()=>act(async()=>{$("#dateMode").value="single";$("#startDate").value=relativeDay($("#startDate").value,n);await browse();}));
for(const [id,tab] of [["ownTab","own"],["sharedTab","shared"]])$("#"+id).addEventListener("click",()=>{listTab=tab;listPage=0;renderList();draftSave();});
$("#selectAll").addEventListener("change",e=>{for(const r of visibleRows()){if(e.target.checked)selected.add(r.id);else selected.delete(r.id);}renderList();draftSave();});
$("#listQuery").addEventListener("input",()=>{listPage=0;renderList();});
$("#manualOpen").addEventListener("click",()=>{$("#identifiers").value="";$("#resolvedPatients").replaceChildren();$("#resolveStatus").textContent="";resolved=[];resolveTask="";$("#confirmResolved").disabled=true;$("#resolveRetry").hidden=true;dialog("manualDialog");});
$("#manualForm").addEventListener("submit",e=>{e.preventDefault();act(async()=>{const task=await api("/tasks/start",{kind:"resolve",identifier_kind:$("#identifierKind").value,identifiers:$("#identifiers").value});resolveTask=task.task_id;$("#resolveStatus").textContent="正在查詢基本資料…";$("#confirmResolved").disabled=true;await refresh();});});
$("#resolveRetry").addEventListener("click",()=>act(async()=>{await api("/tasks/start",{resume:resolveTask});await refresh();}));
$("#confirmResolved").addEventListener("click",()=>{const picked=$$('input[type="checkbox"]:checked',$("#resolvedPatients")).map(n=>n.getAttribute("aria-label").replace("核對 ",""));const merged=new Map(manual.map(p=>[p.mrn,p]));for(const p of resolved)if(picked.includes(p.mrn))merged.set(p.mrn,{mrn:p.mrn,name:p.name,sex:p.sex,birthday:p.birthday,age:p.age});manual=[...merged.values()];$("#manualDialog").close();renderList();draftSave();});
$("#saveCollection").addEventListener("click",()=>act(async()=>{await saveCurrentSet();await refresh();say("病人集合已保存。");}));
$("#useTools").addEventListener("click",()=>act(async()=>{const group=await saveCurrentSet();await refresh();openTools(group);}));
$("#selectedTags").addEventListener("click",()=>openPatientTags(selectedMRNs()));
for(const id of ["toolKind","cutoffMode","soapMode","departmentFilter","reviewRefresh"])$("#"+id).addEventListener("change",toolOptions);
$("#refreshTasks").addEventListener("click",()=>act(refresh));$("#moduleBack").addEventListener("click",()=>navigate("tasks"));
$("#reviewSearchForm").addEventListener("submit",e=>{e.preventDefault();act(loadReview);});$("#reviewTag").addEventListener("change",()=>act(loadReview));
$("#reviewToTools").textContent="勾選病人 → 工具";
$("#reviewToTools").addEventListener("click",()=>act(async()=>openTools(await api("/sets/save",{source:"review",task_id:currentReview,filters:reviewFilters(),selected:[...reviewSelected]}))));
$("#reviewReclassify").addEventListener("click",()=>act(async()=>{await api("/reviews/reclassify",{id:currentReview});await loadReview();say("已套用目前 tag。");}));
$("#libraryForm").addEventListener("submit",e=>{e.preventDefault();libraryPage=0;librarySelected.clear();act(loadLibrary);});
$("#libraryToTools").addEventListener("click",()=>act(async()=>openTools(await api("/sets/save",{source:"library",filters:libraryFilters(),all:true}))));
$("#libraryToTools").before(btn("勾選病人 → 工具",async()=>{if(!librarySelected.size)throw new Error("請先勾選病歷。");openTools(await api("/sets/save",{source:"library",selected:[...librarySelected]}));}));
$("#deleteRecords").addEventListener("click",()=>act(async()=>{if(!librarySelected.size)throw new Error("請先勾選病歷。");if(await confirmDelete(`刪除 ${librarySelected.size} 筆病歷的所有版本與本次檢閱副本？手動 tag 保留。`)){await api("/library/delete",{ids:[...librarySelected]});librarySelected.clear();await loadLibrary();say("病歷已刪除。");}}));
$("#tagSearchForm").addEventListener("submit",e=>{e.preventDefault();tagPage=0;act(loadTags);});
$("#tagsToTools").addEventListener("click",()=>act(async()=>openTools(await api("/sets/save",{source:"tags",filters:tagFilters(),all:true}))));
$("#tagManualOpen").addEventListener("click",()=>openPatientTags());
$("#patientTagForm").addEventListener("submit",e=>{e.preventDefault();const operation=e.submitter?.value||"add";act(async()=>{await api("/patient-tags/update",{mrns:$("#tagMRNs").value,tag_ids:$$('input:checked',$("#tagChoices")).map(n=>n.value),new_tag:$("#newTagName").value,operation});$("#patientTagDialog").close();await refresh();if(page==="tags")await loadTags();if(page==="library")await loadLibrary();if(page==="review")await loadReview();say("手動 tag 已更新。");});});
$("#addRuleTag").addEventListener("click",()=>{$("#ruleTags").append(tagRuleRow());Choices.sync();});
$("#addDepartment").addEventListener("click",()=>{$("#departmentRules").append(departmentRow());syncDefaultDepartment();});
$("#rulesForm").addEventListener("submit",e=>{e.preventDefault();act(async()=>{const categories=$$(".rule-row").map(r=>({id:r.dataset.id,name:r.querySelector('[name="tag-name"]').value,keywords:r.querySelector("textarea").value.split(/\r?\n/).filter(s=>s.trim()),parser:r.dataset.parser,scope:r.querySelector('input[type="radio"]:checked').value}));const departments=$$(".department-rule").map(r=>({id:r.dataset.id,name:r.querySelector("input").value,codes:r.querySelector('[data-field="codes"]').value.split(/\r?\n/).filter(s=>s.trim()),names:r.querySelector('[data-field="names"]').value.split(/\r?\n/).filter(s=>s.trim())}));await api("/reviews/preferences",{departments,default_department:$("#defaultDepartment").value,review_options:window.ToolWorkspace.queryOptions()});await api("/settings",{categories});window.ToolWorkspace?.commitSettings();$("#rulesDialog").close();await refresh();if(!$("#toolSourcePanel").hidden)toolDepartments();say("檢閱設定已保存。");});});
$("#rulesDialog").addEventListener("close",()=>window.ToolWorkspace?.cancelSettings());
$("#rulesForm").addEventListener("invalid",e=>{for(let p=e.target.parentElement;p&&p!==e.currentTarget;p=p.parentElement)if(p.tagName==="DETAILS")p.open=true;},true);
for(const button of $$("button[data-icon]"))setIcon(button,button.dataset.icon,button.dataset.iconLabel||button.textContent,button.dataset.iconText!=="true");
for(const [id,name,label] of [["reviewReclassify","refresh","套用目前 tag"],["reviewToTools","patients","勾選病人 → 工具"],["refreshTasks","refresh","更新任務紀錄"],["refreshList","refresh","更新門診清單"],["previousDay","left","前一天"],["nextDay","right","後一天"]])setIcon($("#"+id),name,label);
$("#accountSettings").addEventListener("click",()=>{$("#settingsAccount").textContent=(info()?.label||info()?.username)+" · "+info()?.campus;$("#concurrency").value=String(root.concurrency);dialog("settingsDialog");});
$("#editLogin").addEventListener("click",()=>showEntry(account));
$("#saveConcurrency").addEventListener("click",()=>act(async()=>{const r=await ra("/concurrency",{value:Number($("#concurrency").value)});root.concurrency=r.concurrency;$("#settingsDialog").close();say("連線設定已保存。");}));
window.addEventListener("message",e=>{if(e.origin!==location.origin||e.source!==$("#moduleFrame").contentWindow)return;if(e.data?.type==="bot:task-started")act(refresh);});
async function poll(){if(stopped)return;if(account&&!pollBusy){pollBusy=true;try{await refresh();if(page==="approvals"){await window.Approvals?.poll();}if(page==="earnings")await window.Earnings?.poll();if(page==="surgery")await window.SurgerySystem?.poll();applyReadonly();}catch(e){if(e.name!=="AbortError")say(e.message,true);}finally{pollBusy=false;}}if(!stopped)setTimeout(poll,2000);}
async function initialize(){const token=location.hash.slice(1);if(/^[A-Za-z0-9_-]{43}$/.test(token)){await ra("/session",{token});history.replaceState(null,"",location.pathname);}root=await ra("/bootstrap");$("#version").textContent=$("#sidebarVersion").textContent="v"+root.version;renderAccounts();loginFields(root.last_account||"");if(root.read_only&&root.accounts.length){const key=root.last_account||root.accounts[0].id;await ra("/accounts/offline",{id:key});await enter(key);}else if(root.accounts.find(a=>a.id===root.last_account)?.remembered)await login(root.last_account,true);applyReadonly();if(root.database_notice){say(root.database_notice);await window.DatabaseUI.open();}poll();}
initialize().catch(e=>say(e.message,true));
