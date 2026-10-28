"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const {surface,deferred,tick}=require("./review_ui.js");
const settle=async()=>{for(let index=0;index<80;index++)await Promise.resolve();};
const samples=[];

function setup(count=1000){
 const s=surface(),c=s.context,$=s.$,calls=[],notes=[],renders=[],timers=new Map(),documentEvents=new Map();let timerId=0;
 c.setTimeout=(run,delay)=>{const id=++timerId;timers.set(id,{run,delay});return id;};c.clearTimeout=id=>timers.delete(id);
 c.document.hidden=false;c.document.addEventListener=(name,run)=>documentEvents.set(name,run);
 $("#reviewPatients").clientHeight=580;
 const panel=$("#reviewPatientPanel"),header=s.node("div",undefined,"review-patient-header"),actions=s.node("div",undefined,"review-selection-toolbar"),summary=s.node("div",undefined,"review-list-summary");
 actions.append($("#reviewHandoffSelected"),$("#reviewSelectionClear"));summary.append($("#reviewSelectionCount"),$("#reviewPager"));header.append(actions,summary);panel.append(header,$("#reviewPatients"));
 c.page="review";c.epoch=1;c.currentReview="R1";c.reviewPatient="";c.reviewData=null;c.reviewSignature="";
 c.work={tasks:[],revisions:{profile:0,review_note:0,patient_tags:0,registrations:0}};
 c.reviewFilters=()=>({q:$("#reviewQuery").value,search_mode:$("#reviewSearchMode").value||"text",tag:$("#reviewTag").value});
 c.ClinicalUI={loading:label=>s.node("div",label,"clinical-loading")};
 c.badges=items=>s.node("span",JSON.stringify(items||[]));
 c.check=(label,checked,run)=>{const input=s.node("input");input.checked=checked;input.setAttribute("aria-label",label);input.run=run;return input;};
 const selection=new Map();c.window.ReviewHandoff={updatePatients(){},has:mrn=>selection.has(mrn),toggle:(patient,checked)=>{if(checked)selection.set(patient.mrn,{mrn:patient.mrn,name:patient.name});else selection.delete(patient.mrn);}};
 let flush=true;const selections=[];
 c.window.ReviewNotesUI={load:async(id,options)=>{notes.push({id,options});await Promise.resolve();},flush:async(...args)=>typeof flush==="function"?flush(...args):flush,sequence:()=>""};
 c.window.FileCompare={context:(...args)=>selections.push(args)};
 c.renderReviewHeader=()=>{};c.window.ToolWorkspace={adoptReview(){}};
 c.renderReviewDetail=()=>{
  const patient=c.window.ReviewPaging.patient();renders.push(patient.mrn);
  const panel=$("#reviewDetail"),soap=s.node("section",patient.records[0]?.soap||"","soap-record"),notice=c.window.ReviewPaging.positionNotice();
  const previous=s.node("button"),next=s.node("button"),note=s.node("textarea");note.id="reviewNoteInput";note.value="synthetic note";
  previous.dataset.reviewMove="-1";next.dataset.reviewMove="1";previous.disabled=!c.window.ReviewPaging.nav(-1);next.disabled=!c.window.ReviewPaging.nav(1);panel.replaceChildren(notice,previous,next,soap,note);
 };
 const source=fs.readFileSync("vghks_bot/static/bot.js","utf8");
 vm.runInContext(source.split("\n").find(line=>line.startsWith("function toggleReviewPatients(")),c);
 vm.runInContext(source.slice(source.indexOf("function renderReviewPatients("),source.indexOf("function soapNode(")),c);
 Object.assign(c.labels,{ready:"已取得",pending:"待處理",error:"未取得"});
 const revisions=new Map(),listGates=new Map(),detailGates=new Map(),invalidPrefetch=new Map();let taskRevision=1,wrongPatient=false,prefetchActive=0,maxPrefetchActive=0;
 const all=Array.from({length:count},(_,index)=>({mrn:"P"+(index+1),name:"合成病人 "+(index+1),status:"ready",dates:["2026-10-01"],auto_tags:[],manual_tags:[],record_count:1,sequence_no:String(index+1).padStart(3,"0")}));
 const selected=values=>(values.q==="none"?[]:values.q==="late"?all.slice(1):values.q==="last"?[all.at(-1)]:all).filter(row=>!values.tag||row.manual_tags.some(tag=>tag.category===values.tag));
 const revision=mrn=>"detail-"+mrn+"-"+(revisions.get(mrn)||0);
 const navigation=(values,mrn)=>{const rows=selected(values),index=rows.findIndex(row=>row.mrn===mrn);return {previous_mrn:index>0?rows[index-1].mrn:null,next_mrn:index>=0?rows[index+1]?.mrn||null:null,position:index<0?null:index,total:rows.length};};
 c.api=async(path,values)=>{
  calls.push({path,values:JSON.parse(JSON.stringify(values))});
  const selectedAccount=c.account,task=values.id;
  if(path==="/reviews/patients"){
   const gate=listGates.get(values.offset)||listGates.get(values.q);if(gate)await gate.promise;
   const rows=selected(values);let offset=Math.min(values.offset||0,Math.max(0,Math.floor((rows.length-1)/40)*40)),focusFound=false;
   if(values.focus_mrn){const index=rows.findIndex(row=>row.mrn===values.focus_mrn);if(index>=0){offset=Math.floor(index/40)*40;focusFound=true;}}
   const patients=JSON.parse(JSON.stringify(rows.slice(offset,offset+40))).map(row=>({...row,data_revision:revision(row.mrn)}));
   return {task:{id:task,status:"completed",revision:taskRevision,member_count:all.length},patients,total:rows.length,all_total:all.length,offset,limit:40,revision:"list-"+taskRevision+"-"+offset,collection_revision:JSON.stringify([selectedAccount,task,taskRevision,values.q,values.search_mode,values.tag,c.work.revisions.profile,c.work.revisions.patient_tags,c.work.revisions.registrations]),focus_found:focusFound,has_results:true,
    patient_revisions:Object.fromEntries(patients.map(patient=>[patient.mrn,revision(patient.mrn)])),selected_revision:revision(values.selected_mrn),selected_navigation:navigation(values,values.selected_mrn)};
  }
  if(path==="/reviews/patient"){
   if(values.prefetch){prefetchActive++;maxPrefetchActive=Math.max(maxPrefetchActive,prefetchActive);}
   try{
    if(detailGates.has(values.mrn))await detailGates.get(values.mrn).promise;
    const row=all.find(row=>row.mrn===values.mrn),prefetchable=row?.status==="ready"&&!row.processing&&!!row.record_count;
    const patient={...row,mrn:wrongPatient?"FOREIGN":values.mrn,records:row.record_count?[{id:"SOAP-"+values.mrn,soap:selectedAccount+" "+task+" "+values.mrn}]:[]};
    let result={task_id:task,patient,revision:revision(values.mrn),...navigation(values,values.mrn)};
    if(values.prefetch){
     result={...result,mrn:values.mrn,prefetchable,patient:prefetchable?patient:null};
     const invalid=invalidPrefetch.get(values.mrn);
     if(invalid==="revision")result.revision="stale";if(invalid==="patient")result.patient={...patient,mrn:"FOREIGN"};if(invalid==="skip"){result.prefetchable=false;result.patient=null;}
    }
    return result;
   }finally{if(values.prefetch)prefetchActive--;}
  }
  throw new Error("Unexpected whole-task read: "+path);
 };
 s.run("vghks_bot/static/review-paging.js");const ui=c.window.ReviewPaging;
 c.selectReviewPatient=mrn=>ui.select(mrn);
 const runTimers=async delay=>{for(const [id,timer] of [...timers])if(timer.delay<=delay){timers.delete(id);timer.run();}await settle();};
 const scroll=async(index,fraction=0)=>{$("#reviewPatients").scrollTop=index*72+fraction;await $("#reviewPatients").emit("scroll");await runTimers(65);};
 const rows=()=>$("#reviewPatients").querySelectorAll(".review-patient-row");
 const hide=hidden=>{c.document.hidden=hidden;documentEvents.get("visibilitychange")?.();};
 const sample={...s,ui,calls,notes,renders,revisions,listGates,detailGates,selections,selection,all,invalidPrefetch,scroll,runTimers,rows,hide,maxPrefetch:()=>maxPrefetchActive,setTaskRevision:value=>taskRevision=value,setFlush:value=>flush=value,setWrongPatient:value=>wrongPatient=value};
 samples.push(sample);return sample;
}

