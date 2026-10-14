"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const {surface,deferred,tick,connection}=require("./sheet_management_ui");

async function accountCopyAndDefaults(){
 const s=surface(),source={id:"SOURCE_B",username:"DOCTOR_B",label:'<img src=x onerror="fail()">',campus:"高榮",spreadsheet_id:"OTHER_SHEET",email:"synthetic@example.invalid"};
 let gate=null,copies=0;
 s.handle(async(path,values)=>{
  if(path.endsWith("/status"))return connection({stale:true,default_hospital:"聯醫"});
  if(path.endsWith("/refresh"))return connection({default_hospital:"聯醫"});
  if(path==="/analysis/google")return {configured:true,spreadsheet_id:"SYNTHETIC_SHEET",email:"current@example.invalid"};
  if(path==="/analysis/google/sources")return {sources:[source]};
  if(path==="/analysis/google/copy"){copies++;assert.deepEqual(JSON.parse(JSON.stringify(values)),{source_account_id:source.id});if(gate)await gate.promise;return {configured:true,spreadsheet_id:source.spreadsheet_id,email:source.email};}
  throw new Error(path);
 });
 await s.ui.open();assert.equal(s.q("sheetManagementHospital").value,"聯醫");
 s.q("sheetManagementHospital").value="高榮";await s.q("sheetManagementHospital").emit("input");await s.q("sheetManagementRefresh").emit("click");assert.equal(s.q("sheetManagementHospital").value,"高榮","refresh overwrote manual campus");
 await s.q("sheetManagementSettings").emit("click");
 const area=s.q("sheetManagementGoogleCopy"),select=area.querySelectorAll("select")[0],copy=area.querySelectorAll("button")[0];
 assert.equal(copy.disabled,true);assert.ok(area.textContent.includes(source.label));assert.equal(area.querySelectorAll("img").length,0);
 select.value=source.id;await select.emit("change");assert.equal(copy.disabled,false);assert.ok(area.textContent.includes(source.email));
 await copy.emit("click");assert.equal(copies,1);assert.equal(s.q("sheetManagementGoogleSheet").value,source.spreadsheet_id);assert.equal(s.q("sheetManagementHospital").value,"高榮","copy replaced recipient campus");assert.match(area.textContent,/已帶入並保存/);
 gate=deferred();const copying=copy.emit("click");await tick();s.context.account="ACCOUNT_NEW";s.context.root.context="DB_NEW";s.ui.reset();gate.resolve();await copying;
 assert.equal(s.q("sheetManagementGoogleSheet").value,"");assert.equal(area.querySelectorAll("select")[0].value,"");assert.doesNotMatch(area.textContent,/synthetic@example|已帶入並保存/);
 s.context.root.read_only=true;await s.ui.open();await s.q("sheetManagementSettings").emit("click");assert.equal(copy.disabled,true);const before=copies;await copy.emit("click");assert.equal(copies,before);
}

async function fuAndSurgerySelectors(){
 const s=surface(),ctx=s.context,make=(tag,text,cls)=>{const node=ctx.document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;},preview=make("section");
 const source=fs.readFileSync("vghks_bot/static/analysis.js","utf8");
 Object.assign(ctx,{el:make,button:(text,handler)=>{const node=make("button",text);node.addEventListener("click",handler);return node;},$:selector=>selector==="#sheetPreview"?preview:null,surgeryEditors:new Map(),config:{read_only:false},Choices:{enhance:()=>{}},localTime:value=>value,action:task=>task(),openSavedRecord:()=>{throw new Error("No fetch expected");}});
 vm.runInContext(source.slice(source.indexOf("function fuCard("),source.indexOf("async function createSheetPreview(")),ctx);
 const labels={mrn:"病歷號",name:"姓名",date:"刀日",hospital:"醫院",side:"側別",procedure:"術式"};
 const fu={id:"FU1",kind:"fu",fields:{mrn:"SYNTHETIC",name:"合成",hospital:"聯醫",tag:"FU"},sources:[]};
 ctx.fuCard(fu,labels);const editor=ctx.surgeryEditors.get(fu.id);assert.equal(editor.inputs.hospital.tagName,"select");assert.equal(editor.inputs.hospital.value,"聯醫");assert.equal(editor.inputs.mrn.readOnly,true);assert.equal(editor.inputs.tag.readOnly,true);
 preview.append(make("p","Old preview"));editor.inputs.hospital.value="高榮";await editor.inputs.hospital.emit("change");assert.equal(preview.children.length,0);assert.equal(ctx.collectProposals()[0].fields.hospital,"高榮");
 const row={id:"SURGERY1",fields:{mrn:"SYNTHETIC",name:"合成",date:"2026-11-02",hospital:"聯醫",side:"OD",procedure:"Phaco-IOL"},sources:[],schedules:[],differences:{},ambiguities:{},evidence:{}};
 ctx.surgeryCard(row,labels);const surgery=ctx.surgeryEditors.get(row.id);assert.equal(surgery.inputs.hospital.tagName,"select");assert.equal(surgery.inputs.hospital.value,"聯醫");surgery.inputs.hospital.value="高榮";await surgery.inputs.hospital.emit("input");assert.equal(ctx.collectProposals().find(value=>value.id===row.id).fields.hospital,"高榮");
}

(async()=>{await accountCopyAndDefaults();await fuAndSurgerySelectors();console.log("Sheet connection and campus UI checks passed");})().catch(error=>{console.error(error);process.exitCode=1;});
