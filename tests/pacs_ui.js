"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const {surface} = require("./review_ui.js");
const tick = async () => {for (let i = 0; i < 20; i++) await Promise.resolve();};
const deferred = () => {let resolve, reject;const promise = new Promise((done, fail) => {resolve = done;reject = fail;});return {promise, resolve, reject};};

async function main() {
  const s = surface(), c = s.context;
  c.document.createElement = tag => s.node(tag);
  c.window.addEventListener = () => {};
  s.run("vghks_bot/static/clinical-ui.js");
  const root = s.$("#pacs-test"), calls = [], gate = deferred();
  let patient = "ONE";
  const scope = {account:"ACCOUNT",review_task_id:"REVIEW",mrn:"ONE",resource:"order_report",reference:"ORDER"};
  const request = async values => {calls.push(values);return gate.promise;};
  const make = () => c.window.ClinicalUI.pacsControl({request,scope,current:() => patient === "ONE"});
  const control = make();root.append(control);
  assert.equal(calls.length, 0, "rendering sent a PACS command");
  const button = control.querySelector("button");
  await button.emit("click");await button.emit("click");await tick();
  assert.equal(calls.length, 1, "double click sent a second command");
  assert.equal(button.disabled, true);
  assert.equal(button["aria-busy"], "true");
  const replacement = make();root.append(replacement);control.remove();
  assert.equal(replacement.querySelector("button").disabled, true, "progress redraw enabled duplicate submit");
  await replacement.querySelector("button").emit("click");await tick();
  assert.equal(calls.length, 1);
  gate.resolve({status:"HANDOFF_SENT",message:"已交接，請核對影像"});await tick();
  assert.equal(replacement.querySelector("button").disabled, false);
  assert.match(replacement.querySelector(".pacs-message").textContent, /核對/);

  const selectionCalls = [];
  const choice = c.window.ClinicalUI.pacsControl({scope:{...scope,reference:"MULTI"},request:async values => {
    selectionCalls.push(values);
    return values.launch_id ? {status:"HANDOFF_SENT",message:"已交接第二份"} : {
      status:"SELECTION_REQUIRED",message:"請選擇",choices:[{id:"A",label:"檢查單 A"},{id:"B",label:"檢查單 B"}]};
  }});root.append(choice);
  await choice.querySelector("button").emit("click");await tick();
  assert.equal(choice.querySelector(".pacs-selection").hidden, false);
  assert.equal(choice.querySelector("label").htmlFor, choice.querySelector("select").id);
  assert.equal(selectionCalls.length, 1, "choice discovery automatically handed off a study");
  choice.querySelector("select").value = "B";
  await choice.querySelector("button").emit("click");await tick();
  assert.equal(selectionCalls[1].launch_id, "B");
  assert.equal(selectionCalls[1].mrn, "ONE");

  const late = deferred(), pending = c.window.ClinicalUI.pacsControl({scope:{...scope,reference:"LATE"},
    request:() => late.promise,current:() => patient === "ONE"});root.append(pending);
  await pending.querySelector("button").emit("click");await tick();
  patient = "TWO";late.resolve({status:"HANDOFF_SENT",message:"舊病人結果"});await tick();
  assert.doesNotMatch(pending.querySelector(".pacs-message").textContent, /舊病人/);
  await pending.querySelector("button").emit("click");await tick();
  assert.equal(calls.length, 1, "stale patient control sent a command");

  let failures = 0;
  const unknown = c.window.ClinicalUI.pacsControl({scope:{...scope,mrn:"TWO"},request:async () => {
    failures++;throw new Error("連線中斷");
  }});root.append(unknown);
  await unknown.querySelector("button").emit("click");await tick();
  assert.equal(failures, 1);
  assert.match(unknown.querySelector(".pacs-message").textContent, /未自動重送/);
  const offline = c.window.ClinicalUI.pacsControl({scope,enabled:false,request:async () => {throw new Error("offline command");}});
  root.append(offline);assert.equal(offline.querySelector("button").disabled, true);
  await offline.querySelector("button").emit("click");await tick();

  const timers = new Map();let nextTimer = 0;
  c.setTimeout = (run, delay) => {timers.set(++nextTimer, {run, delay});return nextTimer;};
  c.clearTimeout = id => timers.delete(id);
  const batchGate = deferred(), batchCalls = [];
  const batch = c.window.ClinicalUI.pacsControl({scope:{...scope,resource:"orders",reference:""},compact:true,batch:true,
    enabled:false,request:values => {batchCalls.push(values);return batchGate.promise;}});
  root.append(batch);
  const batchButton = batch.querySelector("button"), batchMessage = batch.querySelector(".pacs-message");
  assert.equal(batchButton.disabled, true);
  batch.refresh({enabled:true,reset:true});
  await batchButton.emit("click");await tick();
  assert.equal(batchButton.dataset.state, "busy");
  assert.equal(batchMessage.classList.contains("is-visible"), false);
  batch.refresh({enabled:true,reset:true});
  assert.equal(batchButton.disabled, true, "changing selection reenabled an active batch");
  await batchButton.emit("click");
  batchGate.resolve({status:"PARTIAL",message:"舊選取"});await tick();
  assert.equal(batchCalls.length, 1);
  assert.equal(batchButton.disabled, false);
  assert.equal(batchMessage.textContent, "", "old batch result appeared after selection changed");

  const compact = c.window.ClinicalUI.pacsControl({scope,compact:true,
    request:async () => ({status:"HANDOFF_SENT",message:"請核對檢查"})});root.append(compact);
  await compact.querySelector("button").emit("click");await tick();
  assert.equal(compact.querySelector("button").dataset.state, "sent");
  assert.equal(compact.querySelector(".pacs-message").classList.contains("is-visible"), true);
  [...timers.values()].find(timer => timer.delay === 1600).run();
  assert.equal(compact.querySelector("button").dataset.state, undefined, "success left the button highlighted");
  const errorControl = c.window.ClinicalUI.pacsControl({scope,compact:true,
    request:async () => ({status:"UNAVAILABLE",message:"請啟動 DownloadApp"})});root.append(errorControl);
  await errorControl.querySelector("button").emit("click");await tick();
  assert.equal(errorControl.querySelector("button").dataset.state, "error");
  assert.equal(errorControl.querySelector("button").disabled, false);
  assert.equal(errorControl.querySelector(".pacs-message").classList.contains("is-visible"), true);
  assert.equal(compact.querySelector(".pacs-message").classList.contains("is-visible"), false, "two floating messages overlapped");

  // Render the production retina order card and exercise its bound endpoint,
  // including the cohort guard after a delayed UI action.
  c.ClinicalUI = c.window.ClinicalUI;c.el = s.node;c.config = {read_only:false};
  c.embeddedAccount = "ACCOUNT";c.sharedStatus = {online:true};c.currentCohort = {id:"RETINA-COHORT"};
  c.compareMember = () => ({account_id:"ACCOUNT",mrn:"RETINA"});
  c.dayText = c.localTime = value => value;
  c.compareCheckbox = () => s.node("input");
  const retinaCalls = [];
  c.api = async (path, values) => {retinaCalls.push({path,values});return {status:"HANDOFF_SENT",message:"已交接"};};
  const source = fs.readFileSync("vghks_bot/static/analysis.js", "utf8");
  vm.runInContext(source.slice(source.indexOf("function examCard("), source.indexOf("function svgNode(")), c);
  const card = c.examCard({id:"RETINA-ORDER",name:"合成眼底檢查",status:"complete",date:"2026-11-07"}, "order", "FAG");
  root.append(card);assert.equal(retinaCalls.length, 0);
  await card.querySelector("button").emit("click");await tick();
  assert.equal(retinaCalls[0].path, "/api/reviews/pacs/open");
  assert.equal(retinaCalls[0].values.module, "retina");
  assert.equal(retinaCalls[0].values.reference, "RETINA-ORDER");
  c.currentCohort = {id:"OTHER-COHORT"};
  await card.querySelector("button").emit("click");await tick();assert.equal(retinaCalls.length, 1);
  console.log("PACS controls: gesture, duplicate suppression, selection and stale patient checks passed");
}
main().catch(error => {console.error(error);process.exitCode = 1;});