function localReadContract(){
 for(const sample of samples)for(const call of sample.calls){
  assert.ok(["/reviews/patients","/reviews/patient"].includes(call.path),"review list/local prefetch started an automatic clinical request: "+call.path);
  if(call.path==="/reviews/patient")assert.equal(call.values.local_only,true,"patient prefetch allowed a network fallback");
  else assert.equal(call.values.limit,40,"virtual scrolling stopped using bounded metadata batches");
 }
 for(const file of ["bot.html","bot.js","review-paging.js","progress.js"]){
  const text=fs.readFileSync("vghks_bot/static/"+file,"utf8");assert.doesNotMatch(text,/ReviewPreparationUI|review-preparation|review_preparation|reviewPreparation/,"removed background acquisition wiring remains in "+file);
 }
 for(const file of ["review-preparation.js","review-preparation.css"])assert.equal(fs.existsSync("vghks_bot/static/"+file),false);
}

async function compactRailContract(){
 const s=setup(),row=s.all[0];row.name="合成姓名很長仍需完整提供";row.dates=["2026-10-28","2026-10-27","2026-10-26"];
 row.auto_tags=Array.from({length:4},(_,index)=>({category:"AUTO"+index,category_name:"合成眼科分類"+index}));row.manual_tags=[{name:"手動重要分類"}];
 await s.ui.load();
 for(const element of s.rows())assert.equal(element.style.height,"72px","virtual row geometry diverged from the compact rail");
 const button=s.rows()[0].querySelector(".patient-select"),label=button.getAttribute("aria-label");
 assert.match(label,/合成姓名很長仍需完整提供/);assert.match(label,/2026-10-26/);assert.match(label,/合成眼科分類3/);assert.match(label,/手動重要分類/);
 assert.equal(button.title,label,"ellipsis removed complete patient metadata from the tooltip");
 const css=fs.readFileSync("vghks_bot/static/bot.css","utf8");
 assert.match(css,/--review-patient-row-height:72px/);assert.match(css,/grid-template-rows:18px 14px 16px/);
 assert.deepEqual([...css.matchAll(/--review-patient-width:(\d+)px/g)].map(match=>Number(match[1])),[248,232],"an old responsive rule narrowed the expanded rail");
 assert.match(css,/:root\[data-density\] \.review-patients\.is-virtual \.patient-select/,"compact density can override the fixed row spacing");
}

