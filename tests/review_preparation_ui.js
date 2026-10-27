"use strict";
const assert=require("node:assert/strict");
const {surface,deferred,tick}=require("./review_ui.js");
const clone=value=>JSON.parse(JSON.stringify(value));
const text=element=>[element.textContent,...element.all().map(child=>child.textContent)].join(" ");
const resources=["soap","numeric","orders","profile","reports","attachments"];

function setup({state="idle",selected=["P1","P3"],total=95}={}){
 const s=surface(),c=s.context,$=s.$,Element=s.node("div").constructor;
 const calls=[],published=[],messages=[],actions=[],readGates=[],actionGates=[],diagnostics=[];
 const selectedPatients=selected.map(mrn=>({mrn})),saved=new Map();
 let cleared=0,number=0;
 c.epoch=1;c.currentReview="R1";c.work={online:true};c.root={read_only:false};
 c.document.body=s.node("body");
 Object.defineProperty(Element.prototype,"elements",{get(){return Object.fromEntries(this.all().filter(child=>child.name).map(child=>[child.name,child]));}});
 Object.defineProperty(Element.prototype,"options",{get(){return this.children;}});
 Element.prototype.reportValidity=function(){return this.all().filter(child=>child.required&&!child.disabled).every(child=>Number.isFinite(Number(child.value))&&Number(child.value)>=Number(child.min)&&Number(child.value)<=Number(child.max));};
 Element.prototype.showModal=function(){this.open=true;this.previousFocus=c.document.activeElement;this.querySelectorAll("button").find(child=>child.autofocus)?.focus();};
 Element.prototype.close=function(){this.open=false;void this.emit("close");this.previousFocus?.focus();};
 c.Option=function(label,value){const option=s.node("option",label);option.value=value;return option;};
 c.say=(message,error)=>messages.push({message,error});
 c.act=(run,control)=>{
  if(control?.disabled||control?.dataset.busy)return Promise.resolve();
  if(control){control.disabled=true;control.dataset.busy="true";}
  const promise=Promise.resolve().then(run).catch(error=>{if(error.name!=="AbortError")c.say(error.message,true);}).finally(()=>{if(control){control.disabled=false;delete control.dataset.busy;}});
  actions.push(promise);return promise;
 };
 c.btn=(label,run,cls)=>{
  const button=s.node("button",label,cls);button.type="button";button.run=run;
  button.addEventListener("click",()=>c.act(run,button));return button;
 };
 c.window.ReviewHandoff={patients:()=>selectedPatients,clear(){cleared++;selectedPatients.length=0;}};
 c.window.JobProgress={publish:(value,account)=>published.push({value:clone(value),account})};
 c.openLookupDiagnostics=id=>diagnostics.push(id);
 const key=(account,task)=>JSON.stringify([account,task]);
 const idle=task=>({review_task_id:task,state:"idle",reason:"",progress:{done:0,total:0,unit:"項準備"}});
 function summary(task="R1",nextState="running",extra={}){
  return {review_task_id:task,preparation_id:"PREP-"+task,name:"背景準備 "+task,state:nextState,
   scope_count:total,done_count:2,failed_count:0,blocked_count:0,revision:++number,
   options:{soap:true,numeric:false,orders:false,profile:false,reports:false,attachments:false,max_reports:3,max_attachments:10},
   progress:{done:2,total,unit:"項準備",stage:"本機合成準備"},...extra};
 }
 function setSummary(value,account=c.account){saved.set(key(account,value.review_task_id),clone(value));return value;}
 if(state!=="idle")setSummary(summary("R1",state));
 c.api=async(path,values)=>{
  const account=c.account,request=clone(values);calls.push({path,values:request,account,epoch:c.epoch});
  if(path==="/reviews/preparation/read"){
   const gate=readGates.find(value=>!value.used&&(!value.task||value.task===values.review_task_id));
   if(gate){gate.used=true;await gate.promise;}
   return clone(gate?.response||saved.get(key(account,values.review_task_id))||idle(values.review_task_id));
  }
  if(path==="/reviews/preparation/action"){
   const gate=actionGates.find(value=>!value.used);if(gate){gate.used=true;await gate.promise;}
   const previous=saved.get(key(account,values.review_task_id));
   const result=gate?.response||summary(values.review_task_id,values.action==="pause"?"paused":"queued",{
    ...(previous||{}),state:values.action==="pause"?"paused":"queued",options:values.options||previous?.options,
    reason:values.action==="pause"?"目前讀取完成後停止":""});
   setSummary(result,account);return clone(result);
  }
  throw new Error("Preparation UI attempted an unrelated/SDK request: "+path);
 };
 c.openReview=async id=>{c.currentReview=id;s.ui.context({id},{total});await s.ui.read();};
 s.run("vghks_bot/static/review-preparation.js");s.ui=c.window.ReviewPreparationUI;
 function gate(queue,task){const value={...deferred(),task};queue.push(value);return value;}
 async function settle(){await tick();await Promise.all([...actions]);await tick();}
 async function submit(){await $("#reviewPreparationForm").emit("submit");await settle();}
 async function click(label,container=$("#reviewPreparation")){
  const button=container.querySelectorAll("button").find(value=>value.textContent===label);assert.ok(button,"missing action: "+label);
  await button.emit("click");await settle();return button;
 }
 async function escape(){const panel=$("#reviewPreparationDialog");await panel.emit("cancel");panel.close();await tick();}
 return {...s,calls,published,messages,diagnostics,selectedPatients,setSummary,summary,submit,click,settle,escape,
  readGate:task=>gate(readGates,task),actionGate:()=>gate(actionGates),
  starts:()=>calls.filter(call=>call.path.endsWith("/action")),clearCount:()=>cleared,
  initialize:async()=>{s.ui.context({id:c.currentReview},{total});await s.ui.read();await tick();}};
}

