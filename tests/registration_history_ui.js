"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.value="";this.events={};this.dataset={};this.className="";this.ownText="";this.classList={add:name=>{this.className+=" "+name;}};}
 set textContent(value){this.ownText=String(value);this.children=[];}get textContent(){return this.ownText+this.children.map(child=>child.textContent).join(" ");}
 append(...values){this.children.push(...values);}prepend(...values){this.children.unshift(...values);}replaceChildren(...values){this.ownText="";this.children=[...values];}
 setAttribute(name,value){this[name]=value;}addEventListener(name,run){this.events[name]=run;}
 all(){return this.children.flatMap(child=>[child,...child.all()]);}
 querySelector(selector){return this.all().find(child=>selector.startsWith(".")?child.className.split(/\s+/).includes(selector.slice(1)):child.tagName===selector)||null;}
}
const node=(tag,text,cls)=>{const result=new Element(tag);if(text!==undefined)result.textContent=text;if(cls)result.className=cls;return result;};
const context={window:{},node,Node:Element};vm.createContext(context);
const bot=fs.readFileSync("vghks_bot/static/bot.js","utf8"),start=bot.indexOf("function table(");
vm.runInContext(bot.slice(start,bot.indexOf("\n",start)),context);
vm.runInContext(fs.readFileSync("vghks_bot/static/registration-history.js","utf8"),context);
const container=node("div"),rows=[
 {visit_date:"2026-10-15",section_code:"70",section_name:"眼科",room:"02",sequence_no:"001",expected_time:"09:30",registered_at:"115/10/01 08:30",registered_by:"MWEB",no_show:null,notes:'<img src=x onerror="alert(1)">',columns:{"新增欄位":"保留值","空欄":""}},
 {visit_date:"2026-10-14",sequence_no:"002",no_show:null},
 {visit_date:"2026-10-10",sequence_no:"007",status:"院方狀態",no_show:true,registered_by:"OPD_SYN",notes:"回診"},
 {visit_date:"2026-10-10",sequence_no:"007",status:"院方狀態",no_show:null,registered_by:"OPD_SYN",notes:"回診"},
 {visit_date:"2026-10-09",sequence_no:"010",status:"來源取消狀態",cancelled_at:"115/10/08 09:00",cancelled_by:"STAFF_SYN",no_show:false},
 {visit_date:null,sequence_no:"011",no_show:"true",columns:{"日期原文":"不明"}},
];
const original=JSON.stringify(rows);
context.window.RegistrationHistoryUI.append(container,{payload:rows,queried_on:"2026-10-14",registration_schema:1},"2026-10-15");
const all=()=>container.all(),summary=()=>all().find(element=>element.role==="status").textContent;
const headings=()=>all().filter(element=>element.tagName==="h3").map(element=>element.textContent);
assert.deepEqual(headings(),["今天及未來（2）","過去（3）","日期不明（1）"]);
assert.match(summary(),/顯示 6 \/ 6 筆.*未到診 1 筆/);
assert.doesNotMatch(container.textContent,/未知|未提供|未註明|原始欄位|新增欄位|保留值|（空白）|尚無自行掛號/);
assert.equal(all().filter(element=>element.tagName==="details").length,0,"raw fields remain in the patient view");
for(const value of ["預計 09:30","70 眼科","診間 02","001","115/10/01 08:30","MWEB","來源取消狀態","115/10/08 09:00","STAFF_SYN",'<img src=x onerror="alert(1)">'])assert.ok(container.textContent.includes(value),value);
assert.equal(all().filter(element=>element.tagName==="img").length,0,"source text became executable markup");
const cancellations=all().filter(element=>element.className==="registration-cancelled");
assert.equal(cancellations.length,1);assert.equal(cancellations[0].textContent,"來源取消狀態");
assert.equal((container.textContent.match(/來源取消狀態/g)||[]).length,1,"cancellation was shown twice");
const bodyRows=all().filter(element=>element.tagName==="tbody").flatMap(body=>body.children);
const blank=bodyRows.find(row=>row.children[2].textContent==="002");
for(const index of [3,4,5,6])assert.equal(blank.children[index].textContent,"","missing field generated a placeholder");
assert.ok(all().filter(element=>element.role==="region").every(element=>element.tabIndex===0));
assert.ok(all().filter(element=>element.tagName==="th").every(element=>element.scope==="col"));
const filter=all().find(element=>element.tagName==="select"),query=all().find(element=>element.tagName==="input");
assert.equal(filter.children.find(option=>option.value==="unknown").textContent,"未標示");
filter.value="no-show";filter.events.change();assert.match(summary(),/顯示 1 \/ 6 筆.*未到診 1 筆/);
assert.ok(!container.textContent.includes("來源取消狀態"));
filter.value="unknown";filter.events.change();assert.match(summary(),/顯示 5 \/ 6 筆.*未到診 0 筆/);
filter.value="all";query.value="opd_syn";query.events.input();assert.match(summary(),/顯示 2 \/ 6 筆/);
query.value="保留值";query.events.input();assert.match(summary(),/顯示 0 \/ 6 筆/,"search matched a hidden raw field");
query.value="true";query.events.input();assert.match(summary(),/顯示 0 \/ 6 筆/,"search matched a non-visible attendance value");
query.value="NO_MATCH";query.events.input();assert.match(summary(),/顯示 0 \/ 6 筆/);assert.ok(container.textContent.includes("沒有符合紀錄"));
assert.equal(JSON.stringify(rows),original,"display changed the saved source payload");
const legacy=node("div");context.window.RegistrationHistoryUI.append(legacy,{payload:[{sequence_no:"003"}]},"2026-10-14");
assert.doesNotMatch(legacy.textContent,/舊版保存資料|未知|未提供/);assert.match(legacy.textContent,/日期不明（1）/);
const cancelled=node("div");context.window.RegistrationHistoryUI.append(cancelled,{payload:[
 {visit_date:"2026-10-14",sequence_no:"001",status:"已取消",cancelled_at:"115/10/13 08:00"},
 {visit_date:"2026-10-14",sequence_no:"002",status:"保留原文",cancelled_by:"STAFF_SYN"},
],registration_schema:1},"2026-10-14");
assert.deepEqual(cancelled.all().filter(element=>element.className==="registration-cancelled").map(element=>element.textContent),["已取消","已取消"]);
assert.equal((cancelled.textContent.match(/已取消/g)||[]).length,2,"derived cancellation duplicated the source status");
assert.match(cancelled.textContent,/保留原文/);
const empty=node("div");context.window.RegistrationHistoryUI.append(empty,{payload:[],registration_schema:1},"2026-10-14");
assert.match(empty.textContent,/顯示 0 \/ 0 筆/);assert.match(empty.textContent,/沒有紀錄/);assert.doesNotMatch(empty.textContent,/舊版保存資料/);
assert.ok(fs.readFileSync("vghks_bot/static/bot.html","utf8").includes('src="/registration-history.js"'));
console.log("Registration history UI: visible fields, blank missing values, cancellation, filters and raw payload preservation passed.");
async function refreshChecks(){
 const elements=new Map(["dataDialog","dataDialogActions","dataTitle","dataContent"].map(id=>["#"+id,node("div")]));
 const $=selector=>elements.get(selector)||null,modal=$("#dataDialog"),actions=$("#dataDialogActions"),content=$("#dataContent");
 modal.open=false;modal.showModal=()=>{modal.open=true;};
 const saved={payload:rows,registration_schema:1,queried_on:"2026-10-14",updated_at:"2026-10-14T08:00:00+08:00"};
 const calls=[];let number=0,status="running",readGate=null,readFailure=false,startFailure=false,detailFailure=false;
 const c={$ ,node,Node:Element,window:{RegistrationHistoryUI:context.window.RegistrationHistoryUI,ReviewHistoryUI:{dismiss(){}},ReviewNotesUI:{load:async()=>{}}},
  root:{read_only:false,today:"2026-10-14"},work:{online:true},epoch:0,page:"review",currentReview:"R1",extensionContext:null,extensionTask:"",
  Choices:{sync(){}},setPatientDialogContext(){},loadReview:async()=>{},JobProgress:{publish(){}},
  inProgress:new Set(["queued","running","cancelling"]),labels:{completed:"完成",running:"執行中"},
  time:value=>value||"",empty:message=>node("p",message),
  ClinicalUI:{loading:message=>node("div",message,"clinical-loading")},
  btn:(label,run)=>{const button=node("button",label);button.run=run;return button;},
  setIcon:(button,name,label)=>{button.dataset.icon=name;button.setAttribute("aria-label",label);},
  api:async(path,values)=>{
   calls.push({path,values});
   if(path==="/extensions/read"){if(readGate)await readGate.promise;if(readFailure)throw new Error("synthetic read failure");return {data:saved};}
   if(path==="/tasks/start"){if(startFailure)throw new Error("synthetic start failure");return {task_id:"REG"+(++number)};}
   if(path.startsWith("/tasks/detail")){if(detailFailure)throw new Error("synthetic detail failure");return {id:"REG"+number,kind:"registrations",mrn:"SYN",status,name:"掛號紀錄",...(!path.includes("summary=1")?{items:[saved]}:{})};}
   throw new Error(path);
  }};
 vm.createContext(c);
 vm.runInContext(bot.slice(start,bot.indexOf("\n",start)),c);
 vm.runInContext(bot.match(/function dialog\(id\)\{[^\n]+/)[0],c);
 vm.runInContext(bot.slice(bot.indexOf("async function extension("),bot.indexOf("const eyeNumericPatterns=")),c);
 vm.runInContext(bot.slice(bot.indexOf("function renderExtension("),bot.indexOf("const libraryFilters=")),c);
 vm.runInContext(bot.split("\n").find(line=>line.startsWith('$("#dataDialog").addEventListener("close",')),c);
 const starts=()=>calls.filter(call=>call.path==="/tasks/start"),refresh=()=>actions.children[0];
 const pending=c.extension("registrations","SYN");
 assert.equal(refresh().disabled,true,"refresh was enabled during the saved data read");
 assert.equal(refresh().dataset.icon,"refresh");assert.equal(refresh()["aria-label"],"更新掛號紀錄");
 await pending;assert.equal(refresh().disabled,true,"refresh was enabled while its crawler task was running");
 assert.equal(content.all().filter(element=>element.tagName==="button").length,0,"refresh remained in the dialog body");
 status="completed";await c.showExtensionTask("REG1","registrations",false);
 assert.equal(refresh().disabled,false);assert.equal(refresh()["aria-busy"],"false");
 assert.equal(content.all().filter(element=>element.tagName==="button").length,0);
 status="running";await refresh().run();assert.equal(starts().length,2);assert.equal(starts().at(-1).values.force,true);
 for(const state of ["offline","readonly"]){
  c.work.online=state!=="offline";c.root.read_only=state==="readonly";
  const before=starts().length;await c.extension("registrations","SYN");assert.equal(refresh().disabled,true,state);
  await refresh().run();assert.equal(starts().length,before,state+" forced a remote query");
 }
 c.work.online=true;c.root.read_only=false;
 let resolve;readGate={promise:new Promise(done=>{resolve=done;})};
 const reading=c.extension("registrations","SYN"),stale=refresh(),before=starts().length;
 c.dialog("dataDialog");assert.equal(actions.children.length,0,"opening a general dialog retained the refresh action");
 resolve();await reading;readGate=null;assert.equal(starts().length,before,"stale local read started a query");
 const oldCalls=calls.length;await stale.run();assert.equal(calls.length,oldCalls,"stale refresh revived a previous patient context");
 status="completed";await c.extension("registrations","SYN");const beforeAccountChange=refresh();c.epoch++;
 const accountCalls=calls.length;await beforeAccountChange.run();assert.equal(calls.length,accountCalls,"stale account refresh submitted a query");
 modal.open=false;modal.events.close();assert.equal(actions.children.length,0);assert.equal(c.extensionContext,null);
 for(const failure of ["read","start","detail"]){
  readFailure=failure==="read";startFailure=failure==="start";detailFailure=failure==="detail";
  await assert.rejects(c.extension("registrations","SYN"),/synthetic .* failure/);
  assert.equal(refresh().disabled,false,failure+" failure left refresh disabled");
  assert.equal(content.querySelector(".clinical-loading"),null,failure+" failure left a loading indicator");
 }
 assert.equal(JSON.stringify(rows),original,"refresh changed the saved source payload");
 console.log("Registration dialog refresh: header placement, loading, offline, read-only and stale context checks passed.");
}
refreshChecks().catch(error=>{console.error(error);process.exitCode=1;});