async function persistentHeaderAndAttention(){
 const s=setup(),{context:c,$}=s;s.all[1].status="pending";s.all[2].status="error";s.all[3].processing=true;
 await s.ui.load();
 const header=$("#reviewPatientPanel").children[0],count=$("#reviewSelectionCount"),soap=$("#reviewDetail").querySelector(".soap-record");count.textContent="已選 2 位";
 assert.equal(s.rows()[0].querySelector(".review-patient-id").textContent,"P1","completed status wasted a line in the patient rail");
 assert.doesNotMatch(s.rows()[0].querySelector(".patient-select").title,/已取得/);
 assert.match(s.rows()[1].querySelector(".review-patient-id").textContent,/待處理/);assert.match(s.rows()[2].querySelector(".review-patient-id").textContent,/未取得/);assert.match(s.rows()[3].querySelector(".review-patient-id").textContent,/載入中/);
 s.all[3].processing=false;s.setTaskRevision(2);await s.ui.poll({revision:2,status:"completed"},c.work.revisions);assert.equal(s.rows()[3].querySelector(".review-patient-id").textContent,"P4","loading status did not clear on completion");
 await s.scroll(400);assert.equal($("#reviewPatientPanel").children[0],header);assert.equal($("#reviewPatients").contains(header),false);assert.equal(count.textContent,"已選 2 位");assert.equal(header.contains($("#reviewPager")),true);
 assert.match($("#reviewPager").children[0].textContent,/401/);assert.equal($("#reviewDetail").querySelector(".soap-record"),soap);
 const scroll=$("#reviewPatients").scrollTop,reads=s.calls.length;c.toggleReviewPatients();assert.equal($("#reviewPatientPanel").hidden,true);assert.equal($("#reviewPatients").hidden,true);assert.equal($("#reviewPager").hidden,true);assert.equal($("#reviewPatientsToggle").getAttribute("aria-expanded"),"false");
 c.toggleReviewPatients();assert.equal($("#reviewPatientPanel").hidden,false);assert.equal($("#reviewPatients").scrollTop,scroll);assert.equal($("#reviewPatientPanel").children[0],header);assert.equal(s.calls.length,reads);assert.equal($("#reviewDetail").querySelector(".soap-record"),soap);
 const html=fs.readFileSync("vghks_bot/static/bot.html","utf8"),start=html.indexOf('id="reviewPatientPanel"'),end=html.indexOf("</aside>",start),list=html.indexOf('id="reviewPatients"',start);
 for(const id of ["reviewHandoffSelected","reviewSelectionClear","reviewSelectionCount","reviewPager"]){const position=html.indexOf('id="'+id+'"');assert.ok(position>start&&position<list&&position<end,"patient controls escaped their fixed left header: "+id);}
 assert.match(html,/aria-controls="reviewPatientPanel"/);
 const css=fs.readFileSync("vghks_bot/static/bot.css","utf8");assert.match(css,/\.review-patient-header\{flex:none/);assert.match(css,/\.review-pager\{[^}]*margin:0;min-height:16px/);assert.match(css,/\.review-patient-panel>\.review-patients\{[^}]*flex:1[^}]*height:auto/);
}