async function explicitScopeOptionsAndBudgets(){
 const s=setup(),{ui,context:c,$}=s;await s.initialize();
 assert.equal(s.calls.length,1,"initial status read was not shared");assert.equal(s.starts().length,0);
 await ui.open();const panel=$("#reviewPreparationDialog"),form=$("#reviewPreparationForm"),elements=form.elements;
 assert.equal(panel.tagName,"dialog");assert.equal(panel.open,true);assert.equal(panel["aria-labelledby"],"reviewPreparationTitle");
 assert.equal($("#reviewPreparationScope").value,"selected");assert.match($("#reviewPreparationBudget").textContent,/2 位/);
 assert.equal(elements.soap.checked,true);assert.equal(elements.numeric.checked,false);
 assert.equal(elements.max_reports.disabled,true);assert.equal(elements.max_attachments.disabled,true);
 elements.attachments.checked=true;await elements.attachments.emit("change");
 assert.equal(elements.reports.checked,true);assert.equal(elements.orders.checked,true);
 assert.equal(elements.max_reports.disabled,false);assert.equal(elements.max_attachments.disabled,false);
 elements.reports.checked=false;await elements.reports.emit("change");assert.equal(elements.attachments.checked,false);
 elements.attachments.checked=true;await elements.attachments.emit("change");elements.orders.checked=false;await elements.orders.emit("change");
 assert.equal(elements.reports.checked,false);assert.equal(elements.attachments.checked,false);
 elements.attachments.checked=true;await elements.attachments.emit("change");
 elements.max_reports.value="5";elements.max_attachments.value="20";await elements.max_attachments.emit("input");
 assert.match($("#reviewPreparationBudget").textContent,/5 份眼科報告.*20 份附件/);
 assert.match($("#reviewPreparationBudget").textContent,/超出上限.*手動開啟/);
 assert.equal(s.starts().length,0,"choosing resources started preparation");await s.submit();
 assert.equal(s.starts().length,1);const request=s.starts()[0];
 assert.deepEqual(request.values,{review_task_id:"R1",action:"start",mrns:["P1","P3"],options:{soap:true,numeric:false,orders:true,profile:false,reports:true,attachments:true,max_reports:5,max_attachments:20}});
 assert.equal(request.account,"ACCOUNT");assert.equal(panel.open,false);
 assert.deepEqual(s.selectedPatients.map(patient=>patient.mrn),["P1","P3"]);assert.equal(s.clearCount(),0);
 await ui.open();$("#reviewPreparationScope").value="all";await $("#reviewPreparationScope").emit("change");
 assert.match($("#reviewPreparationBudget").textContent,/95 位/);await s.submit();
 assert.equal(s.starts().length,2);assert.equal("mrns" in s.starts()[1].values,false,"all scope was reduced to visible/selected patients");
 await ui.open();elements.max_reports.value="6";await s.submit();assert.equal(s.starts().length,2,"invalid report budget was sent");
 elements.max_reports.value="5";for(const resource of resources)elements[resource].checked=false;
 await s.submit();assert.equal(s.starts().length,2);assert.match(s.messages.at(-1).message,/至少選擇/);
 assert.ok(s.calls.every(call=>/^\/reviews\/preparation\/(read|action)$/.test(call.path)));
 assert.equal(c.account,"ACCOUNT");
}

