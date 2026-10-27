"use strict";
// Exercise asynchronous saves/deletes and hierarchy navigation using local data.
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const deferred=()=>{let resolve,reject;const promise=new Promise((done,fail)=>{resolve=done;reject=fail;});return {promise,resolve,reject};};
const tick=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
function surface(){
 const nodes=new Map();let context;
 class Element{
  constructor(tag="div"){this.tagName=tag;this.children=[];this.dataset={};this.value="";this.textContent="";this.disabled=false;this.hidden=false;this.scrollTop=0;this.scrollLeft=0;this.style={};this.events=new Map();this.className="";this.parentElement=null;
   this.classList={add:name=>{this.className+=" "+name;},toggle(){}};}
  set id(value){this._id=value;nodes.set("#"+value,this);}get id(){return this._id;}
  get firstElementChild(){return this.children[0]||null;}
  get isConnected(){return this.parentElement?this.parentElement.isConnected:[...nodes.values()].includes(this);}
  append(...values){for(const child of values){child.parentElement=this;this.children.push(child);}}
  prepend(...values){for(const child of values)child.parentElement=this;this.children.unshift(...values);}
  insertBefore(child,anchor){if(child.parentElement)child.remove();const index=anchor?this.children.indexOf(anchor):this.children.length;child.parentElement=this;this.children.splice(index,0,child);}
  remove(){if(this.parentElement){this.parentElement.children=this.parentElement.children.filter(child=>child!==this);this.parentElement=null;}}
  before(...values){if(this.parentElement){const index=this.parentElement.children.indexOf(this);for(const child of values)child.parentElement=this.parentElement;this.parentElement.children.splice(index,0,...values);}}
  replaceChildren(...values){for(const child of this.children)child.parentElement=null;this.children=[];this.append(...values);}
  all(){return this.children.flatMap(child=>[child,...child.all()]);}
  contains(value){return value===this||this.all().includes(value);}
  matches(selector){if(selector.startsWith("#"))return this.id===selector.slice(1);if(selector.startsWith("."))return this.className.split(/\s+/).includes(selector.slice(1));if(/^\[data-[\w-]+\]$/.test(selector))return selector.slice(6,-1).replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase()) in this.dataset;return this.tagName===selector;}
  querySelectorAll(selector){return this.all().filter(child=>selector.split(",").some(part=>child.matches(part)));}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  addEventListener(name,run){this.events.set(name,[...(this.events.get(name)||[]),run]);}
  emit(name){if(name==="click")context.document.activeElement=this;return Promise.all((this.events.get(name)||[]).map(run=>run({preventDefault(){},stopPropagation(){},target:this})));}
  setAttribute(name,value){this[name]=value;}focus(){context.document.activeElement=this;}
  removeAttribute(name){delete this[name];}scrollIntoView(){}
 }
 const $=selector=>{if(!nodes.has(selector))nodes.set(selector,new Element());return nodes.get(selector);};
 const node=(tag,text,cls)=>{const element=new Element(tag);if(text!==undefined)element.textContent=text;if(cls)element.className=cls;return element;};
 context={$,$$:(selector,parent)=>parent?parent.querySelectorAll(selector):[...new Set([...nodes.values()].flatMap(element=>[element,...element.all()]))].filter(element=>selector.split(",").some(part=>element.matches(part))),node,
  btn:(label,run,cls)=>{const element=node("button",label,cls);element.run=run;return element;},
  document:{activeElement:null},window:{},root:{read_only:false},account:"ACCOUNT",currentReview:"R1",reviewPatient:"P1",
  reviewData:{task:{id:"R1",name:"合成檢閱",created_at:"2026-10-01"},patients:[{mrn:"P1"}]},work:{tasks:[]},
  inProgress:new Set(["queued","running","cancelling"]),labels:{completed:"完成",running:"執行中"},
  time:value=>value||"",setIcon(){},Choices:{sync(){}},JobProgress:{create:()=>new Element()},act:run=>run(),empty:text=>node("p",text),
  openReview(){},showPage(){},showExtensionTask(){},showTaskSummary(){},showDiagnostics(){},refresh(){},say(){},
  table:()=>new Element("table"),dialog(){},paginate(){},URLSearchParams,DOMException,setTimeout,clearTimeout};
 vm.createContext(context);
 return {context,$,node,run:path=>vm.runInContext(fs.readFileSync(path,"utf8"),context)};
}
async function noteTests(){
 const s=surface(),c=s.context,$=s.$;
 const saved=new Map([["P1","first note"],["P2","second note"]]),calls=[],confirmations=[];
 let confirmation=true,saveGate=null,clearGate=null,saveFailure=false;
 c.act=async run=>{const control=c.document.activeElement,wasDisabled=control?.disabled;if(control)control.disabled=true;try{return await run();}finally{if(control)control.disabled=wasDisabled;}};
 c.api=async(path,values)=>{
  calls.push({path,values});
  if(path.endsWith("/summary"))return {task_id:values.task_id,note_count:[...saved.values()].filter(text=>text.trim()).length};
  if(path.endsWith("/read")||path.endsWith("/page"))return {task_id:values.task_id,patients:(values.mrns||["P1","P2"]).map(mrn=>({mrn,text:saved.get(mrn)||""}))};
  if(path.endsWith("/save")){if(saveFailure)throw new Error("synthetic save failure");if(saveGate)await saveGate.promise;saved.set(values.mrn,values.text);return {updated_at:"saved"};}
  if(path.endsWith("/clear")){if(clearGate)await clearGate.promise;const count=saved.size;saved.clear();return {task_id:values.task_id,cleared_count:count};}
  throw new Error(path);
 };
 c.confirmDelete=async text=>{confirmations.push(text);return typeof confirmation==="boolean"?confirmation:confirmation.promise;};
 s.run("vghks_bot/static/review-notes.js");const ui=c.window.ReviewNotesUI;
 c.renderReviewDetail=()=>{$("#reviewDetail").replaceChildren(ui.control({mrn:c.reviewPatient}).panel);};
 await ui.load("R1");c.renderReviewDetail();
 await ui.openPrint();assert.match($("#reviewNotesContext").textContent,/2 位/,"export omitted filtered-out patient");
 const input=$("#reviewNoteInput");input.value="pending edit";input.emit("input");
 saveGate=deferred();const save=ui.flush("P1");
 confirmation=deferred();const clearing=ui.clearAll();assert.equal(ui.clearAll(),clearing,"double click issued two deletes");
 assert.equal(input.disabled,true);assert.equal(calls.filter(call=>call.path.endsWith("/clear")).length,0);
 saveGate.resolve();await save;await tick();assert.equal(confirmations.length,1);assert.match(confirmations[0],/全部 2 位/);
 confirmation.resolve(false);assert.equal(await clearing,false);assert.equal(saved.size,2);assert.equal(input.disabled,false);
 assert.equal(calls.filter(call=>call.path.endsWith("/clear")).length,0,"cancel deleted notes");
 saveGate=null;confirmation=true;clearGate=deferred();const confirmed=ui.clearAll();await tick();
 assert.equal($("#reviewClearNotes").disabled,true);clearGate.resolve();assert.equal(await confirmed,true);
 assert.equal(saved.size,0,"filtered-out patient's note survived clear");assert.equal($("#reviewNoteInput").value,"");
 assert.equal(ui.control({mrn:"P2"}).panel.children[0].children[0].value,"");
 const savesAfterClear=calls.filter(call=>call.path.endsWith("/save")).length;await ui.flushAll();await tick();
 assert.equal(calls.filter(call=>call.path.endsWith("/save")).length,savesAfterClear,"pending autosave resurrected notes");
 assert.equal($("#reviewNotesPrint").children.length,0);assert.equal($("#reviewNotesPrintButton").disabled,true);

 saved.set("P1","button clear test");clearGate=null;await ui.load("R1",{force:true});c.renderReviewDetail();
 await $("#reviewClearNotes").emit("click");
 assert.equal($("#reviewClearNotes").disabled,true,"shared action wrapper re-enabled clear on an empty task");

 saved.set("P1","retain after failure");await ui.load("R1",{force:true});c.renderReviewDetail();
 clearGate=deferred();const failing=ui.clearAll();await tick();clearGate.reject(new Error("synthetic delete failure"));
 await assert.rejects(failing,/delete failure/);assert.equal(saved.get("P1"),"retain after failure");
 assert.equal($("#reviewNoteInput").value,"retain after failure");assert.equal($("#reviewClearNotes").disabled,false);

 const dirty=$("#reviewNoteInput");dirty.value="unsaved";dirty.emit("input");saveFailure=true;
 const count=confirmations.length;await assert.rejects(ui.clearAll(),/尚未儲存/);assert.equal(confirmations.length,count);
 assert.equal(saved.get("P1"),"retain after failure");saveFailure=false;await ui.flushAll();
 confirmation=deferred();const switching=ui.clearAll();await tick();c.currentReview="R2";ui.reset();confirmation.resolve(true);
 await assert.rejects(switching,error=>error.name==="AbortError");assert.equal(saved.get("P1"),"unsaved");
 c.root.read_only=true;await assert.rejects(ui.clearAll(),/唯讀/);ui.reset();
}
async function activityTests(){
 const s=surface(),c=s.context,$=s.$;s.run("vghks_bot/static/task-activity.js");const ui=c.window.TaskActivityUI;
 const parent={id:"R1",bot:true,kind:"review",name:"2026-10-01 病人集合",status:"completed",created_at:"2026-10-01",account_id:"A",note_count:2,members:[{mrn:"P1",name:"合成甲"},{mrn:"P2",name:"合成乙"}]};
 const other={...parent,id:"R2",created_at:"2026-10-02"};
 const report={id:"C1",bot:true,kind:"history",name:"醫囑報告內容",resource:"order_report",mrn:"P1",review_task_id:"R1",account_id:"A",status:"running",created_at:"2026-10-03"};
 const nextReport={...report,id:"C2",review_task_id:"R2",mrn:"P2"};
 const orphan={...report,id:"C3",review_task_id:undefined};
 const wrongMember={...report,id:"C4",mrn:"P9"},wrongAccount={...report,id:"C5",account_id:"B"};
 const session={id:"S1",session:true,kind:"sdk",status:"completed",created_at:"2026-10-01"};
 const tasks=[parent,other,report,nextReport,orphan,wrongMember,wrongAccount,session];
 const model=ui.groupTasks(tasks);assert.equal(model.totalGroups,6);
 assert.deepEqual([...model.groups.find(group=>group.task.id==="R1").allChildren].map(task=>task.id),["C1"]);
 assert.deepEqual([...model.groups.find(group=>group.task.id==="R2").allChildren].map(task=>task.id),["C2"]);
 assert.equal(ui.groupTasks(tasks,{system:"SDK"}).groups[0].task.id,"S1");
 const search=ui.groupTasks(tasks,{query:"p2"});assert.equal(search.groups.length,2);
 assert.equal(search.groups.find(group=>group.task.id==="R1").children.length,0,"patient without subtask should still find its parent");
 assert.equal(search.groups.find(group=>group.task.id==="R2").children[0].id,"C2");
 assert.equal(parent.status,"completed","child running changed parent's completion state");
 c.work={tasks:[parent,other,report,nextReport,orphan],sdk_sessions:[session]};ui.render({runs:[]});
 const findRoot=()=>$("#taskList").children.find(element=>element.dataset.activityKey==="bot:R1");
 let root=findRoot();assert.equal(root.open,false);root.open=true;root.emit("toggle");
 let patient=root.querySelectorAll(".activity-patient")[0];patient.open=true;patient.emit("toggle");
 let debug=root.querySelectorAll("[data-activity-focus]").find(element=>element.dataset.activityFocus==="bot:C1:debug");debug.focus();
 report.status="completed";report.updated_at="2026-10-04";ui.render();root=findRoot();
 assert.equal(root.open,true,"polling collapsed parent");assert.equal(root.querySelectorAll(".activity-patient")[0].open,true,"polling collapsed patient");
 assert.equal(c.document.activeElement.dataset.activityFocus,"bot:C1:debug","polling lost action focus");
 $("#taskSystem").value="SDK";ui.render();assert.equal($("#taskList").children.length,1);
 c.api=async()=>({tasks:[parent,report],total:1,total_groups:6});
 await ui.reveal("C1");assert.equal($("#taskSystem").value,"");assert.equal(findRoot().open,true);
 ui.reset();$("#taskQuery").value="order_report";ui.render({runs:[]});root=findRoot();
 assert.equal(root.open,true);assert.equal(root.querySelectorAll(".activity-patient")[0].open,true);
 root.open=false;root.emit("toggle");report.updated_at="2026-10-05";ui.render();assert.equal(findRoot().open,false,"polling overrode explicit collapse during search");
 ui.reset();ui.render({runs:[]});assert.equal(findRoot().open,false,"account reset retained old disclosure state");
 const profile={...report,id:"PROFILE1",kind:"profile",name:"病人基本資料",status:"completed",created_at:"2026-10-06"};
 const profileGroups=ui.groupTasks([parent,profile]);assert.equal(profileGroups.totalGroups,1,"profile query did not join its review family");
 assert.deepEqual([...profileGroups.groups[0].allChildren].map(task=>task.id),["PROFILE1"]);
 const openedProfiles=[];c.showExtensionTask=async(...values)=>openedProfiles.push(values);c.work={tasks:[parent,profile]};ui.reset();ui.render({runs:[]});
 const profileRoot=findRoot();profileRoot.open=true;await profileRoot.emit("toggle");
 const profilePatient=profileRoot.querySelector(".activity-patient");profilePatient.open=true;await profilePatient.emit("toggle");
 const profileView=profileRoot.querySelectorAll("[data-activity-focus]").find(element=>element.dataset.activityFocus==="bot:PROFILE1:view");
 assert.ok(profileView,"profile subtask did not expose its saved data action");await profileView.run();
 assert.deepEqual(openedProfiles,[["PROFILE1","profile"]]);
}
async function externalNoteTests(){
 const s=surface(),c=s.context,$=s.$,calls=[];let serverText="first",saveFailure=false;
 c.work.revisions={review_note:0};c.setIcon=(button,name,label)=>{button.iconLabel=label;};
 c.api=async(path,values)=>{
  calls.push({path,values});
  if(path.endsWith("/page"))return {task_id:values.task_id,patients:values.mrns.map(mrn=>({mrn,text:serverText,sequence_no:"001"}))};
  if(path.endsWith("/summary"))return {task_id:values.task_id,note_count:serverText.trim()?1:0};
  if(path.endsWith("/save")){if(saveFailure)throw new Error("synthetic save failure");serverText=values.text;return {updated_at:"saved"};}
  if(path.endsWith("/clear")){serverText="";return {task_id:values.task_id,cleared_count:1};}
  throw new Error(path);
 };
 c.confirmDelete=async()=>true;c.renderReviewDetail=()=>{throw new Error("note-only update rebuilt the SOAP reader");};
 s.run("vghks_bot/static/review-notes.js");const ui=c.window.ReviewNotesUI;await ui.load("R1",{mrns:["P1"]});
 const soap=s.node("section","unchanged SOAP","soap-record"),control=ui.control({mrn:"P1"});$("#reviewDetail").append(soap,control.button,control.panel);
 await control.button.emit("click");const input=$("#reviewNoteInput");input.selectionStart=input.selectionEnd=2;input.setSelectionRange=(start,end)=>{input.selectionStart=start;input.selectionEnd=end;};input.focus();
 serverText="remote edit";c.work.revisions.review_note++;await ui.load("R1",{mrns:["P1"]});
 assert.equal($("#reviewDetail").querySelector(".soap-record"),soap);assert.equal($("#reviewNoteInput"),input);assert.equal(input.value,"remote edit");assert.equal(c.document.activeElement,input);assert.equal(input.selectionStart,2);assert.equal(control.button.iconLabel,"編輯病人備註");
 input.value="local edit after refresh";await input.emit("input");serverText="another remote edit";c.work.revisions.review_note++;await ui.load("R1",{mrns:["P1"]});
 assert.equal(input.value,"local edit after refresh","external note replaced unsaved text");await ui.flush("P1");assert.equal(serverText,"local edit after refresh","visible input handler still referenced an abandoned note object");
 saveFailure=true;input.value="must preserve";await input.emit("input");const before=calls.filter(call=>call.path.endsWith("/page")).length;
 await ui.load("R1",{mrns:["P1"],force:true});assert.equal(calls.filter(call=>call.path.endsWith("/page")).length,before);assert.equal(input.value,"must preserve");
 await assert.rejects(ui.clearAll(),/備註尚未儲存/);assert.equal(calls.filter(call=>call.path.endsWith("/clear")).length,0);
 saveFailure=false;await ui.flush("P1");await ui.clearAll();assert.equal(input.value,"");assert.equal(control.button.iconLabel,"新增病人備註");assert.equal($("#reviewDetail").querySelector(".soap-record"),soap);
 ui.reset();
}
async function profileTests(){
 const s=surface(),c=s.context,$=s.$,calls=[],noteLoads=[],reviewLoads=[],copied=[];
 const source=fs.readFileSync("vghks_bot/static/bot.js","utf8");
 c.Node=s.node("div").constructor;c.epoch=0;c.page="review";c.extensionContext=null;c.extensionTask="";c.work.online=true;
 c.document.createElement=tag=>s.node(tag);c.window.addEventListener=()=>{};
 c.navigator={clipboard:{writeText:async value=>copied.push(value)}};
 s.run("vghks_bot/static/clinical-ui.js");c.ClinicalUI=c.window.ClinicalUI;
 c.window.ReviewNotesUI={control:()=>({button:s.node("button","備註"),panel:s.node("div")}),load:async(...values)=>noteLoads.push(values)};
 $("#dataDialog").showModal=()=>{$("#dataDialog").open=true;};$("#dataDialog").close=()=>{$("#dataDialog").open=false;return $("#dataDialog").emit("close");};
 c.setPatientDialogContext=(id,mrn)=>{$("#"+id).dataset.mrn=mrn;};
 c.JobProgress.publish=()=>{};c.loadReview=async()=>reviewLoads.push(c.currentReview);
 vm.runInContext(source.match(/function dialog\(id\)\{[^\n]+/)[0],c);
 vm.runInContext(source.split("\n").find(line=>line.startsWith('$("#dataDialog").addEventListener("close",')),c);
 vm.runInContext(source.match(/function table\(headers,rows\)\{[^\n]+/)[0],c);
 vm.runInContext(source.slice(source.indexOf("function patientContactRows("),source.indexOf("async function showDiagnostics(")),c);
 vm.runInContext(source.slice(source.indexOf("async function extension("),source.indexOf("const eyeNumericPatterns=")),c);
 vm.runInContext(source.slice(source.indexOf("function renderExtension("),source.indexOf("const libraryFilters=")),c);
 const compact={mrn:"P1",name:"合成病人",mobile_phone:" 0912000000 ",home_phone:"07-0000000",phone:"0912000000",sex:"女",age:"68",updated_at:"compact-saved",cached:true};
 const saved={...compact,national_id:"A123456789",phone:"07-1111111",contact:"合成聯絡人",basic_info_at:"basic-saved",updated_at:"recent-local-save"};
 const profiles=new Map([["P1",compact],["P2",{...saved,mrn:"P2",name:"另一位合成病人",national_id:"B123456789"}]]),tasks=new Map(),readGates=new Map(),detailGates=new Map(),fullDetailGates=new Map();
 let taskNumber=0,startGate=null,startFailure=null;
 c.api=async(path,values)=>{
  calls.push({path,values});
  if(path==="/extensions/read"){const data=profiles.get(values.mrn)||null;if(readGates.has(values.mrn))await readGates.get(values.mrn).promise;return {data};}
  if(path==="/reviews/history/read")return {data:null,complete:true,updated_at:"saved-history",coverage:{numeric_start:"2016-01-01"}};
  if(path==="/tasks/start"){
   if(startGate)await startGate.promise;if(startFailure)throw startFailure;
   const id="PROFILE"+(++taskNumber);tasks.set(id,{id,kind:values.kind,mrn:values.mrn,review_task_id:values.review_task_id,name:"病人基本資料",status:"running",message:"正在查詢"});return {task_id:id};
  }
  if(path.startsWith("/tasks/detail")){const id=new URLSearchParams(path.split("?")[1]).get("id");if(detailGates.has(id))await detailGates.get(id).promise;if(!path.includes("summary=1")&&fullDetailGates.has(id))await fullDetailGates.get(id).promise;const task=tasks.get(id);assert.ok(task,"unknown synthetic profile task");return path.includes("summary=1")?{...task,items:undefined}:task;}
  throw new Error(path);
 };
 const starts=()=>calls.filter(call=>call.path==="/tasks/start"),text=element=>[element.textContent,...element.all().map(child=>child.textContent)].join(" ");
 const rows=()=>Object.fromEntries($("#dataContent").querySelector("tbody").children.map(row=>row.children.map(cell=>cell.children[0].textContent)));
 const update=()=>$("#dataContent").querySelector("button");
 c.reviewData={task:{id:"R1",status:"completed"},patients:[{...saved,records:[]}]};c.renderReviewDetail();
 let contacts=$("#reviewDetail").querySelector(".review-contact-info");
 assert.equal(contacts.parentElement,$("#reviewDetail").querySelector(".review-identity-line"),"contact badges moved outside the patient title row");
 const metadataPatient={checked_at:"2026-10-27T12:00:00+08:00",index_checked_at:"2026-10-27T04:00:00Z",reused:true,reuse_label:"沿用既有檢閱",records:[{date:"2026-10-20",section:"眼科",case_no:"ONE",updated_at:"2026-10-21T08:00:00",first_saved:"2026-10-20T07:00:00",cached:true}]};
 const metadataRows=info=>{const children=info.querySelector(".review-data-values").children;return Object.fromEntries(children.filter((_,index)=>index%2===0).map((child,index)=>[child.textContent,children[index*2+1].textContent]));};
 const info=c.reviewDataInfo(metadataPatient),infoRows=metadataRows(info);
 assert.equal(!!info.open,false,"metadata details expanded by default");assert.match(info.querySelector(".review-data-summary").textContent,/沿用既有檢閱/);
 assert.equal(Object.keys(infoRows).filter(label=>label.includes("核對")).length,1,"equivalent review/index timestamps were repeated");
 assert.match(infoRows["SOAP · 2026-10-20 · 眼科 · ONE · 取得"],/^2026-10-21T08:00:00/);
 assert.doesNotMatch(infoRows["SOAP · 2026-10-20 · 眼科 · ONE · 取得"],/2026-10-27|2026-10-20T07/);
 const different=metadataRows(c.reviewDataInfo({...metadataPatient,index_checked_at:"2026-10-26T12:00:00+08:00"}));
 assert.ok(different["檢閱核對"]&&different["索引核對"],"distinct metadata timestamps were collapsed");
 const noAcquisition=metadataRows(c.reviewDataInfo({...metadataPatient,records:[{...metadataPatient.records[0],updated_at:undefined}]}));
 assert.equal(Object.keys(noAcquisition).some(label=>label.endsWith("取得")),false,"missing SOAP acquisition time fell back to review or first-save time");
 c.SOAPView={create:()=>s.node("div","合成 SOAP")};
 vm.runInContext(source.split("\n").find(line=>line.startsWith("function soapNode(")),c);
 for(const cached of [true,false]){
  const record={...metadataPatient.records[0],id:"SOAP1",doctor:"合成醫師",cached};
  c.reviewData.patients[0]={...saved,...metadataPatient,records:[record]};c.renderReviewDetail();
  const caption=$("#reviewDetail").querySelector(".soap-record").querySelector(".caption");
  assert.equal(caption.textContent,"合成醫師 · ONE · "+(cached?"沿用本機版本":"已保存")+" · 取得 2026-10-21T08:00:00","SOAP caption omitted its source, reuse state or acquisition time");
  assert.doesNotMatch(caption.textContent,/2026-10-27|2026-10-20T07/,"SOAP caption used patient verification or first-save time");
  const section=$("#reviewDetail").querySelector(".soap-record"),original=s.node("details");original.open=true;section.append(original);
  c.reviewData.patients[0].national_id="C123456789";c.renderReviewDetail();
  assert.equal($("#reviewDetail").querySelector(".soap-record"),section,"profile-only change rebuilt an unchanged SOAP section");assert.equal(original.open,true,"profile update collapsed original SOAP text");
 }
 c.reviewData.patients[0]={...saved,...metadataPatient,records:[{...metadataPatient.records[0],doctor:"合成醫師",updated_at:undefined}]};c.renderReviewDetail();
 assert.equal($("#reviewDetail").querySelector(".soap-record").querySelector(".caption").textContent,"合成醫師 · ONE · 沿用本機版本","missing SOAP acquisition time gained a fallback timestamp");
 c.reviewData.patients[0]={...saved,records:[]};c.renderReviewDetail();contacts=$("#reviewDetail").querySelector(".review-contact-info");
 assert.deepEqual(contacts.children.map(child=>child.textContent),["身分證 A123456789","手機 0912000000","住家電話 07-0000000","電話 07-1111111"]);
 assert.equal(calls.length,0,"rendering saved patient identity contacted the server");
 assert.ok(contacts.children.every(child=>child.tagName==="button"&&child.className.includes("patient-copy-badge")),"contact fields are not copyable badges");
 await $("#reviewDetail").querySelector(".review-mrn-copy").emit("click");
 for(const badge of contacts.children)await badge.emit("click");
 await $("#reviewDetail").querySelector(".review-name-copy").emit("click");
 assert.deepEqual(copied,["P1","A123456789","0912000000","07-0000000","07-1111111","P1\t合成病人\t0912000000"],"clipboard values lost columns, leading zeroes or included display labels");
 assert.equal($("#reviewDetail").querySelector(".patient-age-badge"),null,"age 68 retained a badge");
 for(const [age,young] of [[0,true],[54,true],["54歲",true],[55,false],[68,false],["",false]]){
  c.reviewData.patients[0]={...saved,age,records:[]};c.renderReviewDetail();
  assert.equal(!!$("#reviewDetail").querySelector(".patient-age-young"),young,"wrong under-55 age style for "+age);
  assert.equal(!!$("#reviewDetail").querySelector(".patient-age-badge"),young);
  const sex=$("#reviewDetail").querySelectorAll("span").find(child=>child.textContent==="性別 女");
  assert.ok(sex&&!sex.className.includes("badge"),"sex was rendered as a badge");
 }
 c.reviewData.patients[0]={...saved,mobile_phone:"",records:[]};c.renderReviewDetail();
 await $("#reviewDetail").querySelector(".review-name-copy").emit("click");
 assert.equal(copied.at(-1),"P1\t合成病人\t","missing mobile used a different phone or removed the third column");
 c.reviewData.patients[0]={...saved,mrn:"00000123",name:"合成\t病人\r\n乙",mobile_phone:" 0900123456 ",records:[]};c.reviewPatient="00000123";c.renderReviewDetail();
 await $("#reviewDetail").querySelector(".review-name-copy").emit("click");
 assert.equal(copied.at(-1),"00000123\t合成 病人 乙\t0900123456","new patient copy retained an old identity or created extra spreadsheet cells");
 assert.equal(calls.length,0,"copying a patient header started a data request");c.reviewPatient="P1";
 c.reviewData.patients[0]={...compact,records:[]};c.renderReviewDetail();contacts=$("#reviewDetail").querySelector(".review-contact-info");
 assert.deepEqual(contacts.children.map(child=>child.textContent),["手機 0912000000","住家電話 07-0000000"],"compact phone was omitted or duplicated");
 const profileButton=$("#reviewDetail").querySelectorAll("button").find(button=>button.textContent==="病人基本資料");
 assert.ok(profileButton,"review has no explicit patient profile action");await profileButton.run();
 assert.equal($("#dataTitle").textContent,"病人基本資料");assert.equal(starts().length,1);
 assert.deepEqual(JSON.parse(JSON.stringify(starts()[0].values)),{kind:"profile",mrn:"P1",record_id:"",review_task_id:"R1",force:false});
 assert.ok($("#dataContent").querySelector(".clinical-loading"),"running profile task has no waiting state");
 const firstTask=c.extensionTask;tasks.set(firstTask,{...tasks.get(firstTask),status:"completed",items:[saved]});
 await c.showExtensionTask(firstTask,"profile",false);assert.equal(c.extensionTask,"");
 assert.equal(rows()["身分證"],"A123456789");assert.equal(rows()["手機"],"0912000000");assert.equal(rows()["住家電話"],"07-0000000");
 assert.equal(rows()["電話"],"07-1111111");assert.equal(rows()["聯絡人"],"合成聯絡人");
 assert.match($("#dataContent").children[0].textContent,/basic-saved/);assert.doesNotMatch($("#dataContent").children[0].textContent,/recent-local-save/);
 assert.equal(noteLoads.length,1);assert.equal(reviewLoads.length,1,"completed profile did not refresh the review header");
 profiles.set("P1",saved);await c.extension("profile","P1");assert.equal(starts().length,1,"opening a saved full profile started another query");
 profiles.set("P1",{...saved,national_id:""});await c.extension("profile","P1");assert.equal(starts().length,1,"missing national ID invalidated successful full profile");
 assert.equal(rows()["身分證"],"院方未提供");await update().run();assert.equal(starts().length,2);assert.equal(starts().at(-1).values.force,true,"manual update did not force a fresh profile");
 for(const state of ["offline","readonly"]){
  c.work.online=state!=="offline";c.root.read_only=state==="readonly";
  profiles.set("P1",compact);const before=starts().length;await c.extension("profile","P1");
  assert.equal(starts().length,before,state+" compact profile started a query");assert.equal(update().disabled,true,state+" profile update remained enabled");
  assert.equal(rows()["身分證"],"尚未查詢");assert.equal(rows()["手機"],"0912000000");
  profiles.set("P1",saved);await c.extension("profile","P1");assert.equal(rows()["身分證"],"A123456789");assert.equal(update().disabled,true);
  await c.extension("profile","P1","",true);assert.equal(starts().length,before,state+" forced update started a query");assert.equal(update().disabled,true);
  profiles.delete("P1");await c.extension("profile","P1");assert.equal(starts().length,before);assert.equal($("#dataContent").querySelector(".clinical-loading"),null);
  assert.match(text($("#dataContent")),state==="readonly"?/唯讀/:/登入/);
 }
 c.work.online=true;c.root.read_only=false;
 const unsafe='<img src=x onerror="throw 1">';c.extensionContext={kind:"profile",mrn:"P1",record_id:""};
 c.renderExtension({...saved,name:unsafe,national_id:unsafe,contact:unsafe,mobile_phone:"",home_phone:"",phone:""},"profile");
 assert.equal(rows()["姓名"],unsafe);assert.equal(rows()["身分證"],unsafe);assert.equal(rows()["聯絡人"],unsafe);
 assert.equal($("#dataContent").querySelectorAll("img,script").length,0,"profile response was inserted as HTML");
 assert.match(text($("#dataContent")),/院方未提供連絡電話/);assert.equal(rows()["手機"],undefined);
 c.renderExtension({mrn:"P1",basic_info_at:"saved-empty"},"profile");assert.equal(rows()["姓名"],"未提供");assert.equal(rows()["聯絡人"],"未提供");
 profiles.set("P1",compact);readGates.set("P1",deferred());let before=starts().length;
 const closed=c.extension("profile","P1");$("#dataDialog").open=false;$("#dataContent").replaceChildren(s.node("p","closed modal"));
 readGates.get("P1").resolve();await closed;readGates.delete("P1");
 assert.equal(starts().length,before,"closing modal during local read started a query");assert.equal(text($("#dataContent"))," closed modal");
 readGates.set("P1",deferred());const switched=c.extension("profile","P1");await c.extension("profile","P2");const replacement=$("#dataContent").children[0];
 readGates.get("P1").resolve();await switched;readGates.delete("P1");
 assert.equal(starts().length,before,"late first-patient read started a query");assert.equal(rows()["病歷號"],"P2");assert.equal($("#dataContent").children[0],replacement,"late first-patient read replaced the modal");
 readGates.set("P1",deferred());const accountSwitch=c.extension("profile","P1");c.epoch++;$("#dataContent").replaceChildren(s.node("p","other account"));
 readGates.get("P1").resolve();await accountSwitch;readGates.delete("P1");
 assert.equal(starts().length,before,"stale account read started a profile query");assert.equal(text($("#dataContent"))," other account");
 startGate=deferred();const submitting=c.extension("profile","P1");await tick();assert.equal(starts().length,before+1);
 $("#dataDialog").open=false;$("#dataContent").replaceChildren(s.node("p","closed during submit"));const detailsBefore=calls.filter(call=>call.path.startsWith("/tasks/detail")).length;
 startGate.resolve();await submitting;startGate=null;assert.equal(calls.filter(call=>call.path.startsWith("/tasks/detail")).length,detailsBefore,"late submit read a task after modal closed");
 assert.equal(text($("#dataContent"))," closed during submit");
 await c.extension("profile","P1");const staleTask=c.extensionTask;detailGates.set(staleTask,deferred());const polling=c.showExtensionTask(staleTask,"profile",false);
 await c.extension("profile","P2");detailGates.get(staleTask).resolve();await polling;detailGates.delete(staleTask);
 assert.equal(rows()["病歷號"],"P2","late task polling replaced a different patient's saved profile");
 detailGates.set(firstTask,deferred());$("#dataDialog").open=false;
 const opening=c.showExtensionTask(firstTask,"profile");
 assert.equal($("#dataDialog").open,true,"activity profile action did not open its modal before loading");assert.ok($("#dataContent").querySelector(".clinical-loading"));
 await c.extension("profile","P2");const newerTitle=$("#dataTitle").textContent,newerContent=$("#dataContent").children[0],noteCount=noteLoads.length;
 detailGates.get(firstTask).resolve();await opening;detailGates.delete(firstTask);
 assert.equal(rows()["病歷號"],"P2","late activity task summary replaced the newer patient");assert.equal($("#dataContent").children[0],newerContent);
 assert.equal($("#dataTitle").textContent,newerTitle);assert.equal(noteLoads.length,noteCount,"stale activity task refreshed another review");
 detailGates.set(firstTask,deferred());const closing=c.showExtensionTask(firstTask,"profile");$("#dataDialog").open=false;
 $("#dataContent").replaceChildren(s.node("p","closed activity modal"));detailGates.get(firstTask).resolve();await closing;detailGates.delete(firstTask);
 assert.equal($("#dataDialog").open,false,"late activity task reopened a closed modal");assert.equal(text($("#dataContent"))," closed activity modal");
 fullDetailGates.set(firstTask,deferred());const fullOpening=c.showExtensionTask(firstTask,"profile");await tick();
 assert.equal(calls.at(-1).path,"/tasks/detail?id="+firstTask,"completed activity task did not request its saved item");
 await c.extension("profile","P2");fullDetailGates.get(firstTask).resolve();await fullOpening;fullDetailGates.delete(firstTask);
 assert.equal(rows()["病歷號"],"P2","late completed activity task detail replaced the newer patient");
 const startsBeforeActivity=starts().length;await c.showExtensionTask(firstTask,"profile");
 assert.equal(rows()["病歷號"],"P1");assert.equal($("#dataDialog").dataset.mrn,"P1");assert.equal(starts().length,startsBeforeActivity,"opening a saved activity task launched a fresh query");
 detailGates.set(firstTask,deferred());const closeThenGeneric=c.showExtensionTask(firstTask,"profile");await $("#dataDialog").close();
 assert.equal(c.extensionTask,"");assert.equal(c.extensionContext,null,"closing the data dialog retained its pending profile context");
 c.dialog("dataDialog");$("#dataTitle").textContent="集合名稱";$("#dataContent").replaceChildren(s.node("form","generic editor"));
 detailGates.get(firstTask).resolve();await closeThenGeneric;detailGates.delete(firstTask);
 assert.equal($("#dataTitle").textContent,"集合名稱");assert.equal(text($("#dataContent"))," generic editor","old activity response revived after another modal reopened");
 for(const closeFirst of [false,true]){
  readGates.set("P1",deferred());const localReading=c.extension("profile","P1"),beforeGeneric=starts().length;
  if(closeFirst)await $("#dataDialog").close();
  c.dialog("dataDialog");$("#dataContent").replaceChildren(s.node("form","generic takeover"));
  readGates.get("P1").resolve();await localReading;readGates.delete("P1");
  assert.equal(starts().length,beforeGeneric,"generic modal takeover started the old patient query");assert.equal(text($("#dataContent"))," generic takeover");
 }
 c.window.addEventListener=()=>{};c.reviewPatientIdentity=()=>({name:"歷史資料病人"});s.run("vghks_bot/static/review-history.js");
 readGates.set("P1",deferred());const historyTakeover=c.extension("profile","P1"),beforeHistory=starts().length;
 await c.window.ReviewHistoryUI.open("numeric","P2");const historyContent=$("#dataContent").children[0];
 readGates.get("P1").resolve();await historyTakeover;readGates.delete("P1");
 assert.equal(starts().length,beforeHistory,"history modal takeover started the old profile query");assert.equal($("#dataDialog").dataset.view,"history");
 assert.equal($("#dataContent").children[0],historyContent,"old profile local read replaced the history modal");
 profiles.set("P1",compact);startFailure=new Error("synthetic profile query failure");await assert.rejects(c.extension("profile","P1"),/profile query failure/);startFailure=null;
 assert.equal(rows()["手機"],"0912000000","failed profile query discarded compact contact data");assert.equal(update().disabled,false);
 assert.equal($("#dataContent").querySelector(".clinical-loading"),null,"failed profile query retained a spinner");
}
async function historyLoadingTests(){
 const s=surface(),c=s.context,$=s.$;
 c.window.addEventListener=()=>{};c.document.createElement=tag=>c.node(tag);
 s.run("vghks_bot/static/clinical-ui.js");c.ClinicalUI=c.window.ClinicalUI;
 c.dialog=()=>{$("#dataDialog").open=true;};c.setPatientDialogContext=()=>{};
 c.reviewPatientIdentity=()=>({name:"合成病人"});c.extensionTask="";
 c.JobProgress={pending(){},publish(){}};c.work.online=true;
 c.appendNumericTables=(content,tables)=>content.append(c.node("p",tables[0]?.title||"numeric result"));
 let readGate=null,startGate=null,detailGate=null,readFailure=null,startFailure=null,status="running",complete=false,data=null,starts=0,details=0,revision=0,updated="saved-first",issues=[];
 c.api=async path=>{
  if(path==="/reviews/history/read"){if(readGate)await readGate.promise;if(readFailure)throw readFailure;return {data,complete,updated_at:updated,issues};}
  if(path==="/tasks/start"){starts++;if(startGate)await startGate.promise;if(startFailure)throw startFailure;return {task_id:"H1"};}
  if(path.startsWith("/tasks/detail")){details++;if(detailGate)await detailGate.promise;return {status,revision,updated_at:"same-second",message:"合成查詢結果"};}
  throw new Error(path);
 };
 s.run("vghks_bot/static/review-history.js");const ui=c.window.ReviewHistoryUI;
 const spinner=()=>$("#dataContent").querySelector(".clinical-loading");
 readGate=deferred();const reading=ui.open("numeric","P1");
 assert.ok(spinner(),"local read did not show loading");assert.equal(spinner().role,"status");
 assert.equal(spinner().querySelector(".clinical-loading-icon")["aria-hidden"],"true");
 assert.match(spinner().querySelector(".clinical-loading-label").textContent,/讀取/);
 readGate.resolve();await reading;readGate=null;assert.ok(spinner(),"running query lost loading");
 const waiting=spinner();revision++;await ui.poll();assert.equal(spinner(),waiting,"progress-only update restarted spinner");
 assert.match(waiting.querySelector(".clinical-loading-elapsed").textContent,/(?:已等待 )?\d+ 秒/);
 detailGate=deferred();revision++;const beforeDetails=details,firstPoll=ui.poll(),secondPoll=ui.poll();
 detailGate.resolve();await Promise.all([firstPoll,secondPoll]);detailGate=null;
 assert.equal(details-beforeDetails,1,"overlapping polling duplicated history requests");
 data={tables:[{title:"先顯示已取得資料"}]};updated="saved-second";issues=[{code:"TEST_PARSE"}];revision++;await ui.poll();
 assert.ok($("#dataContent").all().some(child=>child.textContent==="先顯示已取得資料"),"partial result was hidden while fetching");
 assert.ok(spinner(),"remaining query did not show waiting state");
 assert.match($("#dataContent").querySelector(".history-index-issues").textContent,/1 項未能確認/);
 const partialSpinner=spinner();revision++;await ui.poll();assert.equal(spinner(),partialSpinner,"unchanged partial result was rebuilt");
 issues=[];
 status="completed";complete=true;data={tables:[{title:"合成數值結果"}]};await ui.poll();
 assert.equal(spinner(),null,"completed report kept loading");assert.ok($("#dataContent").all().some(child=>child.textContent==="合成數值結果"));
 status="running";complete=false;data=null;await ui.open("numeric","P1");
 status="completed";complete=true;data={tables:[{title:"恢復後的合成數值"}]};readFailure=new Error("synthetic polling failure");
 await assert.rejects(ui.poll(),/polling failure/);assert.equal(spinner(),null,"polling read failure kept loading");
 await assert.rejects(ui.poll(),/polling failure/);assert.equal($("#dataContent").querySelectorAll(".history-load-error").length,1,"repeated failure duplicated errors");
 readFailure=null;await ui.poll();assert.ok($("#dataContent").all().some(child=>child.textContent==="恢復後的合成數值"),"failed read consumed task update before result loaded");
 for(const terminal of ["failed","paused","partial","cancelled","completed"]){
  status=terminal;complete=false;data=null;await ui.open("order_report","P1","ORDER1");
  assert.equal(spinner(),null,terminal+" report kept loading");
 }
 status="running";data=null;complete=false;startGate=deferred();const queued=ui.open("case_orders","P1","CASE1");await tick();
 assert.ok(spinner(),"task submission lost loading");startGate.resolve();await queued;startGate=null;
 c.work.online=false;const before=starts;await ui.open("orders","P1");assert.equal(spinner(),null);assert.equal(starts,before,"offline report started query");
 c.work.online=true;c.root.read_only=true;await ui.open("numeric","P1");assert.equal(spinner(),null);assert.equal(starts,before,"readonly report started query");c.root.read_only=false;
 readFailure=new Error("synthetic saved report failure");await assert.rejects(ui.open("numeric","P1"),/saved report failure/);assert.equal(spinner(),null);readFailure=null;
 startFailure=new Error("synthetic query rejection");await assert.rejects(ui.open("case_numeric","P1","CASE1"),/query rejection/);assert.equal(spinner(),null);startFailure=null;
 readGate=deferred();readFailure=new Error("stale request failure");const stale=ui.open("numeric","P1");ui.reset();
 $("#dataContent").replaceChildren(c.node("p","other patient"));readGate.resolve();await assert.rejects(stale,/stale/);
 assert.equal($("#dataContent").children[0].textContent,"other patient","stale error replaced new patient");
}
function layoutTests(){
 const s=surface(),c=s.context,classes=new Set(),progress=[];
 c.document.body={classList:{toggle:(name,enabled)=>{if(enabled)classes.add(name);else classes.delete(name);}}};
 c.page="review";c.reviewSetupExpanded=false;c.reviewHeaderTask="";c.reviewHasResults=false;c.suppressCompletedReview=false;
 c.reviewConditionSummary=()=>"合成條件";c.JobProgress.show=(target,task)=>progress.push(task.status);
 const source=fs.readFileSync("vghks_bot/static/bot.js","utf8");
 vm.runInContext(source.match(/function updateReviewLayout\(\)\{[^\n]+/)[0],c);
 vm.runInContext(source.slice(source.indexOf("function renderReviewHeader("),source.indexOf("async function loadReview(")),c);
 for(const status of ["queued","running","paused","failed","partial","completed"]){
  c.reviewData={task:{id:"R1",status},patients:[{records:[]}]};c.renderReviewHeader(c.reviewData);
  assert.ok(classes.has("review-result-compact"),status+" reserved blank viewport space");
 }
 c.reviewData={task:{id:"R1",status:"running"},patients:[{records:[{id:"SOAP1"}]}]};c.renderReviewHeader(c.reviewData);
 assert.equal(progress.at(-1),"running","full height hid active task progress");
 c.reviewSetupExpanded=true;c.updateReviewLayout();assert.equal(classes.has("review-result-compact"),false);
 c.reviewSetupExpanded=false;c.updateReviewLayout();assert.ok(classes.has("review-result-compact"),"collapsing setup did not restore viewport layout");
 c.page="tasks";c.updateReviewLayout();assert.equal(classes.has("review-result-compact"),false);
 c.page="review";c.updateReviewLayout();assert.ok(classes.has("review-result-compact"),"returning to active review lost viewport layout");
 c.reviewData=null;c.updateReviewLayout();assert.equal(classes.has("review-result-compact"),false);
}
async function toolSelectionTests(){
 const s=surface(),c=s.context,$=s.$,requests=[],modules={},actions=[];let lookupGate=null,startGate=null;
 const kinds=["review","retina","cataract","surgery"],groups=kinds.map(kind=>({id:"SET-"+kind,name:"合成清單 "+kind,
  members:[1,2,3].map(value=>({mrn:"0000000"+value,name:"合成病人"+value,registrations:[]}))}));
 for(const group of groups)modules[group.id.slice(4)]={set_id:group.id};
 const original=JSON.stringify(groups);
 c.work={sets:groups,tasks:[],online:true,preferences:{review_options:{}}};c.page="tool";c.selectedSet=null;
 c.document.getElementById=id=>$("#"+id);c.document.createElement=tag=>s.node(tag);
 c.toolOptions=()=>{};c.openRules=()=>{};c.updateReviewLayout=()=>{};c.reviewSetupExpanded=false;c.refresh=async()=>{};
 c.epoch=0;$("#toolClearPatients").tagName="BUTTON";
 c.JobProgress={pending(){},show(){},ready:async()=>{},wait:async()=>{}};
 $("#toolPage").append(s.node("p"));
 c.api=async(path,values)=>{
  requests.push({path,values});
  if(path==="/tools/state")return {modules:JSON.parse(JSON.stringify(modules))};
  if(path==="/sets/read")return JSON.parse(JSON.stringify(groups.find(group=>group.id===values.id)));
  if(path==="/sets/search")return {sets:groups.filter(group=>[group.name,...group.members.map(patient=>patient.mrn)].some(text=>text.includes(values.q))).map(group=>({id:group.id,name:group.name,member_count:group.members.length,members_preview:group.members.slice(0,1),members_loaded:false}))};
  if(path==="/tools/state/save"){modules[values.module]={set_id:values.set_id};return {modules};}
  if(path==="/tools/handoff/remove")return {worklist:{...groups[3],worklist_module:"surgery",members:groups[3].members.filter(member=>!values.mrns.includes(member.mrn))},cohort:{id:"WORKLIST-COHORT"}};
  if(path==="/tasks/start"){if(values.kind==="resolve")return lookupGate.promise;if(values.kind==="review")return startGate.promise;}
  if(path.startsWith("/tasks/detail"))return {status:"completed",done:1,items:[{mrn:"00000999",input:"00000999",status:"resolved"}]};
  throw new Error(path);
 };
 const source=fs.readFileSync("vghks_bot/static/bot.js","utf8");
 vm.runInContext(source.slice(source.indexOf("async function act("),source.indexOf("async function request(")),c);
 const runAction=c.act;c.act=(...args)=>{const action=runAction(...args);actions.push(action);return action;};
 vm.runInContext(source.slice(source.indexOf("function patientChip("),source.indexOf("const labels=")),c);
 s.run("vghks_bot/static/adaptive-identifiers.js");s.run("vghks_bot/static/tool-workspace.js");const ui=c.window.ToolWorkspace;
 for(const [index,kind] of kinds.entries()){
  await ui.open(kind,groups[index]);assert.equal(c.selectedSet.members.length,3);assert.equal($("#toolClearPatients").disabled,false);
  const chip=$("#toolSelectionSummary").children[0];assert.equal(chip.querySelector(".patient-chip-name").textContent,"合成病人1");
  assert.equal(chip.querySelector(".patient-chip-mrn").textContent,"00000001");assert.match(chip["aria-label"],/移除病人.*00000001/);
  await chip.run();assert.equal(c.selectedSet.members.length,2,"individual badge removal lost behavior");
  await $("#toolClearPatients").emit("click");assert.equal(c.selectedSet.members.length,0);assert.equal($("#toolSelectionSummary").children.length,0);
  assert.equal($("#toolStart").disabled,true);assert.equal($("#toolClearPatients").disabled,true);assert.equal(modules[kind].set_id,"");
  assert.equal(JSON.stringify(groups),original,"clearing deleted saved group members");
 }
 await ui.open("review",groups[0]);const task={id:"R1",set_id:groups[0].id,members:groups[0].members,status:"running",mode:"latest",cutoff:"2026-10-06"};
 ui.adoptReview(task);await $("#toolEditSetup").emit("click");
 lookupGate=deferred();$("#toolIdentifier").value="00000999 ";await $("#toolIdentifier").emit("input");await tick();
 assert.ok(requests.some(request=>request.values?.kind==="resolve"));
 await $("#toolClearPatients").emit("click");lookupGate.resolve({task_id:"LOOKUP1"});await tick();
 assert.equal(c.selectedSet.members.length,0,"late lookup repopulated cleared selection");
 assert.equal($("#toolIdentifier").value,"");assert.equal(task.members.length,3);assert.equal(task.status,"running");
 assert.equal($("#toolStart").textContent,"開始新檢閱","clearing lost existing review context");
 await ui.open("retina",groups[1]);await ui.open("surgery",groups[3]);await $("#toolClearPatients").emit("click");
 assert.equal(modules.retina.set_id,groups[1].id,"clearing one tool reset another tool");
 ui.reset();await ui.open("surgery");assert.equal(c.selectedSet,null,"cleared tool restored old source after reset");
 await ui.open("review",groups[0]);startGate=deferred();await $("#toolStartForm").emit("submit");const starting=actions.at(-1);await tick();
 assert.equal($("#toolClearPatients").disabled,true);await $("#toolClearPatients").emit("click");
 assert.equal(c.selectedSet.members.length,3,"clearing changed selection while starting task");startGate.resolve({task_id:"NEW-REVIEW"});await starting;
 // Carrying a review forward must flush notes before creating the snapshot.
 await ui.open("review",groups[0]);ui.adoptReview({...task,status:"completed"});
 const opened=[],selected=[];c.openReview=async id=>opened.push(id);c.selectReviewPatient=async mrn=>selected.push(mrn);
 let notesGate=deferred();c.window.ReviewNotesUI={flushAll:()=>notesGate.promise};startGate=deferred();
 const before=requests.filter(request=>request.path==="/tasks/start").length;
 const single=ui.refreshPatient("00000002");await tick();await ui.refreshPatient("00000002");
 assert.equal(requests.filter(request=>request.path==="/tasks/start").length,before,"single refresh started before notes were saved");
 notesGate.resolve(true);await tick();
 const singleRequest=requests.filter(request=>request.path==="/tasks/start").at(-1).values;
 assert.equal(singleRequest.base_task_id,"R1");assert.equal(singleRequest.refresh_mrn,"00000002");
 assert.equal(singleRequest.set_id,undefined);assert.equal(singleRequest.cutoff,undefined,"single refresh overrode stored scope");
 startGate.resolve({task_id:"SINGLE"});await single;assert.equal(opened.at(-1),"SINGLE");assert.equal(selected.at(-1),"00000002");
 c.window.ReviewNotesUI.flushAll=async()=>false;
 const blockedCount=requests.length;await assert.rejects(ui.refreshPatient("00000001"),/備註尚未儲存/);assert.equal(requests.length,blockedCount);
 notesGate=deferred();c.window.ReviewNotesUI.flushAll=()=>notesGate.promise;startGate=deferred();
 $("#forceReview").checked=true;await $("#toolStartForm").emit("submit");const carry=actions.at(-1);await tick();
 const startCount=requests.filter(request=>request.path==="/tasks/start").length;notesGate.resolve(true);await tick();
 assert.equal(requests.filter(request=>request.path==="/tasks/start").length,startCount+1);
 const carryRequest=requests.filter(request=>request.path==="/tasks/start").at(-1).values;
 assert.equal(carryRequest.base_task_id,"R1");assert.equal(carryRequest.force,true);assert.equal(carryRequest.refresh_mrn,undefined);
 startGate.resolve({task_id:"CARRY"});await carry;assert.equal(opened.at(-1),"CARRY");
 notesGate=deferred();c.window.ReviewNotesUI.flushAll=()=>notesGate.promise;
 const stale=ui.refreshPatient("00000001");await tick();await ui.open("retina",groups[1]);
 const switchedCount=requests.length;notesGate.resolve(true);await stale;
 assert.equal(requests.length,switchedCount,"late note flush started a refresh in another tool");
 assert.equal($("#toolStart").disabled,false,"switching tools retained another tool's starting state");
 c.root.read_only=true;await ui.open("cataract",groups[2]);const writes=requests.filter(request=>request.path==="/tools/state/save").length;
 await $("#toolClearPatients").emit("click");assert.equal(c.selectedSet.members.length,0);
 assert.equal(requests.filter(request=>request.path==="/tools/state/save").length,writes,"readonly clearing wrote database");
 assert.equal(JSON.stringify(groups),original);ui.reset();c.root.read_only=false;
 await ui.open("surgery",{...groups[3],worklist_module:"surgery"});
 await $("#toolClearPatients").emit("click");assert.equal(c.selectedSet.members.length,0);
 assert.ok(requests.some(request=>request.path==="/tools/handoff/remove"&&request.values.mrns.length===3));
 assert.equal(JSON.stringify(groups),original);ui.reset();
 c.work.sets=groups.map(group=>({id:group.id,name:group.name,member_count:group.members.length,members_preview:group.members.slice(0,1),members_loaded:false}));
 const compactReads=requests.filter(request=>request.path==="/sets/read").length;
 ui.adoptReview({...task,id:"COMPACT",members:undefined,member_count:3});
 assert.equal($("#toolSelectionSummary").children.length,0,"collapsed source eagerly built patient chips");
 assert.match($("#toolSelectedCount").textContent,/3 位/);
 assert.equal(requests.filter(request=>request.path==="/sets/read").length,compactReads,"adopting compact task fetched the whole source set");
 await $("#toolEditSetup").emit("click");assert.equal($("#toolSelectionSummary").children.length,3);
 assert.equal(requests.filter(request=>request.path==="/sets/read").length,compactReads+1,"editing compact source did not load its members");
 $("#toolSavedDialog").close=()=>{};await $("#toolOpenSaved").emit("click");
 $("#toolSavedQuery").value="00000003";await $("#toolSavedQuery").emit("input");await new Promise(resolve=>setTimeout(resolve,230));
 assert.ok(requests.some(request=>request.path==="/sets/search"&&request.values.q==="00000003"),"saved set search searched only preview MRNs");ui.reset();
}
async function handoffTests(){
 const s=surface(),c=s.context,$=s.$,calls=[],opened=[],received=[];let gate=null,flush=true;
 c.Option=function(label,value){const option=s.node("option",label);option.value=value;return option;};
 $("#handoffForm").reportValidity=()=>true;$("#handoffDialog").close=()=>{$("#handoffDialog").open=false;};
 c.dialog=id=>{$("#"+id).open=true;};c.openModule=async(...args)=>opened.push(args);
 c.window.ReviewNotesUI={flushAll:async()=>flush};c.window.ToolWorkspace={receivedHandoff:(...args)=>received.push(args)};
 const result={worklist:{id:"W1"},cohort:{id:"C1"},focus_mrn:"P1"};
 c.api=async(path,values)=>{calls.push({path,values});if(gate)await gate.promise;return result;};
 s.run("vghks_bot/static/review-handoff.js");const ui=c.window.ReviewHandoff;
 const patient={mrn:"P1",name:"合成病人",records:[{id:"SOAP1",date:"2026-10-11",section:"眼科"}]};
 ui.open(patient);$("#handoffModule").value="surgery";$("#handoffSource").value="SOAP1";
 gate=deferred();const send=ui.send(false);await tick();assert.equal($("#handoffAdd").disabled,true);
 await ui.send(false);assert.equal(calls.length,1,"double click appended twice");gate.resolve();await send;gate=null;
 assert.deepEqual(JSON.parse(JSON.stringify(calls[0].values)),{module:"surgery",review_task_id:"R1",mrns:["P1"],record_ids:["SOAP1"]});
 assert.equal(opened.length,0,"add-only opened tool");assert.equal(received.length,1);
 ui.open(patient);$("#handoffModule").value="cataract";$("#handoffSource").value="";await ui.send(true);
 assert.equal(opened[0][0],"C1");assert.equal(opened[0][1],"cataract");assert.equal(opened[0][2].focusMrn,"P1");assert.equal(opened[0][2].selectedOnly,true);
 ui.open(patient);flush=false;const before=calls.length;await assert.rejects(ui.send(true),/備註/);assert.equal(calls.length,before);
 flush=true;c.account="OTHER";await assert.rejects(ui.send(false),/帳號已切換/);
 c.root.read_only=true;assert.throws(()=>ui.open(patient),/唯讀/);
}
async function sheetSyncTests(){
 const s=surface(),c=s.context,$=s.$,calls=[],opened=[];
 $("#toolSourcePanel").insertAdjacentHTML=()=>{};c.document.body={insertAdjacentHTML(){}};
 const close=s.node("button");$("#sheetSyncDialog").querySelector=()=>close;$("#sheetSyncDialog").close=()=>{};
 $("#sheetSyncForm").reportValidity=()=>true;c.openModule=async(...args)=>opened.push(args);
 const state={settings:{enabled:true,interval_minutes:60,last_run_at:"2026-10-11T10:00:00"},online:true,running:false,
  pending:[{id:"PENDING1",mrn:"P1",name:"合成病人",reason:"日期未填",cohort_id:"C1"}]};
 c.api=async(path,values)=>{calls.push({path,values});return path.endsWith("settings")?state.settings:state;};
 s.run("vghks_bot/static/sheet-sync-ui.js");const ui=c.window.SheetSyncUI;
 ui.visibility("module","surgery");await tick();assert.equal($("#sheetSyncPanel").hidden,false);
 assert.match($("#sheetSyncStatus").textContent,/每 60 分鐘/);assert.equal($("#sheetSyncPending").children.length,1);
 const actions=$("#sheetSyncPending").children[0].children[2];await actions.children[0].run();
 assert.equal(opened[0][0],"C1");assert.equal(opened[0][1],"surgery");await actions.children[1].run();
 assert.ok(calls.some(call=>call.path==="/sheet-sync/review"&&call.values.id==="PENDING1"&&call.values.action==="dismiss"));
 $("#sheetSyncEnabled").checked=true;$("#sheetSyncInterval").value="60";await $("#sheetSyncForm").emit("submit");
 assert.ok(calls.some(call=>call.path==="/sheet-sync/settings"&&call.values.enabled===true&&call.values.interval_minutes===60));
 await $("#sheetSyncRun").emit("click");assert.ok(calls.some(call=>call.path==="/sheet-sync/run"));
 ui.visibility("review","review");assert.equal($("#sheetSyncPanel").hidden,true);const before=calls.length;await ui.poll();assert.equal(calls.length,before);
}
async function destinationTests(){
 const s=surface(),c=s.context,editors=new Map();c.surgeryEditors=editors;
 c.el=s.node;c.button=c.btn;c.action=c.act;
 c.Option=function(text,value){const option=s.node("option",text);option.value=value;return option;};
 let reads=0,invalidated=0,gate=deferred();c.ap=async(path,values)=>{assert.equal(path,"surgery/locations");assert.deepEqual(Object.keys(values),[]);reads++;return gate.promise;};
 const source=fs.readFileSync("vghks_bot/static/analysis.js","utf8");
 vm.runInContext(source.slice(source.indexOf("function surgeryDestination("),source.indexOf("function surgeryCard(")),c);
 vm.runInContext(source.slice(source.indexOf("function collectProposals("),source.indexOf("async function createSheetPreview(")),c);
 const inputs={date:s.node("input"),hospital:s.node("input"),mrn:s.node("input")};
 inputs.date.value="2026-10-18";inputs.hospital.value="高榮";inputs.mrn.value="P1";
 const route=c.surgeryDestination({fields:{mrn:"P1"}},inputs,()=>invalidated++);route.select.reportValidity=()=>false;
 const editor={row:{id:"C1",fields:{date:"2026-10-11"}},selected:{checked:true},
  inputs,checks:{date:{checked:true},hospital:{checked:true},mrn:{checked:true}},reviewed:{checked:false},target:{value:""},destination:route.enabled,route};
 editors.set("C1",editor);assert.equal(c.collectProposals()[0].destination,undefined,"editing source route silently authorized a move");
 assert.equal(reads,0,"rendering an ordinary candidate read Google");editor.destination.checked=true;await editor.destination.emit("change");
 assert.equal(reads,0,"checking destination fetched without the read action");assert.throws(()=>c.collectProposals(),/先讀取目的區塊/);
 const reading=route.read();await route.read();assert.equal(reads,1,"double read started parallel Google requests");
 gate.resolve({blocks:[{date:"2026-10-18",hospital:"高榮",block_sync_id:"UID-A",ref:"202610!2",label:"2026-10-18 高榮 · 202610!2"},
  {date:"2026-10-18",hospital:"高榮",block_sync_id:"ref:202610!30",ref:"202610!30"},
  {date:"2026-10-19",hospital:"高榮",block_sync_id:"UID-B",ref:"202610!60"},
  {date:"2026-10-18",hospital:"其他",block_sync_id:"UID-C",ref:"202610!90"}]});await reading;
 assert.equal(route.select.required,true);assert.equal(route.select.children.length,3,"date/hospital filter included unrelated blocks");
 assert.equal(route.select.children[1].textContent,"2026-10-18 高榮 · 202610 第 2 列");
 assert.equal(route.select.children[2].textContent,"202610 第 30 列");
 assert.ok(route.select.children.every(option=>!option.textContent.includes("UID-")&&!option.textContent.includes("ref:")),"technical identity appeared in labels");
 assert.throws(()=>c.collectProposals(),/多個區塊/);route.select.value="ref:202610!30";await route.select.emit("change");
 let destination=c.collectProposals()[0].destination;
 assert.equal(destination.date,"2026-10-18");assert.equal(destination.hospital,"高榮");assert.equal(destination.block_sync_id,"ref:202610!30");
 inputs.date.value="2026-10-19";await inputs.date.emit("input");destination=c.collectProposals()[0].destination;
 assert.equal(route.select.required,false);assert.equal(destination.block_sync_id,"UID-B","unique destination was not selected");
 inputs.date.value="2026-10-20";await inputs.date.emit("input");assert.equal(c.collectProposals()[0].destination.block_sync_id,"");
 assert.equal(reads,1,"editing fields re-read Google");assert.ok(invalidated>0);
 gate=deferred();const failed=route.read();gate.reject(new Error("synthetic locations failure"));await assert.rejects(failed,/locations failure/);
 assert.throws(()=>c.collectProposals(),/先讀取目的區塊/,"failed refresh retained stale destinations");
}
async function fuTests(){
 const s=surface(),c=s.context,editors=new Map(),calls=[],normalCards=[],opened=[];
 c.Option=function(text,value){const option=s.node("option",text);option.value=value;return option;};
 c.surgeryEditors=editors;c.el=s.node;c.button=c.btn;c.action=c.act;c.config={read_only:false};
 c.currentCohort={id:"COHORT",members:[{mrn:"P1",name:"追蹤病人"},{mrn:"P2",name:"無候選病人"},{mrn:"P3",name:"手術病人"}]};
 const labels={mrn:"病歷號",name:"姓名",hospital:"醫院",tag:"Tag",date:"刀日",side:"眼別",procedure:"術式"};
 const fu={id:"FU1",kind:"fu",fields:{mrn:"P1",name:"追蹤病人",hospital:"高榮",tag:"FU"},
  sources:[{date:"2026-10-11",excerpt:"符合追蹤標記的合成SOAP",record_id:"SOAP1",version:"VERSION1"}]};
 const candidates=[fu,{id:"OP3",fields:{mrn:"P3",name:"手術病人"}}];
 c.openSavedRecord=async id=>opened.push(id);
 c.surgeryCard=row=>{normalCards.push(row.id);return s.node("article","手術候選");};
 c.ap=async(path,values)=>{calls.push({path,values});
  if(path==="surgery/candidates")return {candidates,fields:labels};
  if(path==="surgery/drafts"){assert.equal(values.cohort_id,"COHORT");assert.equal(values.mrn,"P1");candidates.push({id:"DRAFT1",manual:true,fields:{mrn:"P1"}});return {};}
  throw new Error(path);
 };
 const source=fs.readFileSync("vghks_bot/static/analysis.js","utf8");
 vm.runInContext(source.slice(source.indexOf("async function loadSurgery("),source.indexOf("function surgeryDestination(")),c);
 vm.runInContext(source.slice(source.indexOf("function collectProposals("),source.indexOf("async function createSheetPreview(")),c);
 vm.runInContext(source.slice(source.indexOf("function previewNode("),source.indexOf('$$("[data-analysis-close]"')),c);
 await c.loadSurgery();assert.deepEqual(normalCards,["OP3"],"FU was rendered as a surgery candidate");
 const editor=editors.get("FU1");assert.ok(editor,"FU card did not join selected proposals");
 assert.deepEqual(Object.keys(editor.inputs),["mrn","name","hospital","tag"]);
 assert.ok(["mrn","name","tag"].every(key=>editor.inputs[key].readOnly),"FU source fields were editable");
 assert.equal(editor.inputs.hospital.tagName,"select");
 assert.equal(editor.card.querySelectorAll("select").length,1,"FU displayed extra route or surgery selection");
 assert.equal(editor.card.querySelectorAll("input").length,4,"FU displayed extra surgery fields");
 assert.ok(editor.card.all().some(node=>node.textContent==="SOAP 版本：VERSION1"),"FU source omitted its SOAP version");
 const proposal=c.collectProposals()[0];assert.deepEqual(Object.keys(proposal),["id","fields","selected_fields"]);
 assert.deepEqual(JSON.parse(JSON.stringify(proposal.fields)),fu.fields);
 assert.deepEqual(Array.from(proposal.selected_fields),["mrn","name","hospital","tag"]);
 const soapButton=editor.card.querySelectorAll("button").find(button=>button.textContent==="查看完整 SOAP");await soapButton.run();assert.deepEqual(opened,["SOAP1"]);
 s.$("#sheetPreview").append(s.node("p","old preview"));editor.selected.checked=false;await editor.selected.emit("change");
 assert.equal(s.$("#sheetPreview").children.length,0);assert.equal(c.collectProposals().length,0);editor.selected.checked=true;
 assert.doesNotThrow(()=>c.previewNode({id:"PREVIEW",spreadsheet_id:"synthetic",can_apply:false,
  items:[{id:"FU1",status:"conflict",fields:fu.fields,reason:"FU 重複列，請先整理。",options:[{ref:"FU!2"}]}]}),"FU conflict tried to use surgery target controls");
 const emptyFor=mrn=>s.$("#surgeryCandidates").children.find(card=>card.tagName==="section"&&card.children[0]?.textContent.endsWith(" · "+mrn));
 assert.ok(emptyFor("P1"),"FU-only patient lost the manual surgery draft entry");assert.ok(emptyFor("P2"));assert.equal(emptyFor("P3"),undefined);
 await emptyFor("P1").querySelector("button").run();assert.equal(calls.filter(call=>call.path==="surgery/drafts").length,1);
 assert.equal(emptyFor("P1"),undefined,"manual draft still appeared missing after creation");assert.ok(emptyFor("P2"));
 c.config.read_only=true;await c.loadSurgery();assert.equal(emptyFor("P2").querySelector("button").disabled,true);
 assert.ok(calls.every(call=>["surgery/candidates","surgery/drafts"].includes(call.path)),"FU rendering fetched Google destinations");
}
function syncDetailsTests(){
 const s=surface(),c=s.context;c.el=s.node;c.button=c.btn;c.action=c.act;c.surgeryEditors=new Map();c.Choices.enhance=()=>{};
 c.Option=function(text,value){const option=s.node("option",text);option.value=value;return option;};
 const source=fs.readFileSync("vghks_bot/static/analysis.js","utf8");
 vm.runInContext(source.slice(source.indexOf("function surgeryDestination("),source.indexOf("function collectProposals(")),c);
 vm.runInContext(source.slice(source.indexOf("function previewNode("),source.indexOf('$$("[data-analysis-close]"')),c);
 const fields={mrn:"P1",name:"合成病人",date:"2026-11-03",hospital:"高榮"};
 const card=c.surgeryCard({id:"OP1",fields,schedules:[],sources:[
  {record_id:"SOAP1",date:"2026-10-11",excerpt:"來源一",version:"SOAP_VERSION_A"},
  {record_id:"SOAP2",date:"2026-10-12",excerpt:"來源二",source_version:"SOAP_VERSION_B"}]},
 {mrn:"病歷號",name:"姓名",date:"刀日",hospital:"醫院"});
 const versions=card.all().filter(node=>node.tagName==="pre").map(node=>node.textContent);
 assert.ok(versions.includes("SOAP 版本：SOAP_VERSION_A"));assert.ok(versions.includes("SOAP 版本：SOAP_VERSION_B"));
 const preview=c.previewNode({id:"PREVIEW",spreadsheet_id:"synthetic",can_apply:true,source_versions:[
  {record_id:"SOAP1",version:"SOAP_VERSION_A"},{record_id:"SOAP2",source_version:"SOAP_VERSION_B"}],items:[
  {id:"OP1",status:"ready",action:"move",fields,changes:[{label:"刀日",before:"2026-10-18",after:"2026-11-03"}],
   sync_id:"SCHEDULE_UID",previous_position:"202610!2",position:"202611!3",
   previous_route:{date:"2026-10-18",hospital:"聯醫",block_sync_id:"OLD_BLOCK_UID"},
   route:{date:"2026-11-03",hospital:"高榮",block_sync_id:"NEW_BLOCK_UID"}},
  {id:"FU2",status:"ready",action:"keep",fields:{mrn:"P2",name:"追蹤病人"},changes:[],sync_id:"FU_UID",position:"FU!8"}]});
 const details=preview.querySelectorAll("details"),identity=details.filter(node=>node.firstElementChild?.textContent==="同步識別與位置");
 assert.equal(identity.length,2);assert.ok(identity.every(node=>node.open===false),"identity details expanded by default");
 const entries=node=>Object.fromEntries(node.querySelectorAll("tr").map(row=>[row.children[0].textContent,row.children[1].textContent]));
 const op=entries(identity[0]),fu=entries(identity[1]);assert.equal(op.ScheduleSyncId,"SCHEDULE_UID");assert.equal(op["原位置"],"202610!2");assert.equal(op["目的位置"],"202611!3");
 assert.equal(op["原區塊"],"2026-10-18 · 聯醫 · 區塊 ID：OLD_BLOCK_UID");
 assert.equal(op["目的區塊"],"2026-11-03 · 高榮 · 區塊 ID：NEW_BLOCK_UID");
 assert.deepEqual(fu,{ScheduleSyncId:"FU_UID","目的位置":"FU!8"},"FU gained irrelevant surgery route details");
 const batch=details.find(node=>node.firstElementChild?.textContent==="本次預覽的 SOAP 來源版本 · 2 筆");
 assert.ok(batch);assert.equal(batch.open,false);assert.deepEqual(batch.querySelectorAll("pre").map(node=>node.textContent),[
  "SOAP1 · SOAP_VERSION_A","SOAP2 · SOAP_VERSION_B"]);
}
module.exports={surface,deferred,tick};
if(require.main===module)(async()=>{await noteTests();await externalNoteTests();await activityTests();await profileTests();await historyLoadingTests();layoutTests();await toolSelectionTests();await handoffTests();await sheetSyncTests();await destinationTests();await fuTests();syncDetailsTests();process.stdout.write("Review UI race, hierarchy, patient profile, loading, viewport, handoff, FU and sheet sync checks passed\n");})().catch(error=>{console.error(error);process.exitCode=1;});