async function initialResultLayout(){
 const s=setup(),{context:c,$}=s,layouts=[];c.reviewSetupExpanded=false;$("#reviewPatients").clientHeight=360;
 c.document.body={classList:{toggle(name,on){assert.equal(name,"review-result-compact");layouts.push(on);$("#reviewPatients").clientHeight=on?720:360;}}};
 const source=fs.readFileSync("vghks_bot/static/bot.js","utf8");vm.runInContext(source.split("\n").find(line=>line.startsWith("function updateReviewLayout(")),c);
 c.renderReviewHeader=()=>c.updateReviewLayout();await s.ui.load();
 assert.equal(layouts[0],true,"first result header did not enter the full-height review layout");assert.equal(s.rows().length,14,"initial window used the old rail height before the result layout was applied");assert.match($("#reviewPager").children[0].textContent,/1–10 \/ 1000/);
 c.reviewSetupExpanded=true;await s.ui.load();assert.equal(layouts.at(-1),false,"publishing result metadata overrode the source editor's expanded layout");assert.equal(s.rows().length,9);
}

async function continuousWindow(){
 const s=setup(),{ui,context:c,calls,renders,$}=s;
 await ui.load();assert.ok(c.reviewData.patients.length<=18);assert.equal(c.reviewPatient,"P1");
 assert.equal(calls.filter(call=>call.path==="/reviews/patient").length,1,"initial viewport eagerly loaded other patients' SOAP");
 assert.equal(s.notes[0].options.mrns.length,1,"scroll metadata caused all patients' notes to accumulate");
 assert.equal(s.rows()[0].querySelector(".review-sequence").textContent,"001","initial registration sequence required notes");
 assert.ok(s.rows().length<=18);assert.equal($("#reviewPager").querySelectorAll("button").length,0,"page buttons remain");
 const firstRow=s.rows()[0],soap=$("#reviewDetail").querySelector(".soap-record");
 firstRow.querySelector(".patient-select").focus();$("#reviewDetail").scrollTop=73;
 await ui.poll({revision:1,status:"completed"},c.work.revisions);assert.equal(calls.length,2,"unchanged status triggered another read");
 s.setTaskRevision(2);s.revisions.set("P900",1);await ui.poll({revision:2,status:"completed"},c.work.revisions);
 assert.equal(calls.filter(call=>call.path==="/reviews/patient").length,1,"another patient's update reloaded selected SOAP");
 assert.equal(s.rows()[0],firstRow,"unchanged summary row was rebuilt");assert.equal(c.document.activeElement,firstRow.querySelector(".patient-select"));
 assert.equal($("#reviewDetail").querySelector(".soap-record"),soap);assert.equal($("#reviewDetail").scrollTop,73);
 const note=$("#reviewNoteInput");note.value="still editing";note.focus();const noteCount=s.notes.length,before=renders.length;
 await s.scroll(400,19);assert.equal(c.reviewPatient,"P1");assert.equal(renders.length,before);assert.equal(s.notes.length,noteCount,"scroll loaded notes");
 assert.equal($("#reviewNoteInput"),note);assert.equal(note.value,"still editing");assert.equal(c.document.activeElement,note);
 assert.equal($("#reviewDetail").querySelector(".soap-record"),soap);assert.equal($("#reviewDetail").scrollTop,73);
 assert.ok(s.rows().some(row=>row.dataset.mrn==="P401"));assert.equal($("#reviewPatients").scrollTop,400*72+19);assert.ok(s.rows().length<=18);
 assert.match($("#reviewPager").textContent+$("#reviewPager").children.map(node=>node.textContent).join(""),/401/);
 assert.ok(calls.filter(call=>call.path==="/reviews/patients").length<=4,"thumb jump fetched skipped batches");
 await s.scroll(800);await s.scroll(995);assert.ok(s.rows().some(row=>row.dataset.mrn==="P1000"));assert.ok(s.rows().length<=18);
 const height=$("#reviewPatients").children.filter(row=>row.style.position!=="absolute").reduce((sum,row)=>sum+(parseFloat(row.style.height)||0),0);
 assert.equal(height,1000*72,"full-list scrollbar height changed with loaded windows");
 const listCount=calls.filter(call=>call.path==="/reviews/patients").length;await s.scroll(0);
 assert.ok(calls.filter(call=>call.path==="/reviews/patients").length>listCount,"offscreen summaries grew into an unbounded cache");
 assert.equal(s.rows()[0].dataset.mrn,"P1");assert.equal(s.rows()[0].getAttribute("aria-posinset"),"1");
 assert.equal(s.rows()[0].getAttribute("aria-setsize"),"1000");assert.ok(calls.every(call=>call.path!=="/reviews/results"));
}