async function emptySelectionAndNativeClose(){
 const s=setup({selected:[]}),{ui,$,context:c}=s;await s.initialize();
 const settings=$("#reviewPreparation").querySelectorAll("button").find(button=>button.textContent==="背景準備");
 assert.equal(settings["aria-haspopup"],"dialog");settings.focus();await ui.open();
 const panel=$("#reviewPreparationDialog"),selection=$("#reviewPreparationScope");
 assert.equal(selection.value,"all");assert.equal(selection.options[1].disabled,true);
 assert.equal(c.document.activeElement.textContent,"關閉","native dialog did not have a safe initial focus");
 await s.escape();assert.equal(panel.open,false);assert.equal(c.document.activeElement,settings);
 assert.equal(s.starts().length,0);assert.equal(s.clearCount(),0);
 s.selectedPatients.push({mrn:"P7"});await ui.open();assert.equal(selection.value,"selected");
 await s.click("關閉",panel);assert.equal(panel.open,false);assert.deepEqual(s.selectedPatients.map(patient=>patient.mrn),["P7"]);
 assert.equal(s.clearCount(),0);assert.equal(s.starts().length,0);
}

async function permissionsAndExplicitRetry(){
 const s=setup({state:"running"}),{ui,context:c,$}=s;await s.initialize();
 await s.click("暫停準備");assert.equal(s.starts().at(-1).values.action,"pause");
 await s.click("繼續準備");assert.equal(s.starts().at(-1).values.action,"resume");
 assert.ok(s.starts().every(call=>!call.values.options&&!call.values.mrns),"pause/resume silently replaced the finite scope");
 const partial=s.summary("R1","partial",{reason:"部分資料已清除",failed_count:1,blocked_count:1,
  errors:[{mrn:"P3",resource:"reports",code:"CACHE_CLEARED",message:"資料已清除；需明確允許重新準備。"}]});
 s.setSummary(partial);ui.poll({review_preparation:partial});
 assert.match(text($("#reviewPreparation")),/重試.*清除/);
 const before=s.starts().length;await ui.progressAction({id:partial.preparation_id},"resume");
 assert.equal(s.starts().length,before,"global retry silently re-fetched cleared data");
 const panel=$("#reviewPreparationDialog");assert.equal(panel.open,true);assert.match(text(panel),/資料已清除/);
 await s.click("重試未完成",$("#reviewPreparationActions"));
 assert.equal(s.starts().length,before+1);assert.equal(s.starts().at(-1).values.action,"retry");assert.equal(panel.open,false);
 assert.deepEqual(s.selectedPatients.map(patient=>patient.mrn),["P1","P3"]);
 c.root.read_only=true;await ui.open();assert.equal($("#reviewPreparationSubmit").disabled,true);
 assert.match(text($("#reviewPreparationResult")),/唯讀/);await s.submit();assert.equal(s.starts().length,before+1);
 await assert.rejects(ui.progressAction({id:partial.preparation_id},"stop"),/唯讀/);
 c.root.read_only=false;c.work.online=false;s.setSummary(s.summary("R1","paused"));await ui.read();await ui.open();
 assert.equal($("#reviewPreparationSubmit").disabled,true);assert.match(text($("#reviewPreparationResult")),/連線/);
 const count=s.starts().length;await s.submit();assert.equal(s.starts().length,count);
 await assert.rejects(ui.progressAction({id:partial.preparation_id},"resume"),/連線|登入/);assert.equal(s.starts().length,count);
 ui.poll({review_preparation:s.summary("R1","running")});
 await ui.progressAction({id:partial.preparation_id},"stop");assert.equal(s.starts().at(-1).values.action,"pause","offline status could not pause an active run");
}

