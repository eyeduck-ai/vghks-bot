"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const {surface,deferred,tick}=require("./review_ui.js");

function setup(){
 const s=surface(),c=s.context,$=s.$,calls=[],notes=[],renders=[];
 c.page="review";c.epoch=1;c.currentReview="R1";c.reviewPatient="";c.reviewData=null;c.reviewSignature="";
 c.work={tasks:[],revisions:{profile:0,review_note:0,patient_tags:0}};
 c.reviewFilters=()=>({q:$("#reviewQuery").value,search_mode:$("#reviewSearchMode").value||"text",tag:$("#reviewTag").value});
 c.ClinicalUI={loading:label=>s.node("div",label,"clinical-loading")};
 c.badges=items=>s.node("span",JSON.stringify(items||[]));
 c.check=(label,checked,run)=>{const input=s.node("input");input.checked=checked;input.setAttribute("aria-label",label);input.run=run;return input;};
 c.window.ReviewHandoff={updatePatients(){},has:()=>false,toggle(){}};
 let flush=true,noteReady=false;const selections=[];
 c.window.ReviewNotesUI={load:async(id,options)=>{notes.push({id,options});await Promise.resolve();noteReady=true;},flush:async(...args)=>typeof flush==="function"?flush(...args):flush,sequence:()=>noteReady?"001":""};
 c.window.FileCompare={context:(...args)=>selections.push(args)};
 c.renderReviewHeader=()=>{};c.window.ToolWorkspace={adoptReview(){}};
 c.renderReviewDetail=()=>{
  const patient=c.window.ReviewPaging.patient();renders.push(patient.mrn);
  const panel=$("#reviewDetail"),soap=s.node("section",patient.records[0].soap,"soap-record"),notice=c.window.ReviewPaging.positionNotice();
  const previous=s.node("button"),next=s.node("button");previous.dataset.reviewMove="-1";next.dataset.reviewMove="1";
  previous.disabled=!c.window.ReviewPaging.nav(-1);next.disabled=!c.window.ReviewPaging.nav(1);panel.replaceChildren(notice,previous,next,soap);
 };
 const source=fs.readFileSync("vghks_bot/static/bot.js","utf8");
 vm.runInContext(source.slice(source.indexOf("function renderReviewPatients("),source.indexOf("function soapNode(")),c);
 const revisions=new Map(),listGates=new Map(),detailGates=new Map();let taskRevision=1,wrongPatient=false;
 const all=Array.from({length:100},(_,index)=>({mrn:"P"+(index+1),name:"合成病人 "+(index+1),status:"ready",dates:["2026-10-01"],auto_tags:[],manual_tags:[],record_count:1}));
 const selected=values=>values.q==="none"?[]:values.q==="late"?all.slice(1):values.q==="last"?[all[99]]:all;
 const revision=mrn=>"detail-"+mrn+"-"+(revisions.get(mrn)||0);
 const navigation=(values,mrn)=>{const rows=selected(values),index=rows.findIndex(row=>row.mrn===mrn);return {previous_mrn:index>0?rows[index-1].mrn:null,next_mrn:index>=0?rows[index+1]?.mrn||null:null,position:index<0?null:index,total:rows.length};};
 c.api=async(path,values)=>{
  calls.push({path,values:JSON.parse(JSON.stringify(values))});
  const selectedAccount=c.account,task=values.id;
  if(path==="/reviews/patients"){
   if(listGates.has(values.q))await listGates.get(values.q).promise;
   const rows=selected(values);let offset=values.offset||0;
   if(values.focus_mrn){const index=rows.findIndex(row=>row.mrn===values.focus_mrn);if(index>=0)offset=Math.floor(index/40)*40;}
   const patients=rows.slice(offset,offset+40);
   return {task:{id:task,status:"completed",revision:taskRevision,member_count:100},patients,total:rows.length,all_total:100,offset,limit:40,revision:"list-"+taskRevision,has_results:true,
    patient_revisions:Object.fromEntries(patients.map(patient=>[patient.mrn,revision(patient.mrn)])),selected_revision:revision(values.selected_mrn),selected_navigation:navigation(values,values.selected_mrn)};
  }
  if(path==="/reviews/patient"){
   if(detailGates.has(values.mrn))await detailGates.get(values.mrn).promise;
   return {task_id:task,patient:{...all.find(row=>row.mrn===values.mrn),mrn:wrongPatient?"FOREIGN":values.mrn,records:[{id:"SOAP-"+values.mrn,soap:selectedAccount+" "+task+" "+values.mrn}]},revision:revision(values.mrn),...navigation(values,values.mrn)};
  }
  throw new Error("Unexpected whole-task read: "+path);
 };
 s.run("vghks_bot/static/review-paging.js");const ui=c.window.ReviewPaging;
 c.selectReviewPatient=mrn=>ui.select(mrn);
 return {...s,ui,calls,notes,renders,revisions,listGates,detailGates,selections,setTaskRevision:value=>taskRevision=value,setFlush:value=>flush=value,setWrongPatient:value=>wrongPatient=value};
}