async function focusSelectionAndNavigation(){
 const s=setup(),{ui,context:c,$}=s;await ui.load();
 const focused=s.rows()[1],button=focused.querySelector(".patient-select");button.focus();
 await s.scroll(400);assert.equal(c.document.activeElement,button);assert.equal($("#reviewPatients").contains(button),true,"virtualization detached keyboard focus");
 assert.equal(focused.style.position,"absolute");assert.ok(s.rows().length<=18);
 const selectedRow=s.rows().find(row=>row.dataset.mrn==="P401"),checkbox=selectedRow.querySelector(".review-handoff-check");checkbox.run(true);
 await s.scroll(0);assert.equal(s.selection.has("P401"),true);assert.deepEqual(Object.keys(s.selection.get("P401")).sort(),["mrn","name"]);
 await ui.select("P40");assert.equal(ui.nav(1),"P41");await ui.move(1);assert.equal(c.reviewPatient,"P41");assert.equal(ui.patient().mrn,"P41");
 assert.ok(s.rows().some(row=>row.dataset.mrn==="P41"));assert.equal($("#reviewPatients").scrollTop,40*72);
 await ui.move(-1);assert.equal(c.reviewPatient,"P40");assert.equal($("#reviewPatients").scrollTop,39*72);
 const selectedSoap=$("#reviewDetail").querySelector(".soap-record"),before=s.renders.length;
 await s.scroll(100);assert.equal(c.reviewPatient,"P40");assert.equal(s.renders.length,before);assert.equal($("#reviewDetail").querySelector(".soap-record"),selectedSoap);
 assert.equal($("#reviewDetail").querySelector(".review-page-context").hidden,false);
 $("#reviewQuery").value="none";await ui.load({resetPage:true});assert.equal(c.reviewData.total,0);assert.equal(c.reviewPatient,"P40");assert.equal(ui.nav(1),"");
 assert.match($("#reviewDetail").querySelector(".review-page-context").textContent,/不符合此篩選/);
 s.setFlush(false);const oldCount=s.calls.length;await assert.rejects(ui.select("P2"),/備註/);assert.equal(s.calls.length,oldCount);assert.equal(c.reviewPatient,"P40");
}

