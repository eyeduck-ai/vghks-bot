"use strict";
// The SDK profile supplies all character meanings; these are synthetic names.
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const {surface}=require("./review_ui.js"),s=surface(),c=s.context,$=s.$,writes=[];
c.document.createElement=tag=>s.node(tag);c.window.addEventListener=()=>{};
c.navigator={clipboard:{writeText:async value=>writes.push(value)}};
s.run("vghks_bot/static/clinical-ui.js");c.ClinicalUI=c.window.ClinicalUI;
const ui=c.ClinicalUI,raw="\ue3a4甲乙",display="\u51c3甲乙";
const profile={source:"vghks",version:"synthetic-1",mapping:{"\ue3a4":"\u51c3","\uea81":"\u771e","\ue03e":"\u7d89"}};
assert.equal(ui.patientName(raw),raw,"The UI must not contain a built-in glyph table");
ui.configurePatientNames(profile);
assert.equal(ui.patientName(raw),display);
assert.equal(ui.patientName("\ue3a4\uea81\ue03e甲乙"),"凃眞綉甲乙","Confirmed glyphs must keep their exact Unicode forms");
assert.equal(ui.patientName(raw,{source:"other"}),raw,"A private code must not cross data sources");
assert.equal(ui.patientName("\ue3a5甲乙"),"\ue3a5甲乙","Unknown private-use characters were guessed");
assert.equal(ui.patientName("凃涂塗眞真綉繡甲乙"),"凃涂塗眞真綉繡甲乙","Standard characters were normalized");
assert.equal(ui.patientName(null),"");
profile.mapping["\ue3a4"]="錯";profile.source="other";
assert.equal(ui.patientName(raw),display,"External profile mutation changed the active mapping");

const bot=fs.readFileSync("vghks_bot/static/bot.js","utf8");
for(const name of ["patientName","visibleRows"]){
 const line=bot.split("\n").find(line=>line.startsWith("function "+name+"("));
 assert.ok(line,"Production helper missing: "+name);vm.runInContext(line,c);
}
vm.runInContext(bot.slice(bot.indexOf("function patientChip("),bot.indexOf("const labels=")),c);
vm.runInContext(bot.slice(bot.indexOf("function patientClipboardRow("),bot.indexOf("function reviewDataInfo(")),c);
const patient={mrn:"00012345",name:raw,mobile_phone:"0900000000",soap:raw+" SYNTHETIC SOAP",debug:{text:raw}};
const before=JSON.stringify(patient),label=c.patientName(patient),chip=c.patientChip(patient,()=>{});
assert.equal(label.children[0].textContent,display);
assert.equal(chip.children[0].textContent,display);
assert.match(chip["aria-label"],/凃甲乙/);
assert.equal(c.patientClipboardRow(patient),"00012345\t"+display+"\t0900000000");
assert.equal(JSON.stringify(patient),before,"Display changed saved names, SOAP or DEBUG data");
const hostile={...patient,name:raw+"<img src=x onerror=alert(1)>"};
const hostileLabel=c.patientName(hostile);
assert.equal(hostileLabel.children[0].textContent,display+"<img src=x onerror=alert(1)>");
assert.equal(hostileLabel.children[0].children.length,0,"Patient text became HTML elements");

c.allRows=()=>[patient];c.own=()=>true;c.listTab="own";
for(const query of [raw,display,"00012345"]){$("#listQuery").value=query;assert.equal(c.visibleRows().length,1);}
$("#listQuery").value="另一人";assert.equal(c.visibleRows().length,0);

