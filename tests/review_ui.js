"use strict";
// Exercise asynchronous saves/deletes and hierarchy navigation using local data.
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const deferred=()=>{let resolve,reject;const promise=new Promise((done,fail)=>{resolve=done;reject=fail;});return {promise,resolve,reject};};
const tick=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
function surface(){
 const nodes=new Map();let context;
 class Element{
  constructor(tag="div"){this.tagName=tag;this.children=[];this.dataset={};this.value="";this.textContent="";this.disabled=false;this.hidden=false;this.scrollTop=0;this.events=new Map();this.className="";this.parentElement=null;
   this.classList={add:name=>{this.className+=" "+name;},toggle(){}};}
  set id(value){this._id=value;nodes.set("#"+value,this);}get id(){return this._id;}
  get firstElementChild(){return this.children[0]||null;}
  get isConnected(){return this.parentElement?this.parentElement.isConnected:[...nodes.values()].includes(this);}
  append(...values){for(const child of values){child.parentElement=this;this.children.push(child);}}
  prepend(...values){for(const child of values)child.parentElement=this;this.children.unshift(...values);}
  remove(){if(this.parentElement){this.parentElement.children=this.parentElement.children.filter(child=>child!==this);this.parentElement=null;}}
  before(...values){if(this.parentElement){const index=this.parentElement.children.indexOf(this);for(const child of values)child.parentElement=this.parentElement;this.parentElement.children.splice(index,0,...values);}}
  replaceChildren(...values){for(const child of this.children)child.parentElement=null;this.children=[];this.append(...values);}
  all(){return this.children.flatMap(child=>[child,...child.all()]);}
  contains(value){return value===this||this.all().includes(value);}
  matches(selector){if(selector.startsWith("#"))return this.id===selector.slice(1);if(selector.startsWith("."))return this.className.split(/\s+/).includes(selector.slice(1));if(selector==="[data-activity-focus]")return "activityFocus" in this.dataset;return this.tagName===selector;}
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
  if(path.endsWith("/read"))return {task_id:values.task_id,patients:["P1","P2"].map(mrn=>({mrn,text:saved.get(mrn)||""}))};
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
}
async function historyLoadingTests(){
 const s=surface(),c=s.context,$=s.$;
 c.window.addEventListener=()=>{};c.document.createElement=tag=>c.node(tag);
 s.run("vghks_bot/static/clinical-ui.js");c.ClinicalUI=c.window.ClinicalUI;
 c.dialog=()=>{$("#dataDialog").open=true;};c.setPatientDialogContext=()=>{};
 c.reviewPatientIdentity=()=>({name:"合成病人"});c.extensionTask="";
 c.JobProgress={pending(){},publish(){}};c.work.online=true;
 c.appendNumericTables=(content,tables)=>content.append(c.node("p",tables[0]?.title||"numeric result"));
 let readGate=null,startGate=null,readFailure=null,startFailure=null,status="running",complete=false,data=null,starts=0;
 c.api=async path=>{
  if(path==="/reviews/history/read"){if(readGate)await readGate.promise;if(readFailure)throw readFailure;return {data,complete};}
  if(path==="/tasks/start"){starts++;if(startGate)await startGate.promise;if(startFailure)throw startFailure;return {task_id:"H1"};}
  if(path.startsWith("/tasks/detail"))return {status,updated_at:status,message:"合成查詢結果"};
  throw new Error(path);
 };
 s.run("vghks_bot/static/review-history.js");const ui=c.window.ReviewHistoryUI;
 const spinner=()=>$("#dataContent").querySelector(".clinical-loading");
 readGate=deferred();const reading=ui.open("numeric","P1");
 assert.ok(spinner(),"local read did not show loading");assert.equal(spinner().role,"status");
 assert.equal(spinner().querySelector(".clinical-loading-icon")["aria-hidden"],"true");
 assert.match(spinner().querySelector(".clinical-loading-label").textContent,/讀取/);
 readGate.resolve();await reading;readGate=null;assert.ok(spinner(),"running query lost loading");
 status="completed";complete=true;data={tables:[{title:"合成數值結果"}]};await ui.poll();
 assert.equal(spinner(),null,"completed report kept loading");assert.ok($("#dataContent").children.some(child=>child.textContent==="合成數值結果"));
 status="running";complete=false;data=null;await ui.open("numeric","P1");
 status="completed";complete=true;data={tables:[{title:"恢復後的合成數值"}]};readFailure=new Error("synthetic polling failure");
 await assert.rejects(ui.poll(),/polling failure/);assert.equal(spinner(),null,"polling read failure kept loading");
 await assert.rejects(ui.poll(),/polling failure/);assert.equal($("#dataContent").querySelectorAll(".history-load-error").length,1,"repeated failure duplicated errors");
 readFailure=null;await ui.poll();assert.ok($("#dataContent").children.some(child=>child.textContent==="恢復後的合成數值"),"failed read consumed task update before result loaded");
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
  if(path==="/tools/state/save"){modules[values.module]={set_id:values.set_id};return {modules};}
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
 c.root.read_only=true;await ui.open("cataract",groups[2]);const writes=requests.filter(request=>request.path==="/tools/state/save").length;
 await $("#toolClearPatients").emit("click");assert.equal(c.selectedSet.members.length,0);
 assert.equal(requests.filter(request=>request.path==="/tools/state/save").length,writes,"readonly clearing wrote database");
 assert.equal(JSON.stringify(groups),original);ui.reset();
}
(async()=>{await noteTests();await activityTests();await historyLoadingTests();layoutTests();await toolSelectionTests();process.stdout.write("Review UI race, hierarchy, loading, viewport and tool selection checks passed\n");})().catch(error=>{console.error(error);process.exitCode=1;});