async function anchorAndOverlayRevision(){
 const s=setup(),{ui,context:c,$}=s;await ui.load();await s.scroll(80,17);
 s.all.splice(0,10);s.setTaskRevision(2);await ui.poll({revision:2,status:"completed"},c.work.revisions);
 assert.equal($("#reviewPatients").scrollTop,70*72+17,"collection update lost the visible MRN anchor");
 assert.ok(s.rows().some(row=>row.dataset.mrn==="P81"));assert.equal(c.reviewPatient,"P1");
 s.all.find(row=>row.mrn==="P81").sequence_no="REGISTERED";c.work.revisions.registrations++;const beforeRegistration=s.calls.length;
 await ui.poll({revision:2,status:"completed"},c.work.revisions);
 assert.equal(s.rows().find(row=>row.dataset.mrn==="P81").querySelector(".review-sequence").textContent,"REGISTERED");
 assert.ok(s.calls.slice(beforeRegistration).some(call=>call.values.focus_mrn==="P81"),"registration refresh did not retain its MRN anchor");
 s.all.find(row=>row.mrn==="P81").sequence_no="";c.window.ReviewNotesUI.sequence=()=>"STALE";c.work.revisions.registrations++;
 await ui.poll({revision:2,status:"completed"},c.work.revisions);assert.equal(s.rows().find(row=>row.dataset.mrn==="P81").querySelector(".review-sequence").textContent,"未提供","old note sequence overrode authoritative blank metadata");
}

async function changingCollectionDuringScroll(){
 const s=setup(),{ui,context:c,$}=s;await ui.load();
 const gate=deferred();s.listGates.set(40,gate);await s.scroll(38,27);
 assert.equal($("#reviewPatients").getAttribute("aria-busy"),"true");assert.match($("#reviewPager").children[0].textContent,/載入中/);
 const soap=$("#reviewDetail").querySelector(".soap-record"),notes=s.notes.length;
 s.all.splice(0,5);s.setTaskRevision(2);gate.resolve();await settle();
 assert.equal($("#reviewPatients").scrollTop,33*72+27,"neighbor-block collection change lost the current MRN anchor");
 assert.ok(s.rows().some(row=>row.dataset.mrn==="P39"));assert.equal(c.reviewPatient,"P1");assert.equal(s.notes.length,notes);assert.equal($("#reviewDetail").querySelector(".soap-record"),soap);
 assert.equal($("#reviewPatients").getAttribute("aria-busy"),"false");assert.ok(s.calls.length<=5,"collection relocation kept reloading indefinitely");
 const polling=setup();await polling.ui.load();const pollGate=deferred();polling.listGates.set(0,pollGate);polling.setTaskRevision(2);
 const refresh=polling.ui.poll({revision:2,status:"completed"},polling.context.work.revisions);await tick();await polling.scroll(400,11);pollGate.resolve();await refresh;
 assert.equal(polling.$("#reviewPatients").scrollTop,400*72+11,"a slow poll restored an obsolete viewport over the user's scroll");assert.ok(polling.rows().some(row=>row.dataset.mrn==="P401"));
}

async function racingReads(){
 const s=setup(),{ui,context:c,$}=s;await ui.load();
 const listGate=deferred();s.listGates.set("late",listGate);$("#reviewQuery").value="late";const oldList=ui.load({resetPage:true});await tick();
 $("#reviewQuery").value="last";await ui.load({resetPage:true});listGate.resolve();await oldList;
 assert.equal(c.reviewData.total,1);assert.equal(s.rows()[0].dataset.mrn,"P1000","late filter response replaced current window");
 $("#reviewQuery").value="";await ui.load({resetPage:true});
 const gate=deferred();s.detailGates.set("P2",gate);const oldDetail=ui.select("P2");await tick();
 await ui.select("P3");gate.resolve();await oldDetail;assert.equal(ui.patient().mrn,"P3");assert.equal(c.reviewPatient,"P3");
 const oldFlush=deferred(),newFlush=deferred();let flushCalls=0;s.setFlush(()=>++flushCalls===1?oldFlush.promise:newFlush.promise);
 const delayedSelection=ui.select("P7");await tick();const latestSelection=ui.select("P8");await tick();newFlush.resolve(true);await latestSelection;oldFlush.resolve(true);await delayedSelection;
 assert.equal(c.reviewPatient,"P8","late note flush selected an older clicked patient");assert.ok(!s.calls.some(call=>call.path==="/reviews/patient"&&call.values.mrn==="P7"));s.setFlush(true);
 const filterFlush=deferred();s.setFlush(()=>filterFlush.promise);const oldSelection=ui.select("P9");await tick();
 $("#reviewQuery").value="last";await ui.load({resetPage:true});filterFlush.resolve(true);await oldSelection;
 assert.equal(c.reviewPatient,"P8","late note flush overrode a newer filter operation");s.setFlush(true);$("#reviewQuery").value="";await ui.load({resetPage:true});
 const scrollGate=deferred();s.listGates.set(400,scrollGate);const scrolling=s.scroll(401);await tick();
 $("#reviewQuery").value="last";await ui.load({resetPage:true});scrollGate.resolve();await scrolling;await settle();
 assert.equal(s.rows()[0].dataset.mrn,"P1000","late scroll response replaced current filter");assert.equal(c.reviewPatient,"P8");
 $("#reviewQuery").value="";await ui.load({resetPage:true});
 const late=deferred();s.detailGates.set("P4",late);const abandoned=ui.select("P4");await tick();
 c.account="OTHER";c.epoch++;c.currentReview="R2";c.reviewPatient="";ui.reset();await ui.load();late.resolve();await abandoned;
 assert.equal(c.reviewData.task.id,"R2");assert.equal(ui.patient().mrn,"P1");assert.match(ui.patient().records[0].soap,/OTHER R2/);
 s.setWrongPatient(true);await assert.rejects(ui.select("P6"),/範圍不符/);assert.equal(ui.patient(),null);
}