async function boundedPages(){
 const s=setup(),{ui,context:c,calls,renders,$}=s;
 await ui.load();assert.equal(c.reviewData.patients.length,40);assert.equal(c.reviewPatient,"P1");
 assert.equal(calls.filter(call=>call.path==="/reviews/patient").length,1,"first page eagerly loaded other patients' SOAP");
 assert.equal(s.notes[0].options.mrns.length,40);
 assert.equal($("#reviewPatients").children[0].querySelector(".review-sequence").textContent,"001","initial page did not refresh registration sequence after notes arrived");
 const firstRow=$("#reviewPatients").children[0],soap=$("#reviewDetail").querySelector(".soap-record");
 firstRow.querySelector(".patient-select").focus();$("#reviewDetail").scrollTop=73;
 await ui.poll({revision:1,status:"completed"},c.work.revisions);assert.equal(calls.length,2,"unchanged status triggered another read");
 s.setTaskRevision(2);s.revisions.set("P90",1);await ui.poll({revision:2,status:"completed"},c.work.revisions);
 assert.equal(calls.filter(call=>call.path==="/reviews/patient").length,1,"another patient's update reloaded selected SOAP");
 assert.equal($("#reviewPatients").children[0],firstRow,"unchanged summary row was rebuilt");
 assert.equal(c.document.activeElement,firstRow.querySelector(".patient-select"));assert.equal($("#reviewDetail").querySelector(".soap-record"),soap);assert.equal($("#reviewDetail").scrollTop,73);
 await ui.select("P40");assert.equal(ui.nav(1),"P41");await ui.move(1);
 assert.equal(c.reviewData.offset,40);assert.equal(c.reviewPatient,"P41");assert.equal(ui.patient().mrn,"P41");
 const selectedSoap=$("#reviewDetail").querySelector(".soap-record"),before=renders.length;
 await ui.load({offset:0});assert.equal(c.reviewPatient,"P41","paging silently selected another patient");
 assert.equal(renders.length,before);assert.equal($("#reviewDetail").querySelector(".soap-record"),selectedSoap);
 assert.equal($("#reviewDetail").querySelector(".review-page-context").hidden,false);
 $("#reviewQuery").value="none";await ui.load({resetPage:true});assert.equal(c.reviewData.total,0);assert.equal(c.reviewPatient,"P41");assert.equal(ui.nav(1),"");
 assert.match($("#reviewDetail").querySelector(".review-page-context").textContent,/不符合此篩選/);
 assert.ok(calls.every(call=>call.path!=="/reviews/results"));
 s.setFlush(false);const oldCount=calls.length;await assert.rejects(ui.select("P2"),/備註/);assert.equal(calls.length,oldCount);assert.equal(c.reviewPatient,"P41");
}

async function racingReads(){
 const s=setup(),{ui,context:c,$}=s;await ui.load();
 const listGate=deferred();s.listGates.set("late",listGate);$("#reviewQuery").value="late";const oldList=ui.load({resetPage:true});await tick();
 $("#reviewQuery").value="last";await ui.load({resetPage:true});listGate.resolve();await oldList;
 assert.equal(c.reviewData.total,1);assert.equal(c.reviewData.patients[0].mrn,"P100","late filter response replaced the current page");
 $("#reviewQuery").value="";await ui.load({resetPage:true});
 const gate=deferred();s.detailGates.set("P2",gate);const oldDetail=ui.select("P2");await tick();
 await ui.select("P3");gate.resolve();await oldDetail;assert.equal(ui.patient().mrn,"P3");assert.equal(c.reviewPatient,"P3");
 const oldFlush=deferred(),newFlush=deferred();let flushCalls=0;s.setFlush(()=>++flushCalls===1?oldFlush.promise:newFlush.promise);
 const delayedSelection=ui.select("P7");await tick();const latestSelection=ui.select("P8");await tick();newFlush.resolve(true);await latestSelection;oldFlush.resolve(true);await delayedSelection;
 assert.equal(c.reviewPatient,"P8","late note flush selected an older clicked patient");assert.ok(!s.calls.some(call=>call.path==="/reviews/patient"&&call.values.mrn==="P7"));s.setFlush(true);
 const late=deferred();s.detailGates.set("P4",late);const abandoned=ui.select("P4");await tick();
 c.account="OTHER";c.epoch++;c.currentReview="R2";c.reviewPatient="";ui.reset();await ui.load();late.resolve();await abandoned;
 assert.equal(c.reviewData.task.id,"R2");assert.equal(ui.patient().mrn,"P1");assert.match(ui.patient().records[0].soap,/OTHER R2/);
 s.setWrongPatient(true);await assert.rejects(ui.select("P6"),/範圍不符/);assert.equal(ui.patient(),null);
}

async function sharedReadsAndBoundedMemory(){
 const s=setup(),{ui,calls,context:c}=s;
 const gate=deferred();s.listGates.set("",gate);const first=ui.load(),second=ui.load();await tick();
 assert.equal(calls.filter(call=>call.path==="/reviews/patients").length,1,"same local page request was not shared");gate.resolve();await Promise.all([first,second]);
 for(let number=2;number<=10;number++)await ui.select("P"+number);
 const before=calls.filter(call=>call.path==="/reviews/patient").length;await ui.select("P1");
 assert.equal(calls.filter(call=>call.path==="/reviews/patient").length,before+1,"detail cache retained every visited SOAP");
 const previous=calls.length;s.revisions.set("P1",1);s.setTaskRevision(2);await ui.poll({status:"completed",revision:2},c.work.revisions);
 assert.equal(calls.length,previous+2,"selected revision did not refresh its own detail");
 assert.equal(calls.at(-1).values.mrn,"P1");
 const revisionGate=deferred();s.listGates.set("",revisionGate);const reading=ui.load();await tick();c.work.revisions.profile=1;revisionGate.resolve();await reading;
 const beforeRefresh=calls.length;await ui.poll({status:"completed",revision:2},c.work.revisions);
 assert.equal(calls.length,beforeRefresh+1,"revision arriving during a page read was incorrectly acknowledged without re-reading it");
}

(async()=>{await boundedPages();await racingReads();await sharedReadsAndBoundedMemory();process.stdout.write("Review paging: bounded details, cross-page navigation, stable DOM, note guards and stale-context isolation passed\n");})().catch(error=>{console.error(error);process.exitCode=1;});
