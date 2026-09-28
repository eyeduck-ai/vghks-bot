"use strict";
const $ = selector => document.querySelector(selector);
const $$ = (selector, parent=document) => [...parent.querySelectorAll(selector)];
const node = (tag, text, cls) => {const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
const iconPaths={search:["M21 21l-5-5","M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14"],folder:["M3 7V4h6l2 3h10v13H3Z","M3 10h18"],patients:["M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2","M9 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8","M17 4a4 4 0 0 1 0 7M22 21v-2a4 4 0 0 0-3-4"],settings:["M4 7h16M4 17h16","M9 4v6M15 14v6"],close:["M6 6l12 12M6 18 18 6"],plus:["M12 5v14M5 12h14"],refresh:["M20 8a8 8 0 1 0 0 8","M20 3v5h-5"],left:["M15 5l-7 7 7 7"],right:["M9 5l7 7-7 7"],calendar:["M4 5h16v16H4ZM4 10h16M8 3v4M16 3v4"],tag:["M3 3h8l10 10-8 8L3 11ZM7 7h.01"],chart:["M4 3v17h17M8 14l4-5 4 3 5-7"],diagnostic:["M14 2H4v20h16V8ZM14 2v6h6M8 12h8M8 16h5"],trash:["M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"]};
iconPaths.help=["M12 17h.01","M9.5 9a2.5 2.5 0 1 1 4.1 1.9c-.9.7-1.6 1.2-1.6 2.6","M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20"];
iconPaths.panelClose=["M3 4.5h18v15H3z","M9 4.5v15","M16 9l-3 3 3 3"];
iconPaths.panelOpen=["M3 4.5h18v15H3z","M9 4.5v15","M13 9l3 3-3 3"];
iconPaths.orders=["M5 3h14v18H5z","M8 8h8M8 12h8M8 16h5"];
iconPaths.history=["M12 3a9 9 0 1 1-8.5 6","M3 3v6h6","M12 7v5l3 2"];
iconPaths.info=["M12 11v6M12 7h.01","M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20"];
const reviewIconFiles={reportCurrent:"review-report-current.svg",reportHistory:"review-report-history.svg",ordersCurrent:"review-orders-current.svg",ordersHistory:"review-orders-history.svg",scanCurrent:"review-scan-current.svg",scanHistory:"review-scan-current.svg",visitsHistory:"review-visits-history.svg",registrationRecords:"review-registration-records.svg",tagAdd:"review-tag-add.svg",noteAdd:"review-note-add.svg"};
function setIcon(button,name,label,only=true){
 let icon;
 if(reviewIconFiles[name]){icon=document.createElement("img");icon.src="/"+reviewIconFiles[name];icon.alt="";icon.width=36;icon.height=36;icon.draggable=false;icon.className="icon review-icon";icon.setAttribute("aria-hidden","true");}
 else{const svg=document.createElementNS("http://www.w3.org/2000/svg","svg");svg.setAttribute("viewBox","0 0 24 24");svg.setAttribute("fill","none");svg.setAttribute("stroke","currentColor");svg.setAttribute("stroke-width","1.7");svg.setAttribute("stroke-linecap","round");svg.setAttribute("stroke-linejoin","round");svg.setAttribute("aria-hidden","true");svg.classList.add("icon");for(const value of iconPaths[name]||iconPaths.settings){const path=document.createElementNS(svg.namespaceURI,"path");path.setAttribute("d",value);svg.append(path);}icon=svg;}
 button.replaceChildren(icon,node("span",label,only?"sr-only":""));if(name==="scanHistory")button.append(node("span","◷","scan-clock"));button.title=label;button.setAttribute("aria-label",label);button.classList.toggle("icon-button",only);button.classList.toggle("icon-text",!only);button.classList.toggle("review-icon-button",!!reviewIconFiles[name]);if(name==="scanHistory")button.classList.add("scan-history-button");return button;
}
const actionIcons={"上一位":"left","下一位":"right","掛號紀錄":"registrationRecords","加入／移除 tag":"tagAdd","DEBUG 資訊":"help","該次數值類報告":"reportCurrent","歷年數值類報告":"reportHistory","該次醫囑清單":"ordersCurrent","歷年醫囑清單":"ordersHistory","該次掃描病歷":"scanCurrent","歷年掃描病歷":"scanHistory","歷次就診":"visitsHistory","重試":"refresh","移除":"trash"};
const btn = (text, callback, cls) => {const n=node("button",text,cls);n.type="button";if(actionIcons[text])setIcon(n,actionIcons[text],text);n.addEventListener("click",()=>act(callback,n));if(root?.read_only&&/^(續跑|暫停|改名|移除|使用工具|加入|更新|刪除|保存)/.test(text))n.disabled=true;return n;};
function patientChip(patient,remove){const name=patient.name||"姓名未提供",button=btn(name+"（"+patient.mrn+"） ×",remove,"patient-chip");button.setAttribute("aria-label","移除病人 "+name+"，病歷號 "+patient.mrn);button.title="點擊移除這位病人";return button;}
const labels={queued:"排隊中",running:"執行中",completed:"完成",partial:"部分完成",paused:"待續跑",cancelled:"已暫停",interrupted:"待續跑",failed:"失敗",cancelling:"暫停中",ready:"已取得",pending:"待處理",no_visit:"查無就診",unknown:"需確認",missing:"SOAP 尚無內容",error:"未取得",future:"尚未到診",deleted:"已刪除",no_source:"未指定來源"};
const inProgress=new Set(["queued","running","cancelling"]);
labels.forbidden="權限不足";
let root, account="", epoch=0, work, listing, selected=new Set(), manual=[], listTab="own", page="patients";
let activeListRun="", selectedSet=null, currentReview="", reviewData=null, reviewPatient="", reviewSignature="";
let libraryData, libraryPage=0, librarySelected=new Set(), tagPage=0, extensionTask="", extensionContext=null;
let libraryRecordSequence=0, libraryReturnFocus=null;
let toastTimer, draftTimer, pollBusy=false, searchSequence=0, stopped=false;
let collapsedDays=new Set();
let suppressCompletedReview=false,reconnectTask="",reconnectTarget="";
const reconnectAttempts=new Map(),recoverySeen=new Map(),resumedTasks=new Set();
let recoveringConnection=false;
let reviewHeaderTask="",reviewHeaderExpanded=false,reviewSetupExpanded=false,reviewHasResults=false;

function say(message,error=false){const n=$(error?"#notice":"#toast");n.textContent=message;n.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>n.hidden=true,error?9000:3500);}
function applyReadonly(){window.DatabaseUI?.readonly();}
async function act(callback,source=document.activeElement){$("#notice").hidden=true;const revision=epoch,control=source?.tagName==="BUTTON"?source:null;if(control?.dataset.busy)return;const wasDisabled=control?.disabled;if(control){control.dataset.busy="true";control.setAttribute("aria-busy","true");control.disabled=true;}try{return await callback();}catch(e){if(e.name!=="AbortError")say(e.message||"操作未完成",true);}finally{if(control){delete control.dataset.busy;control.removeAttribute("aria-busy");if(control.isConnected&&revision===epoch)control.disabled=!!wasDisabled;}}}
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
function updateAccountHeader(){const current=info(),label=$("#currentAccountLabel");label.textContent=current?.label||current?.username||"目前帳號";label.title=current?(current.label||current.username)+" · "+current.campus:"";}
function fillSelect(select,items,placeholder="全部",selectedValue=select.value){select.replaceChildren(new Option(placeholder,""),...items.map(i=>new Option(i.name||i.label||i.id,i.id)));if(items.some(i=>i.id===selectedValue))select.value=selectedValue;}
function badges(tags,manualTag=false){const wrap=node("span");for(const tag of tags||[])wrap.append(node("span",(manualTag?"手動 · ":"")+(tag.name||tag.category_name),"badge"+(manualTag?" manual":"")));return wrap;}
function categoryOptions(){const cats=work?.categories||[];for(const id of ["reviewTag","libraryTag","tagFilter"])fillSelect($("#"+id),cats,"全部 tag");}
function empty(text){return node("p",text,"empty");}
function table(headers,rows){const wrap=node("div",undefined,"table-wrap"),table=node("table",undefined,"patient-table"),head=node("thead"),tr=node("tr"),body=node("tbody");for(const h of headers){const th=node("th",h);th.scope="col";tr.append(th);}head.append(tr);for(const cells of rows){const r=node("tr");for(const value of cells){const c=node("td");c.append(value instanceof Node?value:node("span",value??"—"));r.append(c);}body.append(r);}table.append(head,body);wrap.append(table);return wrap;}
function patientName(p){const n=node("span");n.append(node("strong",p.name||"姓名未提供"),node("span",p.mrn,"mrn"));return n;}
function check(label,checked,change){const n=node("input");n.type="checkbox";n.checked=checked;n.setAttribute("aria-label",label);n.addEventListener("change",()=>change(n.checked));return n;}
function paginate(container,index,total,change){container.replaceChildren();const max=Math.max(1,Math.ceil(total/40));const before=btn("上一頁",()=>change(index-1)),after=btn("下一頁",()=>change(index+1));before.disabled=index===0;after.disabled=index+1>=max;container.append(before,node("span",`第 ${index+1} / ${max} 頁`),after);}
function reviewPatientIdentity(mrn,taskId=currentReview){
 const patient=(reviewData?.task.id===taskId?reviewData.patients.find(row=>row.mrn===mrn):null)
  ||work?.sets?.flatMap(group=>group.members||[]).find(row=>row.mrn===mrn)
  ||manual.find(row=>row.mrn===mrn);
 return {name:patient?.name||"姓名未提供",mrn};
}
window.reviewPatientIdentity=reviewPatientIdentity;
function setPatientDialogContext(id,mrn,name="",taskId=currentReview){
 const dialogElement=$("#"+id),context=$(id==="dataDialog"?"#dataPatient":"#tagPatient");
 if(!mrn){context.textContent="";context.hidden=true;dialogElement.removeAttribute("aria-describedby");return;}
 const patient=reviewPatientIdentity(mrn,taskId);
 context.textContent=(name||patient.name)+" · 病歷號 "+mrn;
 context.hidden=false;dialogElement.setAttribute("aria-describedby",context.id);
}
function dialog(id){Choices.sync();if(id==="dataDialog"){$("#dataDialog").dataset.view="general";setPatientDialogContext(id,"");}if(!$("#"+id).open)$("#"+id).showModal();}
function confirmDelete(text){$("#confirmText").textContent=text;const d=$("#confirmDialog");d.returnValue="";d.showModal();return new Promise(resolve=>d.addEventListener("close",()=>resolve(d.returnValue==="delete"),{once:true}));}