async function compactGlobalProgressAndOtherTaskPause(){
 const s=setup(),{ui,$}=s;await s.initialize();
 const before=s.calls.length,other=s.summary("R2","running",{scope_count:12,done_count:7,failed_count:1,blocked_count:2,
  progress:{done:7,total:36,unit:"項準備",stage:"眼科報告"}});
 assert.equal("members" in other,false);ui.poll({review_preparation:other});await tick();
 assert.ok(s.calls.length<=before+1,"a new global preparation caused unbounded local refreshes");
 assert.ok(s.calls.slice(before).every(call=>call.path==="/reviews/preparation/read"),"compact polling started acquisition work");
 const refreshed=s.calls.length;ui.poll({review_preparation:clone(other)});await tick();
 assert.equal(s.calls.length,refreshed,"unchanged compact polling repeatedly refreshed local preparation data");
 const caption=text($("#reviewPreparation"));assert.match(caption,/另一份檢閱/);assert.match(caption,/12 位/);
 assert.match(caption,/7\s*\/\s*36/);assert.match(caption,/1 項未完成/);assert.match(caption,/2 項待處理/);
 assert.equal(s.published.at(-1).value.id,other.preparation_id);assert.equal(s.published.at(-1).account,"ACCOUNT");
 assert.equal(s.published.at(-1).value.kind,"review_preparation");assert.equal(s.published.at(-1).value.status,"running");
 await s.click("暫停準備");assert.deepEqual(s.starts().at(-1).values,{review_task_id:"R2",action:"pause"});
 await ui.progressAction({id:other.preparation_id},"resume");assert.deepEqual(s.starts().at(-1).values,{review_task_id:"R2",action:"resume"});
 await ui.progressAction({id:other.preparation_id},"debug");assert.deepEqual(s.diagnostics,[other.preparation_id]);
 assert.deepEqual(s.selectedPatients.map(patient=>patient.mrn),["P1","P3"]);
 const switched=setup({state:"running"});await switched.initialize();
 switched.setSummary(switched.summary("R1","paused",{reason:"已更換準備範圍"}));
 const replacement=switched.summary("R2","running");switched.ui.poll({review_preparation:replacement});await tick();
 switched.ui.poll({review_preparation:{...replacement,state:"completed"}});await tick();
 assert.match(text(switched.$("#reviewPreparation")),/已暫停/);
 assert.doesNotMatch(text(switched.$("#reviewPreparation")),/背景準備 · 準備中/,"an inactive replacement resurrected an old cached running state");
}

