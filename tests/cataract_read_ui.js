"use strict";
// Exercise cache/request behavior with controlled responses, without hospital I/O.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const nodes = new Map(), messages = new Map();
class Node {
  constructor() {this.children=[];this.value="";this.dataset={};this.scrollTop=0;this.classList={toggle(){}};}
  set id(value) {nodes.set("#"+value,this);}
  before() {} after() {} closest() {return new Node();}
  append(...values) {this.children.push(...values);}
  replaceChildren(...values) {this.children=values;}
  contains() {return false;} setAttribute() {} getAttribute() {return "";}
  querySelectorAll() {return [];} querySelector() {return null;} addEventListener() {} focus() {}
}
const $ = selector => {
  if(selector==="#cataractPatientsToggle"&&!nodes.has(selector))return null;
  if(!nodes.has(selector))nodes.set(selector,new Node());
  return nodes.get(selector);
};
const deferred = () => {let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};};
const calls = [], updates = [];
let respond, pending = 0;
const context = {
  $, $$:()=>[], el:()=>new Node(), button:(text,run)=>{const node=new Node();node.click=run;return node;}, embeddedAccount:"b".repeat(32),
  currentCohort:{id:"a".repeat(32),members:[{mrn:"TEST001",account_id:"b".repeat(32)},{mrn:"TEST002",account_id:"b".repeat(32)}]},
  config:{read_only:false}, performance, location:{origin:"http://synthetic.invalid"}, resultRequest:0,
  document:{createComment:()=>new Node(),activeElement:null,addEventListener(){}},
  window:{addEventListener:(name,callback)=>messages.set(name,[...(messages.get(name)||[]),callback]),CataractNumeric:{exams:[]}},
  ClinicalUI:{mrnBadge:()=>new Node(),ageBadge:()=>new Node(),patientAge:()=>"未提供"},
  JobProgress:{pending:()=>{pending++;return {remove(){}};}},
  loadAnalysisResult:async options=>updates.push(options), notify(){}, action:run=>run(),
  ap:(path,values)=>{calls.push({path,values});return respond(path,values);}
};
context.parent=context.window;context.parent.postMessage=()=>{};
$("#analysisView").value="cataract";$("#analysisPatient").value="TEST001";
vm.createContext(context);
vm.runInContext(fs.readFileSync("vghks_bot/static/cataract.js","utf8"),context);
const ui=context.window.CataractUI;
const values=mrn=>({cohort_id:context.currentCohort.id,mrn,module:"cataract"});
const data=(mrn,revision="r1")=>({member:{mrn},data_revision:revision,numeric:[],orders:[]});
const summary=(revision="r1")=>({members:["TEST001","TEST002"].map(mrn=>({mrn,ready:true,data_revision:revision})),queue:{state:"completed",pending:[]}});
async function main() {
  ui.layout();
  let response=deferred();respond=()=>response.promise;
  const first=ui.readResult(values("TEST001")), duplicate=ui.readResult(values("TEST001"));
  assert.equal(first,duplicate);assert.equal(calls.length,1);
  response.resolve(data("TEST001"));await first;
  await ui.readResult(values("TEST001"));assert.equal(calls.length,1,"revisit requested saved data again");

  respond=async()=>data("TEST002");await ui.readResult(values("TEST002"));
  response=deferred();respond=()=>response.promise;
  const before=calls.length, polls=Array.from({length:20},()=>ui.refreshStatus({force:true}));
  const start=ui.autoStart();response.resolve(summary());await Promise.all([...polls,start]);
  assert.equal(calls.length,before+1,"concurrent status reads were not combined");
  assert.equal(pending,0,"completed patient displayed background fetch progress");
  assert.equal(calls.filter(call=>call.path==="cataract/queue").length,0);
  await $("#cataractPatients").children.find(node=>node.dataset.patientMrn==="TEST002").click();
  assert.equal(calls.filter(call=>call.path==="cataract/queue").length,0,"switching a completed patient sent prioritize");
  $("#analysisPatient").value="TEST001";updates.length=0;

  respond=async()=>({members:[{mrn:"TEST001",ready:true,data_revision:"r2"},{mrn:"TEST002",ready:true,data_revision:"r1"}],queue:{state:"completed",pending:[]}});
  await ui.refreshStatus({force:true});
  assert.equal(ui.cachedResult(values("TEST001")),null,"changed data kept stale SOAP/numeric");
  assert.equal(ui.cachedResult(values("TEST002")).member.mrn,"TEST002","unrelated patient's cache was discarded");
  assert.equal(updates.length,1);

  response=deferred();respond=()=>response.promise;
  const previousValues=values("TEST001"), late=ui.readResult(previousValues);
  context.currentCohort={...context.currentCohort,id:"c".repeat(32)};ui.layout();
  response.resolve(data("TEST001"));await late;
  assert.equal(ui.cachedResult(previousValues),null,"old collection's response restored discarded data");
  respond=async()=>data("TEST001","r3");await ui.readResult(values("TEST001"));
  response=deferred();respond=()=>response.promise;
  const oldPoll=ui.refreshStatus({force:true});
  context.currentCohort={...context.currentCohort,id:"d".repeat(32)};ui.layout();
  response.resolve(summary("r4"));assert.equal(await oldPoll,null,"old collection's status overwrote current status");
  assert.equal(ui.cachedResult(values("TEST001")),null);

  respond=async()=>({members:["TEST001","TEST002"].map(mrn=>({mrn,ready:false,attempted:true})),queue:{state:"completed",pending:[]}});
  await ui.refreshStatus({force:true});await ui.autoStart();
  await $("#cataractPatients").children.find(node=>node.dataset.patientMrn==="TEST002").click();
  assert.equal(pending,0,"partial failure was implicitly retried");
  $("#analysisPatient").value="TEST001";
  response=deferred();respond=()=>response.promise;
  const cleared=ui.readResult(values("TEST001"));
  for(const receive of messages.get("message"))receive({origin:context.location.origin,source:context.parent,
    data:{type:"bot:data-cleared",account:context.embeddedAccount,mrns:["TEST001"]}});
  response.resolve(data("TEST001","deleted"));await cleared;
  assert.equal(ui.cachedResult(values("TEST001")),null,"in-flight response restored cleared local data");
  console.log("Patient cache, request coalescing, completed queue suppression and stale context checks passed.");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