// Real embedded cohort rendering must use the same configured profile.
const analysis=fs.readFileSync("vghks_bot/static/analysis.js","utf8");
c.el=s.node;c.button=(text)=>s.node("button",text);
c.Option=function(text,value){const option=s.node("option",text);option.value=value;return option;};
c.analysisState={cohorts:[{id:"COHORT",name:"SYNTHETIC",members:[patient]}]};
c.accounts=new Map();c.compared=new Set();c.updateComparison=()=>{};
$("#cohortSelect").value="COHORT";
vm.runInContext(analysis.slice(analysis.indexOf("function selectCohort("),analysis.indexOf("async function saveCurrentCohort(")),c);
c.selectCohort();
assert.equal($("#analysisPatient").children[0].textContent,display+" · 00012345");
assert.equal($("#cohortMembers").children[0].children[0].textContent,display);
assert.equal(JSON.stringify(patient),before,"Embedded rendering changed the cohort source");

// Handoff keeps raw identities while projecting the confirmation caption.
s.run("vghks_bot/static/review-handoff.js");
c.window.ReviewHandoff.open(patient);
assert.equal($("#handoffPatient").textContent,display+" · 00012345");
c.window.ReviewHandoff.toggle(patient,true);
assert.equal(c.window.ReviewHandoff.patients()[0].name,raw,"Handoff selection changed patient identity");
assert.equal(JSON.stringify(patient),before);

// Note previews/prints map only the patient name; clinical note text stays raw.
const notes=fs.readFileSync("vghks_bot/static/review-notes.js","utf8");
c.table=(_headers,rows)=>{c.printRows=rows;return s.node("table");};
vm.runInContext(notes.slice(notes.indexOf(" function noteTable("),notes.indexOf(" async function openPrint(")),c);
const note={...patient,text:raw+" SYNTHETIC NOTE"},noteBefore=JSON.stringify(note);
c.noteTable([note]);assert.equal(c.printRows[0][1],display);
assert.equal(c.printRows[0][3].textContent,note.text,"Printing rewrote note content");
assert.equal(JSON.stringify(note),noteBefore);

(async()=>{
 const copy=ui.copyButton(c.patientClipboardRow(patient),{label:"刀表資料",text:ui.patientName(patient.name)});
 await copy.emit("click");assert.equal(writes[0],"00012345\t"+display+"\t0900000000");
 assert.equal(JSON.stringify(patient),before);
 // Exercise the real scan opening path without fetching a hospital response.
 const scan=fs.readFileSync("vghks_bot/static/scan-browser.js","utf8"),comparisons=[];
 c.mount=()=>{};c.clearView=()=>{};c.context=null;c.categoryFilter="";c.searchText="";c.syncCompare=()=>{};
 c.window.FileCompare={context:(...values)=>comparisons.push(values)};
 c.document.getElementById=id=>$("#"+id);c.dialog=()=>$("#scanBrowserDialog");
 $("#scanBrowserDialog").showModal=()=>{$("#scanBrowserDialog").open=true;};
 c.content=()=>$("#scanBrowserContent");c.make=s.node;c.refresh=async()=>{};
 vm.runInContext(scan.slice(scan.indexOf("  async function open(options)"),scan.indexOf("  async function refresh(")),c);
 const options={account:"SYNTHETIC",mrn:patient.mrn,patient_name:raw,resource:"scans"},scanBefore=JSON.stringify(options);
 await c.open(options);
 assert.equal($("#scanBrowserPatient").textContent,display+" · 病歷號 00012345");
 assert.deepEqual(comparisons[0],["SYNTHETIC","00012345",display]);
 assert.equal(JSON.stringify(options),scanBefore,"Scan display changed its raw context");
 ui.configurePatientNames({source:"other",version:"synthetic-2",mapping:{"\ue3a4":"錯"}});
 assert.equal(ui.patientName(raw),raw);
 assert.equal(ui.patientName(raw,{source:"other"}),raw);
 ui.configurePatientNames({source:"vghks",version:"synthetic-3",mapping:{"ab":"錯","\ue3a4":"兩字"}});
 assert.equal(ui.patientName(raw),raw,"Malformed multi-character mapping was applied");
 ui.configurePatientNames();assert.equal(ui.patientName(raw),raw,"An older SDK should retain raw names");
 console.log("SDK-configured patient names, source isolation, safe rendering and raw preservation checks passed");
})().catch(error=>{console.error(error);process.exitCode=1;});