async function staleReadsCannotAffectAnotherContext(){
 for(const change of [c=>{c.account="OTHER";},c=>{c.currentReview="R2";},c=>{c.epoch++;}]){
  const s=setup(),{ui,context:c,$}=s,gate=s.readGate("R1");
  ui.context({id:"R1"},{total:95});const oldRead=ui.read();await tick();
  change(c);ui.context({id:c.currentReview},{total:9});await ui.read();await ui.open();
  const panel=$("#reviewPreparationDialog"),contents=text($("#reviewPreparation")),published=s.published.length;
  gate.response=s.summary("R1","running");gate.resolve();await oldRead;await tick();
  assert.equal(panel.open,true,"late status response closed another context's dialog");
  assert.equal(text($("#reviewPreparation")),contents);assert.equal(s.published.length,published);
  assert.equal(s.starts().length,0);assert.equal(s.messages.length,0);
 }
 for(const change of [c=>{c.account="OTHER";},c=>{c.currentReview="R2";},c=>{c.epoch++;}]){
  const s=setup(),{ui,context:c}=s,gate=s.readGate("R1");
  ui.context({id:"R1"},{total:95});await tick();change(c);ui.context({id:c.currentReview},{total:9});await ui.read();
  gate.reject(new Error("old account read failed"));await tick();
  assert.equal(s.messages.length,0,"an old context's read failure was shown in the new context");
 }
 for(const change of [c=>{c.account="OTHER";},c=>{c.currentReview="R2";},c=>{c.epoch++;}]){
  const s=setup(),{ui,context:c,$}=s;await s.initialize();
  const gate=s.readGate("R1"),opening=s.click("背景準備");await tick();
  change(c);ui.context({id:c.currentReview},{total:9});await ui.read();await ui.open();
  gate.reject(new Error("old dialog read failed"));await opening;
  assert.equal(s.messages.length,0,"an abandoned dialog read error was shown in the new context");
  assert.equal($("#reviewPreparationDialog").open,true);
 }
}

async function staleActionsDoNotCloseNewDialogs(){
 for(const change of [c=>{c.account="OTHER";},c=>{c.currentReview="R2";},c=>{c.epoch++;}]){
  const s=setup(),{ui,context:c,$}=s;await s.initialize();await ui.open();
  const gate=s.actionGate(),oldSubmit=s.submit();await tick();assert.equal(s.starts().length,1);
  change(c);ui.context({id:c.currentReview},{total:8});await ui.read();await ui.open();
  const panel=$("#reviewPreparationDialog"),before=text($("#reviewPreparation")),published=s.published.length;
  gate.resolve();await oldSubmit;assert.equal(panel.open,true,"an old action closed a newly opened dialog");
  assert.equal(text($("#reviewPreparation")),before);assert.equal(s.published.length,published);assert.equal(s.messages.length,0);
  assert.deepEqual(s.selectedPatients.map(patient=>patient.mrn),["P1","P3"]);
 }
 for(const change of [c=>{c.account="OTHER";},c=>{c.currentReview="R2";},c=>{c.epoch++;}]){
  const s=setup(),{ui,context:c,$}=s;await s.initialize();await ui.open();
  const gate=s.actionGate(),sending=s.submit();await tick();
  change(c);ui.context({id:c.currentReview},{total:8});await ui.read();await ui.open();
  gate.reject(new Error("old action failed"));await sending;
  assert.equal(s.messages.length,0,"an abandoned action error was shown in the new context");
  assert.equal($("#reviewPreparationDialog").open,true);assert.equal(s.published.length,0);
 }
 const s=setup(),{ui,$}=s;await s.initialize();await ui.open();
 const gate=s.actionGate();gate.response=s.summary("WRONG-TASK","queued");const sending=s.submit();await tick();gate.resolve();await sending;
 assert.equal($("#reviewPreparationDialog").open,true);assert.equal(s.published.length,0);assert.match(s.messages.at(-1).message,/範圍不符/);
 const same=setup();await same.initialize();await same.ui.open();const failure=same.actionGate(),submitting=same.submit();await tick();
 failure.reject(new Error("current action failed"));await submitting;
 assert.equal(same.$("#reviewPreparationDialog").open,true);assert.match(same.messages.at(-1).message,/current action failed/);
}

(async()=>{
 await explicitScopeOptionsAndBudgets();await emptySelectionAndNativeClose();await permissionsAndExplicitRetry();
 await compactGlobalProgressAndOtherTaskPause();await staleReadsCannotAffectAnotherContext();await staleActionsDoNotCloseNewDialogs();
 process.stdout.write("Review preparation: explicit finite scopes, local status reads, budgets, pause/retry, permissions and stale-context isolation passed\n");
})().catch(error=>{console.error(error);process.exitCode=1;});