async function sharedReadsAndBoundedMemory(){
 const s=setup(),{ui,calls,context:c}=s;
 const gate=deferred();s.listGates.set("",gate);const first=ui.load(),second=ui.load();await tick();
 assert.equal(calls.filter(call=>call.path==="/reviews/patients").length,1,"same local window request was not shared");gate.resolve();await Promise.all([first,second]);
 for(let number=2;number<=10;number++)await ui.select("P"+number);
 const before=calls.filter(call=>call.path==="/reviews/patient").length;await ui.select("P1");
 assert.equal(calls.filter(call=>call.path==="/reviews/patient").length,before+1,"detail cache retained every visited SOAP");
 const previous=calls.length;s.revisions.set("P1",1);s.setTaskRevision(2);await ui.poll({status:"completed",revision:2},c.work.revisions);
 assert.equal(calls.length,previous+2,"selected revision did not refresh its own detail");assert.equal(calls.at(-1).values.mrn,"P1");
 const revisionGate=deferred();s.listGates.set("",revisionGate);const reading=ui.load();await tick();c.work.revisions.profile=1;revisionGate.resolve();await reading;
 const beforeRefresh=calls.length;await ui.poll({status:"completed",revision:2},c.work.revisions);
 assert.equal(calls.length,beforeRefresh+1,"revision arriving during a read was acknowledged without re-reading it");
}

async function nearbyLocalPrefetch(){
 const s=setup(),{ui,calls,context:c,$}=s;await ui.load();await ui.select("P40");
 const soap=$("#reviewDetail").querySelector(".soap-record"),before=s.renders.length,notes=s.notes.length,scroll=$("#reviewPatients").scrollTop,data=c.reviewData,next=ui.nav(1);
 await s.runTimers(180);
 assert.deepEqual(calls.filter(call=>call.values.prefetch).map(call=>call.values.mrn),["P41","P42","P39"],"prefetch exceeded next two / previous one");
 assert.equal(s.maxPrefetch(),1);assert.ok(calls.filter(call=>call.path==="/reviews/patient").every(call=>call.values.local_only===true));
 assert.equal(c.reviewPatient,"P40");assert.equal(ui.nav(1),next);assert.equal(s.notes.length,notes);assert.equal(s.renders.length,before);
 assert.equal($("#reviewDetail").querySelector(".soap-record"),soap);assert.equal($("#reviewPatients").scrollTop,scroll);assert.equal(c.reviewData,data,"prefetch published an offscreen metadata page");
 const bodyCount=calls.filter(call=>call.path==="/reviews/patient").length;await ui.select("P41");
 assert.equal(calls.filter(call=>call.path==="/reviews/patient").length,bodyCount,"nearby complete SOAP was not reused");
 assert.equal(ui.patient().mrn,"P41");
}