function renderAccounts(){
 const keep=$("#loginAccount").value;
 fillSelect($("#loginAccount"),root.accounts.map(a=>({id:a.id,name:(a.label||a.username)+" · "+a.campus})),"新增帳號",keep);
 $("#accountSwitch").replaceChildren(...root.accounts.map(a=>new Option((a.label||a.username)+" · "+a.campus+(a.online?" · 已連線":""),a.id)),new Option("＋ 新增帳號","__new"));
 $("#accountSwitch").value=account;
 updateAccountHeader();
}
function loginFields(key=""){
 const a=root.accounts.find(a=>a.id===key);$("#loginAccount").value=key;$("#loginUsername").value=a?.username||"";$("#loginUsername").readOnly=!!a;
 $("#loginLabel").value=a?.label||"";const campus=a?.campus||"高榮",campusField=$("#loginCampus");campusField.querySelector("option[data-legacy]")?.remove();if(![...campusField.options].some(option=>option.value===campus)){const legacy=new Option(campus+"（既有院區）",campus);legacy.dataset.legacy="true";campusField.add(legacy);}campusField.value=campus;$("#loginPassword").value="";$("#loginPassword").placeholder=a?.remembered?"已保存；留白使用保存的密碼":"請輸入院內密碼";
 $("#rememberLogin").checked=a?!!a.remembered:true;$("#offlineButton").disabled=!a;$("#forgetPassword").disabled=!a?.remembered;$("#loginError").hidden=true;
}
function showEntry(key=""){
 if(account&&work&&!root.read_only){clearTimeout(draftTimer);api("/draft/save",draftValue()).catch(()=>{});}
 clearTimeout(draftTimer);manualLookup.reset();manualErrors.clear();renderManualErrors();window.ToolWorkspace?.reset();window.SurgerySystem?.reset();window.ReviewNotesUI?.reset();epoch++;account="";$("#moduleFrame").removeAttribute("src");for(const d of $$("dialog[open]"))d.close();$("#shell").hidden=true;$("#entry").hidden=false;loginFields(key);
 window.FileCompare?.context("", "");
}
async function login(key, automatic=false){
 const revision=epoch;
 const control=$("#loginSubmit");control.disabled=true;control.textContent="正在登入…";$("#loginError").hidden=true;
 try {
  const body=automatic?{id:key}:{id:key||undefined,username:$("#loginUsername").value,password:$("#loginPassword").value,label:$("#loginLabel").value,campus:$("#loginCampus").value,remember:$("#rememberLogin").checked};
  const value=await ra("/accounts/login",body);if(revision!==epoch)return;reconnectAttempts.delete(value.account.id);$("#loginPassword").value="";root=await ra("/bootstrap");if(revision!==epoch)return;renderAccounts();await enter(value.account.id);
 }catch(e){if($("#entry").hidden)say("帳號已登入，畫面載入未完成："+(e.message||"請稍後重試"),true);else{$("#loginError").textContent=e.message;$("#loginError").hidden=false;}}
 finally{control.disabled=false;control.textContent="登入";}
}
function openReconnect(taskId="",target=account,reason=""){
 if(!target||root.read_only)return;
 const selected=root.accounts.find(a=>a.id===target);
 $("#reconnectTitle").textContent=target===account?"重新連線院內系統":"連線至 "+(selected?.label||selected?.username||"所選帳號");
 reconnectTask=taskId;reconnectTarget=target;$("#reconnectReason").textContent=reason||`${selected?.label||selected?.username||"此帳號"}：`+(taskId?"院內連線已中斷；重新連線後會從已保存的任務進度續跑。":"重新連線後會保留目前畫面與已取得的資料。");
 $("#reconnectPassword").value="";$("#reconnectPassword").placeholder=selected?.remembered?"已保存密碼可留白":"請輸入院內密碼";
 $("#reconnectRemember").checked=!!selected?.remembered;$("#reconnectError").hidden=true;
 dialog("reconnectDialog");$("#reconnectPassword").focus();
}
async function enter(key){
 collapsedDays.clear();manualLookup.reset();manualErrors.clear();renderManualErrors();
 window.ReviewHistoryUI?.reset();window.ReviewNotesUI?.reset();reconnectTask="";reconnectTarget="";
 epoch++;account=key;window.FileCompare?.context(key, "");window.Approvals?.reset();window.ToolWorkspace?.reset();window.Earnings?.reset();window.SurgerySystem?.reset();work=null;listing=null;selected=new Set();manual=[];libraryPage=tagPage=0;listTab="own";selectedSet=null;currentReview="";reviewData=null;reviewPatient="";reviewSignature="";extensionTask=activeListRun="";librarySelected.clear();
 $("#moduleFrame").removeAttribute("src");for(const d of $$("dialog[open]"))d.close();
 for(const id of ["reviewQuery","listQuery","libraryQuery","libraryStart","libraryEnd","tagQuery"])$("#"+id).value="";
 for(const id of ["libraryTag","tagFilter","reviewTag","libraryTask"])$("#"+id).value="";
 $("#entry").hidden=true;$("#shell").hidden=false;$("#accountSwitch").value=key;updateAccountHeader();$("#startDate").value=$("#endDate").value=root.today;$("#dateMode").value="single";dateMode();
 for(const id of ["taskList","libraryRecords","tagPatients","reviewPatients","reviewDetail","dataContent","librarySoapContent","savedSets"])$("#"+id)?.replaceChildren();
 $("#listJob").textContent="";$("#connectionStatus").textContent="載入中";showPage("patients");renderList();renderSets();
 await refresh();const draft=work.draft;
 if(draft){$("#startDate").value=draft.start||root.today;$("#endDate").value=draft.end||root.today;$("#dateMode").value=draft.mode||"single";dateMode();selected=new Set(draft.selected||[]);manual=draft.manual||[];listTab=draft.tab||"own";}
 await readList();showPage("patients");renderSets();applyReadonly();
}
function showPage(name){page=name;$("#sidebarAccount").open=false;Choices.sync();window.ToolWorkspace?.visibility(name);window.SurgerySystem?.visibility(name);for(const section of $$("main>.page"))section.hidden=section.id!==name+"Page";for(const b of $$("[data-page]"))b.classList.toggle("active",b.dataset.page===name);$("#databaseBanner").hidden=name==="review";$("#sidebarDatabaseStatus").hidden=name!=="review";document.body.classList.toggle("review-result-compact",name==="review"&&reviewData&&["completed","partial"].includes(reviewData.task.status)&&reviewHasResults&&!reviewSetupExpanded);}
function toggleReviewPatients(){const hidden=!$("#reviewPatients").hidden;$("#reviewPatients").hidden=hidden;$("#reviewLayout").classList.toggle("patients-collapsed",hidden);const toggle=$("#reviewPatientsToggle");toggle.setAttribute("aria-expanded",String(!hidden));setIcon(toggle,hidden?"panelOpen":"panelClose",hidden?"展開病人清單":"收合病人清單");}
async function navigate(name){if(page==="review"&&name!=="review"&&!await window.ReviewNotesUI?.flushAll())throw new Error("有備註尚未儲存，請重試後再切換頁面。");showPage(name);if(name==="library")await loadLibrary();if(name==="tags")await loadTags();if(name==="tasks")await refresh();if(name==="approvals"){await window.Approvals.open();}if(name==="earnings")await window.Earnings.open();if(name==="surgery")await window.SurgerySystem.open();}
const draftValue=()=>({start:$("#startDate").value,end:$("#endDate").value,mode:$("#dateMode").value,selected:[...selected],manual,tab:listTab});
function draftSave(){if(root.read_only)return;clearTimeout(draftTimer);const key=account;draftTimer=setTimeout(()=>{if(key!==account)return;act(()=>api("/draft/save",draftValue()));},250);}
function dateMode(){Choices.sync();const single=$("#dateMode").value==="single";$("#endDateField").hidden=single;if(single)$("#endDate").value=$("#startDate").value;$("#endDate").min=$("#startDate").value;}
function moveDay(day){const d=new Date(day+"T12:00:00Z");return d.toISOString().slice(0,10);}
function relativeDay(base,offset){const d=new Date(base+"T12:00:00Z");d.setUTCDate(d.getUTCDate()+offset);return moveDay(d.toISOString().slice(0,10));}
const ranges=()=>({start:$("#startDate").value,end:$("#dateMode").value==="single"?$("#startDate").value:$("#endDate").value});
function own(r){return r.ownership==="DEDICATED";}
const allRows=()=>listing?.days.flatMap(d=>d.rows)||[];
function visibleRows(){const q=$("#listQuery").value.trim().toLocaleLowerCase();return allRows().filter(r=>(listTab==="own"?own(r):!own(r))&&(!q||(r.mrn+" "+r.name+" "+(r.sequence_no||"")).toLocaleLowerCase().includes(q)));}
function selectedPatients(){const patients=new Map();for(const row of allRows())if(selected.has(row.id)&&row.mrn&&!patients.has(row.mrn))patients.set(row.mrn,{mrn:row.mrn,name:row.name});for(const patient of manual)if(!patients.has(patient.mrn)||!patients.get(patient.mrn).name)patients.set(patient.mrn,patient);return [...patients.values()];}
function selectedMRNs(){return selectedPatients().map(patient=>patient.mrn);}
function removeSelectedPatient(mrn){for(const row of allRows())if(row.mrn===mrn)selected.delete(row.id);manual=manual.filter(patient=>patient.mrn!==mrn);renderList();draftSave();}
function renderManualChips(){$("#resolvedPatients").replaceChildren(...manual.map(patient=>patientChip(patient,()=>removeSelectedPatient(patient.mrn))));}
async function browse(force=false){
 dateMode();if(!$("#dateForm").reportValidity())return;
 selected.clear();manual=[];activeListRun="";
 let pending=JobProgress.pending("#listJob","門診清單","正在準備門診清單查詢…");
 try{
  await readList();collapsedDays.clear();
  if(work.online){const value=await api("/lists/browse",{account_ids:[account],ranges:{[account]:ranges()},force});activeListRun=value.run_ids[0];pending=JobProgress.pending("#listJob","門診清單","任務已排入，等待院內資料…");}
  else{pending?.remove();say("離線顯示已保存的門診清單。");}
  for(const r of allRows())if(own(r)&&r.mrn)selected.add(r.id);renderList();draftSave();
 }catch(error){pending?.remove();throw error;}
}
async function readList(){listing=await api("/lists",{account_id:account,...ranges()});renderList();}
function renderList(){
 const rows=visibleRows(),mine=allRows().filter(own).length;$("#ownTab").textContent=`我的門診（${mine}）`;$("#sharedTab").textContent=`其他／共用門診（${allRows().length-mine}）`;
 $("#ownTab").setAttribute("aria-selected",listTab==="own");$("#sharedTab").setAttribute("aria-selected",listTab!=="own");
 const chosen=rows.filter(r=>selected.has(r.id)).length;$("#selectAll").checked=!!rows.length&&chosen===rows.length;$("#selectAll").indeterminate=chosen>0&&chosen<rows.length;$("#selectAll").disabled=!rows.length;
 $("#listSummary").textContent=`${rows.length} 筆掛號 · 已選 ${chosen} 筆`;
 const content=$("#registrationGroups");content.replaceChildren();const groups=new Map();
 for(const row of rows){if(!groups.has(row.day))groups.set(row.day,[]);groups.get(row.day).push(row);}
 for(const [day,items] of groups){
  const detail=node("details",undefined,"registration-day"),groupKey=listTab+day;detail.open=!collapsedDays.has(groupKey);detail.addEventListener("toggle",()=>{if(detail.isConnected){if(detail.open)collapsedDays.delete(groupKey);else collapsedDays.add(groupKey);}});const summary=node("summary",day);const allDay=rows.filter(r=>r.day===day);summary.append(node("span",`${allDay.length} 筆 · 已選 ${allDay.filter(r=>selected.has(r.id)).length}`,"day-count"));detail.append(summary);
  const selectLine=node("div",undefined,"date-selection"),label=node("label",undefined,"check");label.append(check("選取 "+day+" 全日病人",allDay.every(r=>selected.has(r.id)),v=>{for(const r of allDay){if(v)selected.add(r.id);else selected.delete(r.id);}renderList();draftSave();}),node("span","選取此日期"));selectLine.append(label);detail.append(selectLine);
  detail.append(table(["選取","掛號序號","病人／病歷號","性別／年齡","科別／診間","狀態"],items.map(r=>[
   check("選取 "+r.mrn+" "+r.day+" 掛號序號 "+(r.sequence_no||"未提供"),selected.has(r.id),v=>{if(v)selected.add(r.id);else selected.delete(r.id);renderList();draftSave();}),r.sequence_no||"—",patientName(r),(r.sex||"—")+" · "+(r.age||"—"),(r.section_code||"未註明")+" / "+(r.room||"—"),
   own(r)?(r.day>root.today?"未來掛號":r.status):r.doctor_card?"其他醫師 · "+r.doctor_card:r.doctor_label_present?"共用門診":"歸屬未確認"
  ])));content.append(detail);
 }
 if(!rows.length)content.append(empty(listing?.days.some(d=>d.cached)?"此範圍沒有符合的掛號。":"選擇日期查看門診清單，或手動加入病人。"));
 $("#manualMembersSection").hidden=!manual.length;$("#manualCount").textContent=manual.length+" 位";
 $("#manualMembers").replaceChildren(table(["病人／病歷號","性別／生日","操作"],manual.map(p=>[patientName(p),(p.sex||"—")+" · "+(p.birthday||p.age||"—"),btn("移除",()=>{manual=manual.filter(m=>m.mrn!==p.mrn);renderList();draftSave();},"quiet")])));
 const n=selectedMRNs().length;$("#selectionCount").textContent=`已選 ${n} 位病人`;
 $("#listSelectedChips").replaceChildren(...selectedPatients().map(patient=>patientChip(patient,()=>removeSelectedPatient(patient.mrn))));
 renderManualChips();
 for(const id of ["saveCollection","useTools","selectedTags"])$("#"+id).disabled=!n;for(const button of $$("[data-use-tool]"))button.disabled=!n;
}
async function saveCurrentSet(){return api("/sets/save",{registrations:allRows().filter(r=>selected.has(r.id)).map(r=>({day:r.day,id:r.id})),mrns:manual.map(p=>p.mrn).join("\n"),source_range:ranges()});}
function renderSets(){const container=$("#savedSets");container.replaceChildren();for(const group of work?.sets||[]){const r=node("div",undefined,"saved-row"),text=node("div");text.append(node("strong",group.name),node("span",`${group.members.length} 位 · ${time(group.updated_at)}`,"caption"));const actions=node("div",undefined,"actions");actions.append(btn("使用工具",()=>openTools(group)),btn("改名",()=>renameSet(group),"quiet"),btn("移除",async()=>{if(await confirmDelete("移除此病人集合？病歷資料會保留。")){await api("/sets/delete",{id:group.id});await refresh();}},"quiet"));r.append(text,actions);container.append(r);}if(!work?.sets.length)container.append(empty("尚無保存的病人集合。"));}
function renameSet(group){$("#dataTitle").textContent="集合名稱";const form=node("form"),input=node("input"),save=node("button","保存","primary");input.value=group.name;input.required=true;input.maxLength=100;input.setAttribute("aria-label","集合名稱");save.type="submit";form.append(input,save);form.addEventListener("submit",event=>{event.preventDefault();act(async()=>{await api("/sets/save",{id:group.id,name:input.value});$("#dataDialog").close();await refresh();});});$("#dataContent").replaceChildren(form);dialog("dataDialog");}
async function openTools(group){return window.ToolWorkspace.open("review",group);}
function toolOptions(){
 const registration=$("#soapMode").value==="registration";
 $("#soapModeHelp").textContent=(registration?"依選取的掛號日期與科別取得該次門診 SOAP；未來掛號尚未到診。":"取得病人最新符合條件的門診 SOAP；若較新紀錄無內容，會往前補找。")+"就診日期以今天為上限；科別名稱或代碼包含任一關鍵字即符合，以逗號或頓號分隔。";
 $("#reviewRefreshHelp").textContent=$("#forceReview").checked?"本次重新查詢就診紀錄並下載 SOAP；舊版本保留。":"預設重用有效的已存病歷；索引或 SOAP 快取過期、資料缺漏時才補抓。";Choices.sync();
}
async function openModule(cohort,module="retina"){window.ToolWorkspace?.activate(module);if(await window.ToolWorkspace?.adoptModule(cohort)===false)return;$("#moduleTitle").textContent={retina:"視網膜比較",cataract:"白內障術前比較",surgery:"刀表更新"}[module]||"檢查比較";$("#moduleFrame").src="/tools?account="+encodeURIComponent(account)+"&cohort="+encodeURIComponent(cohort)+"&module="+encodeURIComponent(module);showPage("module");}
function renderTasks(history){
 const container=$("#taskList");container.replaceChildren();const tasks=[...(work.tasks||[]).map(t=>({...t,bot:true})),...(work.sdk_sessions||[]).map(t=>({...t,session:true})),...(history?.runs||[]).filter(r=>r.kind!=="bot")].sort((a,b)=>(b.created_at||"").localeCompare(a.created_at||""));
 $("#taskCount").textContent=tasks.filter(t=>inProgress.has(t.status)).length||"";
 for(const task of tasks){const row=node("div",undefined,"task-row"),desc=node("div"),actions=node("div",undefined,"actions");const source=task.kind.startsWith("approval_")?"審查查詢":task.kind.startsWith("earnings_")?"薪資業績":task.kind==="surgery_schedule"?"手術系統":task.session?"SDK":task.kind==="analysis"?"比較工具":"門診系統";desc.append(node("p",source,"task-source"),node("h3",task.name||(task.kind==="analysis"?"歷年檢查比較":"門診清單")),node("p",`${labels[task.status]||task.status} · ${time(task.created_at)} · ${task.message||""}`));
  if(task.bot&&task.kind==="review")actions.append(btn("檢閱",()=>openReview(task.id)));
  else if(task.bot&&task.kind==="surgery_schedule")actions.append(btn("查看排程",async()=>{showPage("surgery");await window.SurgerySystem.open(task.id);}));
  else if(task.bot&&task.kind==="resolve")actions.append(btn("加入已核對病人",async()=>{const detail=await api("/tasks/detail?id="+encodeURIComponent(task.id));for(const patient of detail.items.filter(item=>item.status==="resolved"))addManualPatient(patient);dialog("manualDialog");manualLookup.focus();}));
  else if(task.bot&&task.kind==="history")actions.append(btn("查看",()=>window.ReviewHistoryUI.openTask(task)));
  else if(task.bot&&["numeric","registrations"].includes(task.kind))actions.append(btn("查看",()=>showExtensionTask(task.id,task.kind)));
  else if(task.bot)actions.append(btn("查看摘要",()=>showTaskSummary(task.id)));
  else if(task.kind==="analysis")actions.append(btn("檢閱結果",()=>openModule(task.cohort_id,task.modules?.[0]||"retina")));
  if(!task.session&&inProgress.has(task.status))actions.append(btn("暫停",async()=>{await api(task.bot?"/tasks/stop":"/stop",{id:task.id});await refresh();},"quiet"));
  else if(!task.session&&["paused","partial","failed","cancelled","interrupted"].includes(task.status)&&task.kind!=="list")actions.append(btn("續跑",async()=>{if(task.bot)await api("/tasks/start",{resume:task.id});else await api("/analysis/start",{resume:task.id});await refresh();},"primary"));
  const debug=btn("DEBUG 資訊",()=>showDiagnostics(task.session?{session_id:task.id}:{task_id:task.id}));setIcon(debug,"help","DEBUG 資訊",false);actions.append(debug);
  if(!task.session)desc.append(JobProgress.create(task));row.append(desc,actions);container.append(row);
 }if(!tasks.length)container.append(empty("尚無 SDK 抓取紀錄。"));
}
async function showTaskSummary(id){const task=await api("/tasks/detail?id="+encodeURIComponent(id));$("#dataTitle").textContent=task.name||"抓取任務";const content=$("#dataContent");content.replaceChildren(table(["欄位","內容"],[["狀態",labels[task.status]||task.status],["開始",time(task.created_at)],["完成",time(task.finished_at)||"—"],["進度",`${task.done??0} / ${task.total??"—"}`],["說明",task.message||"—"]]));const all=task.items||[],items=all.slice(0,200);if(items.length){content.append(node("h3","處理項目"),table(["項目","狀態","說明／錯誤代碼"],items.map(item=>[item.mrn||item.apply_seq||[item.kind,item.period].filter(Boolean).join(" · ")||item.part||"—",labels[item.status]||item.status||"—",[item.message,item.code].filter(Boolean).join(" · ")||"—"])));if(all.length>items.length)content.append(node("p",`只顯示前 ${items.length} / ${all.length} 項。`,"caption"));}dialog("dataDialog");}
async function refresh(){
 if(!account)return;const [value,history]=await Promise.all([api("/workbench"),api("/history")]);work=value;window.Approvals?.badge(value.approval_counts,value.approval_monitor_enabled);categoryOptions();renderSets();renderTasks(history);$("#connectionStatus").textContent=work.online?"已登入":work.offline_mode?"離線檢閱":"連線中斷";$("#connectionStatus").classList.toggle("offline",!work.online);$("#dataDirectory").textContent=info()?.campus+" · "+(info()?.label||info()?.username);
 const previous=recoverySeen.get(account);recoverySeen.set(account,work.recovery_count||0);if(previous!==undefined&&work.recovery_count>previous)say("院內連線已自動恢復，查詢已繼續。");
 if(activeListRun){const run=history.runs.find(r=>r.id===activeListRun);if(run){JobProgress.show("#listJob",run);if(!inProgress.has(run.status)){activeListRun="";await readList();for(const r of allRows())if(own(r)&&r.mrn)selected.add(r.id);renderList();draftSave();}}}
 await window.ToolWorkspace?.poll();
 if(extensionTask&&$("#dataDialog").open)await showExtensionTask(extensionTask,extensionContext?.kind,false);
 await window.ReviewHistoryUI?.poll();
 if(currentReview&&page==="review"){const task=work.tasks.find(t=>t.id===currentReview),signature=task?.updated_at+"/"+task?.status;if(task&&signature!==reviewSignature)await loadReview();}
}
function addManualPatient(patient){
 if(!patient.mrn)return;
 const profiles=new Map(manual.map(value=>[value.mrn,value]));
 profiles.set(patient.mrn,{mrn:patient.mrn,name:patient.name,sex:patient.sex,birthday:patient.birthday,age:patient.age});
 manual=[...profiles.values()];renderList();draftSave();
}
async function openReview(id){if(currentReview&&currentReview!==id&&!await window.ReviewNotesUI.flushAll())throw new Error("有備註尚未儲存，請重試後再切換任務。");suppressCompletedReview=work.tasks.some(t=>t.id===id&&t.status==="completed");window.ToolWorkspace?.activate("review");currentReview=id;reviewData=null;reviewSignature="";reviewPatient="";$("#reviewQuery").value="";$("#reviewTag").value="";showPage("review");await loadReview();}
async function resumeReview(){
 const task=(work?.tasks||[]).find(t=>t.id===currentReview&&t.kind==="review")||(work?.tasks||[]).find(t=>t.kind==="review");
 if(!task)return window.ToolWorkspace.open("review");
 if(task.id!==currentReview)return openReview(task.id);
 window.ToolWorkspace.activate("review");showPage("review");
 if(!reviewData||reviewSignature!==task.updated_at+"/"+task.status)await loadReview();
 else{window.ToolWorkspace.adoptReview(reviewData.task);renderReviewHeader(reviewData);}
}
function interruptedTask(){
 return (work?.tasks||[]).filter(task=>task.status==="paused"&&/^(AUTH_|NETWORK_)/.test(task.error_code||"")&&!resumedTasks.has(account+task.id)&&Date.now()-Date.parse(task.finished_at||"")<300000).sort((a,b)=>(b.finished_at||"").localeCompare(a.finished_at||""))[0];
}
async function maybeRecoverConnection(){
 if(!account||!work||root.read_only||work.online||work.offline_mode||recoveringConnection||work.tasks?.some(task=>inProgress.has(task.status)))return;
 const key=account,state=reconnectAttempts.get(key)||{next:0,delay:15000,blocked:false};
 if(state.blocked||Date.now()<state.next)return;
 recoveringConnection=true;$("#connectionStatus").textContent="重新連線中";
 const taskId=interruptedTask()?.id||"";
 try{
  const result=await ra("/accounts/activate",{id:key});if(account!==key)return;
  if(result.online){reconnectAttempts.delete(key);root=await ra("/bootstrap");renderAccounts();await refresh();if(taskId&&work.tasks.some(task=>task.id===taskId&&task.status==="paused")){resumedTasks.add(key+taskId);await api("/tasks/start",{resume:taskId});await refresh();}say("院內連線已自動恢復。"+ (taskId?"受影響任務已續跑。":""));}
  else if(result.status==="needs_password"){state.blocked=true;reconnectAttempts.set(key,state);openReconnect(taskId,key,result.message?"自動重新連線未通過，請更新此帳號密碼。"+result.message:"");}
  else{state.next=Date.now()+state.delay;state.delay=Math.min(state.delay*2,120000);reconnectAttempts.set(key,state);$("#connectionStatus").textContent="連線中斷";say("院內連線暫時無法恢復，稍後會自動重試。"+(result.message||""),true);}
 }catch(error){if(account===key){state.next=Date.now()+state.delay;state.delay=Math.min(state.delay*2,120000);reconnectAttempts.set(key,state);$("#connectionStatus").textContent="連線中斷";say("重新連線未完成，稍後會自動重試。"+(error.message||""),true);}}
 finally{recoveringConnection=false;}
}
const reviewFilters=()=>({q:$("#reviewQuery").value,search_mode:$("#reviewSearchMode").value,tag:$("#reviewTag").value});
function reviewConditionSummary(task){const department=task.department_keywords?.length?"科別含任一：「"+task.department_keywords.join("」、「")+"」":task.department_keyword?"科別含「"+task.department_keyword+"」":task.department_filter==="all"?"不限科別":"同科別";return [task.mode==="registration"?"該次門診的 SOAP":"最新 SOAP",department,"截至 "+task.cutoff,task.force?"重新下載病歷":task.refresh?"檢查新紀錄":"重用已存病歷"].join(" · ");}
function renderReviewHeader(value){
 const task=value.task;
 if(reviewHeaderTask!==task.id){reviewHeaderTask=task.id;reviewHeaderExpanded=false;reviewSetupExpanded=false;reviewHasResults=false;}
 reviewHasResults ||= value.patients.some(patient=>patient.records?.length);
 const finished=["completed","partial"].includes(task.status)&&reviewHasResults;
 $("#reviewTitle").textContent="病歷檢閱";
 $("#reviewHeadline").textContent=`${value.all_total} 位病人 · ${labels[task.status]||task.status}`;
 $("#reviewContext").textContent=`${task.name||"病人集合"} · 建立於 ${time(task.created_at)}`;
 $("#reviewConditions").textContent=reviewConditionSummary(task);
 $("#reviewHeaderDetails").hidden=!reviewHeaderExpanded;
 const toggle=$("#reviewInfoToggle");toggle.setAttribute("aria-expanded",String(reviewHeaderExpanded));setIcon(toggle,"info",reviewHeaderExpanded?"收起檢閱資訊":"展開檢閱資訊");
 document.body.classList.toggle("review-result-compact",page==="review"&&finished&&!reviewSetupExpanded);
 if(finished||suppressCompletedReview&&task.status==="completed")$("#reviewProgress").replaceChildren();else JobProgress.show("#reviewProgress",task);
}
async function loadReview(){const sequence=++searchSequence;const value=await api("/reviews/results",{id:currentReview,...reviewFilters()});if(sequence!==searchSequence||page!=="review"||value.task.id!==currentReview)return;await window.ReviewNotesUI.load(currentReview);if(sequence!==searchSequence||page!=="review"||value.task.id!==currentReview)return;reviewData=value;reviewSignature=value.task.updated_at+"/"+value.task.status;window.ToolWorkspace?.adoptReview(value.task);renderReviewHeader(value);
 $("#reviewSummary").textContent=`${value.total} / ${value.all_total} 位`;
 const patients=$("#reviewPatients");patients.replaceChildren();if(!value.patients.some(p=>p.mrn===reviewPatient))reviewPatient=value.patients[0]?.mrn||"";
 for(const p of value.patients){const b=btn("",()=>selectReviewPatient(p.mrn),"patient-select"+(p.mrn===reviewPatient?" active":""));b.dataset.mrn=p.mrn;const heading=node("span",undefined,"review-patient-top"),sequenceNo=window.ReviewNotesUI.sequence(p.mrn);const sequenceBadge=node("span",sequenceNo||"未提供","review-sequence");sequenceBadge.title="掛號序號："+(sequenceNo||"未提供");heading.append(node("strong",p.name||p.mrn),sequenceBadge);b.append(heading,node("span",`${p.mrn} · ${labels[p.status]||p.status}`,"caption"));if(p.records?.length)b.append(node("span",p.records.map(r=>r.date).filter((v,i,a)=>a.indexOf(v)===i).join("、"),"caption"));const tags=new Map((p.records||[]).flatMap(r=>r.matches||[]).map(t=>[t.category,t]));b.append(badges([...tags.values()]),badges(p.manual_tags,true));patients.append(b);}
 if(!value.patients.length)patients.append(empty("沒有符合的病人。"));$("#reviewSummary").textContent=`${value.total} / ${value.all_total} 位`+(value.scope_issue_count?` · ${value.scope_issue_count} 份病歷的 tag 判定不完整`:"");renderReviewDetail();
}
function soapNode(record){return SOAPView.create(record);}
function patientAge(p){
 const birthday=String(p.birthday||p.records?.find(r=>r.birthday)?.birthday||"").trim();
 const parts=birthday.match(/^(\d{4})-(\d{2})-(\d{2})$/);
 if(parts){
  const [year,month,day]=parts.slice(1).map(Number),date=new Date(Date.UTC(year,month-1,day));
  const todayParts=Object.fromEntries(new Intl.DateTimeFormat("en",{timeZone:"Asia/Taipei",year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(new Date()).map(part=>[part.type,Number(part.value)]));
  const age=todayParts.year-year-(todayParts.month<month||todayParts.month===month&&todayParts.day<day?1:0);
  if(date.toISOString().slice(0,10)===birthday&&age>=0&&age<=130)return age+" 歲";
 }
 const supplied=String(p.age||p.records?.find(r=>r.age)?.age||"").trim();
 return supplied?(/^\d+$/.test(supplied)?supplied+" 歲":supplied):"未提供";
}
async function copyPatientMrn(mrn,button){
 try{if(!navigator.clipboard?.writeText)throw new Error("Clipboard API unavailable");await navigator.clipboard.writeText(mrn);say("已複製病歷號");}
 catch{const input=node("input");input.type="text";input.readOnly=true;input.value=mrn;input.setAttribute("aria-label","請手動複製病歷號");button.replaceWith(input);input.focus();input.select();say("無法自動複製，請按 Ctrl+C",true);}
}
async function selectReviewPatient(mrn){if(reviewPatient!==mrn&&!await window.ReviewNotesUI.flush(reviewPatient)){say("有備註尚未儲存，請重試後再切換病人。",true);return;}if(reviewPatient!==mrn)window.ScanBrowser?.reset();reviewPatient=mrn;renderReviewDetail();$("#reviewDetail").scrollTop=0;for(const n of $$(".patient-select"))n.classList.toggle("active",n.dataset.mrn===mrn);}
function renderReviewDetail(){
 const panel=$("#reviewDetail");panel.replaceChildren();
 const p=reviewData?.patients.find(patient=>patient.mrn===reviewPatient);
 if(!p){panel.append(empty("選擇左側病人檢閱 SOAP。"));return;}
 window.FileCompare?.context(account,p.mrn,p.name);
 const tagTargets=new Map(),searchTargets=[],recordSections=[];
 for(const record of p.records||[]){
  const section=node("section",undefined,"soap-record"),soap=soapNode(record);
  for(const target of soap.querySelectorAll("[data-tag-categories]")){
   for(const category of JSON.parse(target.dataset.tagCategories))if(!tagTargets.has(category))tagTargets.set(category,target);
  }
  section.append(node("h3",record.date+" · "+record.section),node("p",[record.doctor,record.case_no,record.cached?"使用本機版本":"已保存",time(record.updated_at)].filter(Boolean).join(" · "),"caption"));
  for(const mark of soap.querySelectorAll(".soap-original mark[data-search-hit], .soap-view > .soap mark[data-search-hit]")){
   searchTargets.push({mark,date:record.date,hit:mark.textContent.trim().replace(/\s+/g," ").slice(0,24)||"命中內容"});
  }
  section.append(soap);recordSections.push(section);
 }
 const name=node("div",undefined,"detail-meta"),demographics=node("div",undefined,"review-demographics");
 const mrn=btn("",()=>copyPatientMrn(p.mrn,mrn),"review-mrn-copy");mrn.textContent=p.mrn;mrn.title="點擊複製病歷號";mrn.setAttribute("aria-label","複製病歷號 "+p.mrn);
 demographics.append(mrn,node("span","年齡 "+patientAge(p),"muted"),node("span","性別 "+(p.sex||"未提供"),"muted"));
 const noteControl=window.ReviewNotesUI.control(p);
 name.append(node("h2",p.name||"姓名未提供"),demographics);panel.append(name);
 const controls=node("div",undefined,"review-controls"),shortcuts=node("div",undefined,"detail-shortcuts");
 shortcuts.append(btn("該次數值類報告",()=>window.ReviewHistoryUI.openCurrent("case_numeric",p.mrn,p.records)),
  btn("歷年數值類報告",()=>window.ReviewHistoryUI.open("numeric",p.mrn)),
  btn("該次醫囑清單",()=>window.ReviewHistoryUI.openCurrent("case_orders",p.mrn,p.records)),
  btn("歷年醫囑清單",()=>window.ReviewHistoryUI.open("orders",p.mrn)),
  btn("該次掃描病歷",()=>window.ReviewHistoryUI.openCurrent("case_scans",p.mrn,p.records)),
  btn("歷年掃描病歷",()=>window.ReviewHistoryUI.open("scans",p.mrn)),
  btn("歷次就診",()=>window.ReviewHistoryUI.open("visits",p.mrn)),
  btn("掛號紀錄",()=>extension("registrations",p.mrn)),btn("加入／移除 tag",()=>openPatientTags([p.mrn])),
  noteControl.button);
 const divider=node("span",undefined,"review-control-divider");divider.setAttribute("role","separator");divider.setAttribute("aria-orientation","vertical");
 const index=reviewData.patients.indexOf(p),nav=node("div",undefined,"detail-actions");
 for(const [label,d] of [["上一位",-1],["下一位",1]]){const next=reviewData.patients[index+d],button=btn(label,()=>selectReviewPatient(next.mrn));button.disabled=!next;nav.append(button);}
 controls.append(shortcuts,divider,nav);panel.append(controls,noteControl.panel);
 const autoTags=new Map(),summary=node("div",undefined,"review-tag-summary");
 for(const record of p.records||[])for(const tag of record.matches||[])if(!autoTags.has(tag.category))autoTags.set(tag.category,tag);
 if(autoTags.size){
  const group=node("div",undefined,"review-tag-group");group.append(node("strong","自動 TAG"));
  for(const tag of autoTags.values()){
   const target=tagTargets.get(tag.category),badge=btn("",()=>SOAPView.reveal(target),"badge tag-evidence");
   badge.append(node("span",tag.category_name||tag.category),node("small",tag.keyword,"tag-keyword"));
   badge.setAttribute("aria-label",`${tag.category_name||tag.category}：定位到 ${tag.keyword} 的 SOAP 依據`);
   badge.title=target?"跳到 SOAP 依據":"此 TAG 的依據尚無法定位";badge.disabled=!target;group.append(badge);
  }
  summary.append(group);
 }
 if(p.manual_tags?.length){const group=node("div",undefined,"review-tag-group");group.append(node("strong","手動 TAG"),badges(p.manual_tags));summary.append(group);}
 if(searchTargets.length){
  const group=node("div",undefined,"review-tag-group review-search-group");group.append(node("strong","搜尋命中"));
  const choices=node("span",undefined,"review-search-choices"),moreChoices=node("span",undefined,"review-search-choices");
  searchTargets.forEach(({mark,date,hit},index)=>{
   const label=`${date} · 第 ${index+1} 處 · ${hit}`,badge=btn(hit,()=>SOAPView.reveal(mark),"badge search-evidence");
   badge.title="跳到 "+label;badge.setAttribute("aria-label","跳到搜尋命中："+label);
   (index<6?choices:moreChoices).append(badge);
  });
  group.append(choices);
  if(searchTargets.length>6){const more=node("details",undefined,"review-search-more");more.append(node("summary",`顯示其餘 ${searchTargets.length-6} 處`),moreChoices);group.append(more);}
  summary.append(group);
 }
 if(summary.childElementCount)panel.append(summary);
 if(["error","partial","forbidden"].includes(p.status)&&!inProgress.has(reviewData.task.status))panel.append(btn("重試此病人",async()=>{await api("/tasks/start",{resume:currentReview,retry_only:[p.mrn]});await loadReview();}));
 if(p.message)panel.append(node("p",p.message,"attempts"));if(p.index_checked_at)panel.append(node("p","索引核對 "+time(p.index_checked_at),"caption"));
 if(p.attempts?.some(a=>a.status!=="ready")){const d=node("details",undefined,"attempts"),list=node("ul");d.append(node("summary","較新紀錄／未取得項目"));for(const a of p.attempts.filter(a=>a.status!=="ready"))list.append(node("li",`${a.date} · ${a.case_no} · ${a.status==="empty"?"SOAP 空白":labels[a.status]||a.status} ${a.message||""}`));d.append(list);panel.append(d);}
 panel.append(...recordSections);
 if(!recordSections.length)panel.append(empty(labels[p.status]||"尚未取得 SOAP"));
}
async function showDiagnostics(values){
 const result=await api("/reviews/diagnostics",values);$("#dataTitle").textContent="DEBUG 資訊";
 const content=$("#dataContent"),text=JSON.stringify(result,null,2);content.replaceChildren(node("p","SDK 互動摘要與錯誤資訊保存在目前資料庫；不含帳密或院方回應本文。","caption"));
 content.append(btn("匯出 DEBUG JSON",()=>{const url=URL.createObjectURL(new Blob([text],{type:"application/json;charset=utf-8"})),a=node("a");a.href=url;a.download="VGHKS-debug-"+(values.mrn||values.task_id||values.session_id)+".json";document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}));
 if(result.sdk_events?.length)content.append(node("h3","SDK 互動"),table(["時間","服務／動作","結果","錯誤"],result.sdk_events.map(event=>[time(event.occurred_at),event.service+"."+event.method,event.status==="ok"?"完成":"ERROR",[event.error_code,event.message].filter(Boolean).join(" · ")||"—"])));
 else content.append(empty("此紀錄尚無逐次 SDK 互動摘要；舊版紀錄可能只有任務與錯誤資訊。"));
 if(result.saved_attempts?.length)content.append(node("h3","未完成項目"),table(["病歷號／階段","狀態","訊息"],result.saved_attempts.map(item=>[item.mrn||item.stage||"—",item.code||item.status||"—",item.message||"—"])));
 if(result.items?.length){const details=node("details"),summary=node("summary",`錯誤細節（${result.total}）`);details.append(summary,node("pre",JSON.stringify(result.items,null,2)));content.append(details);}
 else if(!result.saved_attempts?.length&&!result.sdk_events?.some(event=>event.status==="error"))content.append(node("p","此紀錄沒有保存的 ERROR。","caption"));
 dialog("dataDialog");
}
async function extension(kind,mrn,recordId="",force=false){window.ReviewHistoryUI?.dismiss();extensionContext={kind,mrn,record_id:recordId};$("#dataTitle").textContent=kind==="numeric"?"本次數值報告":"掛號紀錄";$("#dataContent").replaceChildren(node("p","正在讀取…","muted"));dialog("dataDialog");setPatientDialogContext("dataDialog",mrn);const {data}=await api("/extensions/read",extensionContext);if(data)renderExtension(data,kind);if(!work.online){if(!data)$("#dataContent").replaceChildren(empty("沒有已保存資料，請登入後再查詢。"));return;}const task=await api("/tasks/start",{...extensionContext,force});extensionTask=task.task_id;await showExtensionTask(extensionTask,kind,false);}
async function showExtensionTask(id,kind,open=true){const task=await api("/tasks/detail?id="+encodeURIComponent(id));extensionTask=id;extensionContext={kind:kind||task.kind,mrn:task.mrn,record_id:task.record_id||""};if(open){$("#dataTitle").textContent=task.name;dialog("dataDialog");setPatientDialogContext("dataDialog",task.mrn);}if(task.items.length)renderExtension(task.items[0],kind||task.kind);else $("#dataContent").replaceChildren(node("p",task.message||labels[task.status],"muted"));$("#dataContent").prepend(JobProgress.create(task));if(!inProgress.has(task.status)){extensionTask="";if((kind||task.kind)==="registrations"&&page==="review"&&currentReview){await window.ReviewNotesUI.load(currentReview,{force:true});await loadReview();}}}
function appendNumericTables(c,tables){for(const t of tables){
  const rows=t.rows||[],paths=t.column_paths||[],legacy=t.headers||[],width=Math.max(0,...rows.map(row=>row.length));
  const aligned=paths.length===width&&rows.every(row=>row.length===width);
  const oldAligned=!paths.length&&!t.header_rows?.length&&legacy.length===width&&rows.every(row=>row.length===width);
  const headers=aligned?paths.map(path=>path.filter(Boolean).join(" / ")||"—"):oldAligned?legacy:Array.from({length:width},(_,i)=>"欄 "+(i+1));
  c.append(node("h3",t.title||t.name||"數值報告"),table(headers,rows));
  if(!aligned&&!oldAligned)c.append(node("p","表頭無法安全對應欄位，以下僅依原始欄位順序顯示。","caption"));
  if(t.header_rows?.length)c.append(node("p","來源表頭："+t.header_rows.map(parts=>parts.join(" / ")).join("；"),"caption"));
  if(t.parsing_issues?.length)c.append(node("p","來源解析提示："+t.parsing_issues.join("、"),"caption"));
 }if(!tables.length)c.append(empty("沒有可顯示的數值表。"));}
function renderExtension(data,kind){const c=$("#dataContent");c.replaceChildren(node("p",`${data.cached?"本機資料 · ":""}最後取得 ${time(data.updated_at)}`,"caption"));c.append(btn("更新資料",()=>extension(kind,extensionContext.mrn,extensionContext.record_id,true)));if(data.status==="deleted"){c.append(empty("資料已刪除。"));return;}
 if(kind==="numeric")appendNumericTables(c,data.payload?.tables||[]);
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
const tagFilters=()=>({q:$("#tagQuery").value,tag:$("#tagFilter").value,source:$("#tagSource").value});
async function loadTags(){const data=await api("/patient-tags/search",{...tagFilters(),offset:tagPage*40,limit:40});$("#tagSummary").textContent=data.total+" 位病人 · 相同病歷號合併";$("#tagPatients").replaceChildren(table(["病人／病歷號","SOAP 自動 tag","手動 tag","操作"],data.patients.map(p=>[patientName(p),badges(p.auto_tags),badges(p.manual_tags,true),btn("編輯 tag",()=>openPatientTags([p.mrn]))])));paginate($("#tagPagination"),tagPage,data.total,async n=>{tagPage=n;await loadTags();});}
function openPatientTags(mrns=[]){const unique=[...new Set(mrns)];$("#tagMRNs").value=unique.join("\n");$("#newTagName").value="";$("#tagChoices").replaceChildren(...(work.categories||[]).map(t=>{const label=node("label",undefined,"check"),input=check("tag "+t.name,false,()=>{});input.value=t.id;label.append(input,node("span",t.name));return label;}));setPatientDialogContext("patientTagDialog",unique.length===1?unique[0]:"");dialog("patientTagDialog");}
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
function openRules(){window.ToolWorkspace?.beginSettings();$("#ruleTags").replaceChildren(...work.categories.map(tagRuleRow));$("#tagSettingsSummary").textContent=work.categories.map(t=>t.name).join("、");dialog("rulesDialog");}


$("#loginAccount").addEventListener("change",()=>loginFields($("#loginAccount").value));
$("#loginForm").addEventListener("submit",e=>{e.preventDefault();login($("#loginAccount").value);});
$("#offlineButton").addEventListener("click",()=>act(async()=>{const key=$("#loginAccount").value;await ra("/accounts/offline",{id:key});await enter(key);}));
$("#forgetPassword").addEventListener("click",()=>act(async()=>{const id=$("#loginAccount").value;await ra("/accounts/forget",{id});root=await ra("/bootstrap");renderAccounts();loginFields(id);say("已移除資料庫保存的密碼。");}));
$("#accountSwitch").addEventListener("change",()=>act(async()=>{const key=$("#accountSwitch").value,previous=account;try{if(!await window.ReviewNotesUI.flushAll())throw new Error("有備註尚未儲存，請重試後再切換帳號。");if(key==="__new"){showEntry();renderAccounts();return;}if(key===account)return;if(!root.read_only)await api("/draft/save",draftValue());const result=await ra("/accounts/activate",{id:key});if(result.online||result.status==="offline"){root=await ra("/bootstrap");renderAccounts();await enter(key);say(result.online?"已切換帳號，原帳號的連線仍保留。":"已切換至離線檢閱。");}else if(result.status==="needs_password")openReconnect("",key,"此帳號需要密碼才能連線。"+(result.message||""));else say("目標帳號暫時無法連線，請稍後重試。"+(result.message||""),true);}finally{if(account===previous)$("#accountSwitch").value=previous;}}));
$("#sidebarAccount").addEventListener("toggle",()=>{if($("#sidebarAccount").open)act(async()=>{root=await ra("/bootstrap");renderAccounts();});});
for(const b of $$("[data-page]"))b.addEventListener("click",()=>act(()=>navigate(b.dataset.page)));
document.addEventListener("click",event=>{const panel=$("#sidebarAccount");if(panel.open&&!panel.contains(event.target))panel.open=false;});
$("#reviewPatientsToggle").addEventListener("click",toggleReviewPatients);
$("#reviewInfoToggle").addEventListener("click",()=>{reviewHeaderExpanded=!reviewHeaderExpanded;if(reviewData)renderReviewHeader(reviewData);});
$("#reviewChangeSetup").addEventListener("click",()=>{reviewSetupExpanded=true;document.body.classList.remove("review-result-compact");if($("#toolEditSetup").getAttribute("aria-expanded")!=="true")$("#toolEditSetup").click();$("#toolSourcePanel").scrollIntoView({block:"start"});});
for(const b of $$("[data-close]"))b.addEventListener("click",()=>$("#"+b.dataset.close).close());
$("#logout").addEventListener("click",()=>act(async()=>{if(!await window.ReviewNotesUI.flushAll())throw new Error("有備註尚未儲存，請重試後再登出。");const key=account;clearTimeout(draftTimer);if(!root.read_only){await api("/draft/save",draftValue());await ra("/accounts/logout",{id:key});}showEntry(key);}));
$("#reconnect").addEventListener("click",()=>openReconnect());
$("#reconnectForm").addEventListener("submit",event=>{event.preventDefault();const button=event.submitter;button.disabled=true;$("#reconnectError").hidden=true;const affected=reconnectTask,target=reconnectTarget||account;const password=$("#reconnectPassword").value;
 (async()=>{try{
  await ra("/accounts/login",{id:target,password,remember:$("#reconnectRemember").checked});
  reconnectAttempts.delete(target);root=await ra("/bootstrap");renderAccounts();$("#reconnectDialog").close();$("#reconnectPassword").value="";reconnectTask="";reconnectTarget="";
  if(target!==account){if(!root.read_only)await api("/draft/save",draftValue());await enter(target);say("已切換並連線至所選帳號。");return;}
  await refresh();
  const task=work.tasks.find(item=>item.id===affected);
  if(task&&["paused","failed"].includes(task.status))await api("/tasks/start",{resume:affected});
  await refresh();say("院內系統已重新連線。已保存的畫面與任務保留。");
 }catch(error){$("#reconnectError").textContent=error.message||"重新連線失敗。";$("#reconnectError").hidden=false;}
 finally{button.disabled=false;}})();
});
$("#exit").addEventListener("click",()=>act(async()=>{if(!await window.ReviewNotesUI.flushAll())throw new Error("有備註尚未儲存，請重試後再結束程式。");clearTimeout(draftTimer);if(!root.read_only)await api("/draft/save",draftValue());await ra("/shutdown",{});stopped=true;document.body.replaceChildren(empty("VGHKS-bot 已結束，資料與任務進度已保留。"));}));
$("#dateMode").addEventListener("change",dateMode);$("#startDate").addEventListener("change",dateMode);
$("#dateForm").addEventListener("submit",e=>{e.preventDefault();act(()=>browse());});$("#refreshList").addEventListener("click",()=>act(()=>browse(true)));
for(const b of $$("[data-day]"))b.addEventListener("click",()=>act(async()=>{$("#dateMode").value="single";$("#startDate").value=relativeDay(root.today,Number(b.dataset.day));await browse();}));
for(const [id,n] of [["previousDay",-1],["nextDay",1]])$("#"+id).addEventListener("click",()=>act(async()=>{$("#dateMode").value="single";$("#startDate").value=relativeDay($("#startDate").value,n);await browse();}));
for(const [id,tab] of [["ownTab","own"],["sharedTab","shared"]])$("#"+id).addEventListener("click",()=>{listTab=tab;renderList();draftSave();});
$("#selectAll").addEventListener("change",e=>{for(const r of visibleRows()){if(e.target.checked)selected.add(r.id);else selected.delete(r.id);}renderList();draftSave();});
$("#listQuery").addEventListener("input",renderList);
const manualErrors=new Map();
function renderManualErrors(){
 const area=$("#manualLookupErrors");
 area.replaceChildren(...[...manualErrors].map(([key,error])=>{
  const row=node("div",undefined,"patient-lookup-error");
  row.append(node("span",error.token+"："+error.message),btn("重試",()=>{
   manualErrors.delete(key);renderManualErrors();manualLookup.add([error.token],error.mode);
  },"quiet"));
  return row;
 }));
}
const manualLookup=window.PatientTokens.create({
 input:"identifiers",kind:()=>$("#identifierKind").value,request:api,
 canLookup:()=>!!account&&!!work?.online&&!root.read_only,status:"resolveStatus",
 onResolved:(patient,mode)=>{manualErrors.delete(mode+":"+(patient.input||patient.mrn));renderManualErrors();addManualPatient(patient);},
 onFailed:(token,message,mode)=>{manualErrors.set(mode+":"+token,{token,message,mode});renderManualErrors();},
});
$("#manualOpen").addEventListener("click",()=>{dialog("manualDialog");renderManualChips();manualLookup.focus();});
$("#saveCollection").addEventListener("click",()=>act(async()=>{await saveCurrentSet();await refresh();say("病人集合已保存。");}));
$("#useTools").addEventListener("click",()=>act(async()=>{const group=await saveCurrentSet();await refresh();openTools(group);}));
$("#selectedTags").addEventListener("click",()=>openPatientTags(selectedMRNs()));
for(const id of ["toolKind","soapMode","forceReview"])$("#"+id).addEventListener("change",toolOptions);
$("#refreshTasks").addEventListener("click",()=>act(refresh));$("#moduleBack").addEventListener("click",()=>navigate("tasks"));
$("#reviewSearchForm").addEventListener("submit",e=>{e.preventDefault();act(loadReview);});$("#reviewTag").addEventListener("change",()=>act(loadReview));
$("#libraryForm").addEventListener("submit",e=>{e.preventDefault();libraryPage=0;librarySelected.clear();act(loadLibrary);});
$("#libraryToTools").addEventListener("click",()=>act(async()=>openTools(await api("/sets/save",{source:"library",filters:libraryFilters(),all:true}))));
$("#libraryToTools").before(btn("勾選病人 → 工具",async()=>{if(!librarySelected.size)throw new Error("請先勾選病歷。");openTools(await api("/sets/save",{source:"library",selected:[...librarySelected]}));}));
$("#deleteRecords").addEventListener("click",()=>act(async()=>{if(!librarySelected.size)throw new Error("請先勾選病歷。");if(await confirmDelete(`刪除 ${librarySelected.size} 筆病歷的所有版本與本次檢閱副本？手動 tag 保留。`)){await api("/library/delete",{ids:[...librarySelected]});librarySelected.clear();await loadLibrary();say("病歷已刪除。");}}));
$("#tagSearchForm").addEventListener("submit",e=>{e.preventDefault();tagPage=0;act(loadTags);});
$("#tagsToTools").addEventListener("click",()=>act(async()=>openTools(await api("/sets/save",{source:"tags",filters:tagFilters(),all:true}))));
$("#tagManualOpen").addEventListener("click",()=>openPatientTags());
$("#patientTagForm").addEventListener("submit",e=>{e.preventDefault();const operation=e.submitter?.value||"add";act(async()=>{await api("/patient-tags/update",{mrns:$("#tagMRNs").value,tag_ids:$$('input:checked',$("#tagChoices")).map(n=>n.value),new_tag:$("#newTagName").value,operation});$("#patientTagDialog").close();reviewSignature="";await refresh();if(page==="tags")await loadTags();if(page==="library")await loadLibrary();say("手動 tag 已更新。");});});
$("#addRuleTag").addEventListener("click",()=>{$("#ruleTags").append(tagRuleRow());Choices.sync();});
$("#rulesForm").addEventListener("submit",e=>{e.preventDefault();act(async()=>{
 const categories=$$(".rule-row").map(r=>({id:r.dataset.id,name:r.querySelector('[name="tag-name"]').value,keywords:r.querySelector("textarea").value.split(/\r?\n/).filter(s=>s.trim()),parser:r.dataset.parser,scope:r.querySelector('input[type="radio"]:checked').value}));
 const previous=(work?.categories||[]).map(({id,name,keywords,parser,scope})=>({id,name,keywords,parser,scope}));
 const categoriesChanged=JSON.stringify(categories)!==JSON.stringify(previous);
 await api("/reviews/preferences",{review_options:window.ToolWorkspace.queryOptions()});
 await api("/settings",{categories});
 const task=work?.tasks.find(t=>t.id===currentReview);
 if(categoriesChanged&&task&&!inProgress.has(task.status))await api("/reviews/reclassify",{id:currentReview});
 window.ToolWorkspace?.commitSettings();$("#rulesDialog").close();reviewSignature="";await refresh();
 say("檢閱設定已保存。");
});});
$("#rulesDialog").addEventListener("close",()=>window.ToolWorkspace?.cancelSettings());
$("#rulesForm").addEventListener("invalid",e=>{for(let p=e.target.parentElement;p&&p!==e.currentTarget;p=p.parentElement)if(p.tagName==="DETAILS")p.open=true;},true);
for(const button of $$("button[data-icon]"))setIcon(button,button.dataset.icon,button.dataset.iconLabel||button.textContent,button.dataset.iconText!=="true");
for(const [id,name,label] of [["refreshTasks","refresh","更新爬蟲紀錄"],["refreshList","refresh","更新門診清單"],["previousDay","left","前一天"],["nextDay","right","後一天"]])setIcon($("#"+id),name,label);
$("#accountSettings").addEventListener("click",()=>{$("#settingsAccount").textContent=(info()?.label||info()?.username)+" · "+info()?.campus;$("#concurrency").value=String(root.concurrency);dialog("settingsDialog");});
$("#editLogin").addEventListener("click",()=>showEntry(account));
$("#saveConcurrency").addEventListener("click",()=>act(async()=>{const r=await ra("/concurrency",{value:Number($("#concurrency").value)});root.concurrency=r.concurrency;$("#settingsDialog").close();say("連線設定已保存。");}));
window.addEventListener("message",e=>{if(e.origin!==location.origin||e.source!==$("#moduleFrame").contentWindow)return;if(e.data?.type==="bot:task-started")act(refresh);else if(page==="module"&&e.data?.type==="bot:compare-open")window.FileCompare?.open();else if(page==="module")window.FileCompare?.receive(e,$("#moduleFrame"),account);});
async function poll(){if(stopped)return;if(account&&!pollBusy){pollBusy=true;try{await refresh();await maybeRecoverConnection();if(page==="approvals"){await window.Approvals?.poll();}if(page==="earnings")await window.Earnings?.poll();if(page==="surgery")await window.SurgerySystem?.poll();applyReadonly();}catch(e){if(e.name!=="AbortError")say(e.message,true);}finally{pollBusy=false;}}if(!stopped)setTimeout(poll,2000);}
async function initialize(){const token=location.hash.slice(1);if(/^[A-Za-z0-9_-]{43}$/.test(token)){await ra("/session",{token});history.replaceState(null,"",location.pathname);}root=await ra("/bootstrap");$("#version").textContent=$("#sidebarVersion").textContent="v"+root.version;renderAccounts();loginFields(root.last_account||"");if(root.read_only&&root.accounts.length){const key=root.last_account||root.accounts[0].id;await ra("/accounts/offline",{id:key});await enter(key);}else if(root.accounts.find(a=>a.id===root.last_account)?.remembered)await login(root.last_account,true);applyReadonly();if(root.database_notice){say(root.database_notice);await window.DatabaseUI.open();}poll();}
initialize().catch(e=>say(e.message,true));
