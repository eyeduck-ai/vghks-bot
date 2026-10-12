"use strict";
// Synthetic UI checks: no credentials, patient data or network requests.
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
function surface(){
 const nodes=new Map();
 class Element{
  constructor(tag="div"){this.tagName=tag;this.children=[];this.dataset={};this.className="";this.value="";this.disabled=false;this.checked=false;this.scrollTop=0;this.events=new Map();}
  append(...children){for(const child of children){child.parentElement=this;this.children.push(child);}}
  replaceChildren(...children){this.children=[];this.append(...children);}
  all(){return this.children.flatMap(child=>[child,...child.all()]);}
  addEventListener(name,run){this.events.set(name,run);}
  setAttribute(name,value){this[name]=value;}
  emit(name){return this.events.get(name)?.({preventDefault(){}});}
 }
 const $=selector=>{if(!nodes.has(selector))nodes.set(selector,new Element());return nodes.get(selector);};
 const node=(tag,text,cls)=>{const value=new Element(tag);value.textContent=text||"";value.className=cls||"";return value;};
 const calls=[],opened=[];
 const context={$,node,$$:selector=>[...new Set([...nodes.values()].flatMap(value=>[value,...value.all()]))].filter(value=>value.className.split(/\s+/).includes(selector.slice(1))),
  root:{read_only:false},account:"A",currentReview:"R1",reviewPatient:"P1",window:{ReviewNotesUI:{flushAll:async()=>true,sequence:()=>""}},DOMException,
  Option:function(text,value){const option=node("option",text);option.value=value;return option;},
  dialog:()=>{$("#handoffDialog").open=true;},act:run=>run(),refresh:async()=>{},say(){},
  api:async(path,values)=>{calls.push({path,values});return {cohort:{id:"COHORT"},focus_mrn:values.mrns[0]};},
  openModule:async(...args)=>opened.push(args),labels:{completed:"完成"},badges:()=>node("span"),empty:text=>node("p",text),
  btn:(text,run,cls)=>{const button=node("button",text,cls);button.addEventListener("click",run);return button;},
  check:(label,checked,change)=>{const input=node("input");input.type="checkbox";input.checked=checked;input.setAttribute("aria-label",label);input.addEventListener("change",()=>change(input.checked));return input;},
  selectReviewPatient:mrn=>{context.reviewPatient=mrn;$("#reviewDetail").scrollTop=0;}};
 $("#handoffForm").reportValidity=()=>true;$("#handoffDialog").close=()=>{$("#handoffDialog").open=false;};
 vm.createContext(context);vm.runInContext(fs.readFileSync("vghks_bot/static/review-handoff.js","utf8"),context);
 const bot=fs.readFileSync("vghks_bot/static/bot.js","utf8");
 vm.runInContext(bot.slice(bot.indexOf("function renderReviewPatients("),bot.indexOf("function soapNode(")),context);
 return {context,$,calls,opened,ui:context.window.ReviewHandoff};
}
async function run(){
 const {context:c,$,calls,opened,ui}=surface();
 const first={mrn:"P1",name:"合成甲",status:"completed",records:[{id:"SOAP1",date:"2026-10-01"}]};
 const second={mrn:"P2",name:"合成乙",status:"completed",records:[{id:"SOAP2",date:"2026-10-02"}]};
 $("#reviewPatients").scrollTop=70;$("#reviewDetail").scrollTop=110;
 c.renderReviewPatients([first,second]);
 const rows=$("#reviewPatients").children;
 assert.equal(rows[0].tagName,"div");assert.equal(rows[0].children[0].tagName,"label");assert.equal(rows[0].children[1].tagName,"button");
 assert.equal(rows[0].children[1].all().some(value=>value.type==="checkbox"),false,"checkbox nested in patient button");
 const check=rows[1].children[0].children[0];check.checked=true;check.emit("change");
 assert.equal(c.reviewPatient,"P1","selection moved review focus");assert.equal($("#reviewDetail").scrollTop,110);
 ui.toggle(first,true);ui.toggle(first,true);ui.toggle({mrn:" "},true);
 assert.equal(ui.patients().length,2,"selection did not deduplicate MRNs");
 assert.equal($("#reviewHandoffSelected").disabled,false);
 c.renderReviewPatients([first]);assert.equal(ui.patients().length,2,"filter dropped hidden selection");
 assert.equal($("#reviewPatients").scrollTop,70,"refresh reset patient rail scroll");
 c.renderReviewPatients([{...first,name:"合成甲更新"},second]);
 assert.equal(ui.patients().find(patient=>patient.mrn==="P1").name,"合成甲更新");
 assert.ok($("#reviewPatients").children.every(row=>row.children[0].children[0].checked),"refresh lost checkbox state");
 ui.scope();assert.equal(ui.patients().length,2,"same review/page return cleared selection");
 await $("#reviewHandoffSelected").emit("click");
 assert.equal($("#handoffSource").children.length,1);assert.equal($("#handoffSource").disabled,true);
 $("#handoffSource").value="SOAP2";$("#handoffModule").value="surgery";
 await ui.send(false);
 assert.deepEqual(Array.from(calls[0].values.mrns),["P2","P1"]);assert.equal(calls[0].values.record_ids,undefined,"batch allowed individual SOAP source");
 assert.equal(c.reviewPatient,"P1");assert.equal($("#reviewDetail").scrollTop,110);assert.equal(ui.patients().length,2);
 ui.openMany([first,second,first,{mrn:""}]);$("#handoffModule").value="cataract";await ui.send(true);
 assert.deepEqual(Array.from(calls[1].values.mrns),["P1","P2"]);assert.equal(opened[0][2].focusMrn,"P1");
 ui.open(first);assert.equal($("#handoffSource").disabled,false);assert.equal($("#handoffSource").children.length,2);
 $("#handoffSource").value="SOAP1";await ui.send(false);assert.deepEqual(Array.from(calls[2].values.record_ids),["SOAP1"]);
 ui.openMany([first]);assert.equal($("#handoffSource").children.length,1,"one selected patient enabled individual source");
 c.root.read_only=true;ui.renderSelection();assert.equal($("#reviewHandoffSelected").disabled,true);
 assert.ok($("#reviewPatients").children.every(row=>row.children[0].children[0].disabled));
 const before=calls.length;await assert.rejects(ui.send(false),/唯讀/);assert.equal(calls.length,before);
 assert.throws(()=>ui.openMany([first]),/唯讀/);ui.toggle(first,false);assert.equal(ui.patients().length,2,"readonly checkbox changed selection");
 c.root.read_only=false;c.currentReview="R2";ui.renderSelection();assert.equal(ui.patients().length,0,"task switch retained selection");
 ui.toggle(first,true);c.account="B";ui.renderSelection();assert.equal(ui.patients().length,0,"account switch retained selection");
 ui.toggle(first,true);ui.clear();assert.equal(ui.patients().length,0);assert.equal($("#reviewHandoffSelected").disabled,true);
 ui.toggle(second,true);ui.reset();assert.equal(ui.patients().length,0);
 ui.open(first);c.currentReview="R3";await assert.rejects(ui.send(false),error=>error.name==="AbortError");
 const race=surface(),rc=race.context;let finish,received=0,refreshed=0;
 rc.api=()=>new Promise(resolve=>{finish=resolve;});rc.window.ToolWorkspace={receivedHandoff:()=>received++};rc.refresh=async()=>{refreshed++;};
 race.ui.openMany([first,second]);const pending=race.ui.send(true);for(let i=0;i<12;i++)await Promise.resolve();
 rc.account="OTHER";race.$("#handoffStatus").textContent="新帳號狀態";finish({cohort:{id:"OLD"},focus_mrn:"P1"});
 await assert.rejects(pending,error=>error.name==="AbortError");assert.equal(received,0);assert.equal(refreshed,0);assert.equal(race.opened.length,0);
 assert.equal(race.$("#handoffStatus").textContent,"新帳號狀態","stale response changed new account status");
 process.stdout.write("Handoff selection UI checks passed\n");
}
run().catch(error=>{console.error(error);process.exitCode=1;});