async function prefetchCompletenessAndRevision(){
 const s=setup(),{ui,calls}=s;
 s.all[10].status="partial";s.all[11].processing=true;s.all[8].record_count=0;
 await ui.load();await ui.select("P10");await s.runTimers(180);assert.equal(calls.filter(call=>call.values.prefetch).length,0,"partial/pending/empty SOAP was prefetched");
 s.all[10].status="ready";s.all[11].processing=false;s.all[8].record_count=1;
 s.invalidPrefetch.set("P11","skip");s.invalidPrefetch.set("P12","revision");s.invalidPrefetch.set("P9","patient");s.setTaskRevision(2);
 await ui.poll({status:"completed",revision:2},s.context.work.revisions);await s.runTimers(180);
 assert.equal(calls.filter(call=>call.values.prefetch).length,3);
 let before=calls.filter(call=>call.path==="/reviews/patient"&&!call.values.prefetch).length;await ui.select("P11");
 assert.equal(calls.filter(call=>call.path==="/reviews/patient"&&!call.values.prefetch).length,before+1,"server-skipped SOAP entered the complete cache");
 before=calls.filter(call=>call.path==="/reviews/patient"&&!call.values.prefetch).length;await ui.select("P12");
 assert.equal(calls.filter(call=>call.path==="/reviews/patient"&&!call.values.prefetch).length,before+1,"wrong-revision prefetch entered cache");
 before=calls.filter(call=>call.path==="/reviews/patient"&&!call.values.prefetch).length;await ui.select("P9");
 assert.equal(calls.filter(call=>call.path==="/reviews/patient"&&!call.values.prefetch).length,before+1,"wrong-patient prefetch entered cache");
}

async function prefetchCancellationAndSingleLane(){
 const s=setup(),{ui,calls,context:c}=s;await ui.load();
 const gate=deferred();s.detailGates.set("P2",gate);await s.runTimers(180);
 assert.equal(calls.filter(call=>call.values.prefetch).length,1);
 await ui.select("P10");await s.runTimers(180);
 assert.equal(calls.filter(call=>call.values.prefetch).length,1,"a new selection opened a second prefetch lane");
 gate.resolve();await settle();await s.runTimers(180);
 assert.deepEqual(calls.filter(call=>call.values.prefetch).map(call=>call.values.mrn),["P2","P11","P12","P9"]);assert.equal(s.maxPrefetch(),1);
 const before=calls.filter(call=>call.path==="/reviews/patient"&&!call.values.prefetch).length;await ui.select("P2");
 assert.equal(calls.filter(call=>call.path==="/reviews/patient"&&!call.values.prefetch).length,before+1,"abandoned selection's prefetch was reused");
 const hidden=setup();await hidden.ui.load();hidden.hide(true);await hidden.runTimers(180);assert.equal(hidden.calls.filter(call=>call.values.prefetch).length,0,"hidden tab scheduled local prefetch");
 hidden.hide(false);const late=deferred();hidden.detailGates.set("P2",late);await hidden.runTimers(180);hidden.hide(true);late.resolve();await settle();
 assert.equal(hidden.calls.filter(call=>call.values.prefetch).length,1,"hidden tab continued scheduling after an in-flight read");
 const old=setup();await old.ui.load();const stale=deferred();old.detailGates.set("P2",stale);await old.runTimers(180);
 old.context.account="SECOND";old.context.epoch++;old.context.currentReview="R2";old.context.reviewPatient="";old.ui.reset();await old.ui.load();stale.resolve();await settle();
 assert.equal(old.calls.filter(call=>call.values.prefetch).length,1,"late account response scheduled more old-context reads");assert.match(old.ui.patient().records[0].soap,/SECOND R2/);
 const route=setup();await route.ui.load();route.context.page="patients";route.ui.visibility();await route.runTimers(180);assert.equal(route.calls.filter(call=>call.values.prefetch).length,0,"leaving review continued local prefetch");
 c.page="patients";ui.visibility();
}

(async()=>{await continuousWindow();await compactRailContract();await persistentHeaderAndAttention();await initialResultLayout();await focusSelectionAndNavigation();await anchorAndOverlayRevision();await changingCollectionDuringScroll();await racingReads();await sharedReadsAndBoundedMemory();await nearbyLocalPrefetch();await prefetchCompletenessAndRevision();await prefetchCancellationAndSingleLane();localReadContract();process.stdout.write("Review compact continuous list: initial full-height layout, fixed left controls, bounded DOM/cache, MRN anchors and local-only prefetch passed\n");})().catch(error=>{console.error(error);process.exitCode=1;});
