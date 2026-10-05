"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");

async function refreshTests(){
 const source=fs.readFileSync("vghks_bot/static/bot.js","utf8"),start=source.indexOf("async function refresh("),end=source.indexOf("function addManualPatient",start);
 const calls=[],nodes=new Map();let snapshotReads=0,revision="first",loadCount=0;
 const status={catalog_revision:"first",tasks:[{id:"review",kind:"review",status:"completed",updated_at:"later"}],revisions:{task:1},runs:[],online:true,active_count:0};
 const context={account:"account",work:null,currentReview:"",extensionTask:"",activeListRun:"",page:"patients",catalogRevision:"",activityRevision:"",reviewSignature:"",recoverySeen:new Map(),URLSearchParams,
  api:async path=>{calls.push(path);if(path.startsWith("/status?"))return {...status,catalog_revision:revision};if(path==="/workbench?compact=1"){snapshotReads++;return {tasks:[{id:"review",kind:"review",status:"running"}],sets:[]};}throw new Error("unexpected full history read "+path);},
  $:selector=>{if(!nodes.has(selector))nodes.set(selector,{textContent:"",open:false,classList:{toggle(){}}});return nodes.get(selector);},
  categoryOptions(){},renderSets(){},say(){},loadReview:async()=>{loadCount++;},inProgress:new Set(["running","queued","cancelling"]),
  window:{Approvals:{badge(){}},ToolWorkspace:{watched:()=>[],poll:async()=>{}},TaskActivityUI:{invalidate(){},load:async()=>{}},ReviewHistoryUI:{poll:async()=>{}}}};
 vm.createContext(context);vm.runInContext(source.slice(start,end),context);
 await context.refresh({light:true});assert.equal(snapshotReads,1,"initial snapshot was not loaded");
 await context.refresh({light:true});assert.equal(snapshotReads,1,"unchanged catalog was reloaded");
 assert.equal(context.work.tasks[0].status,"completed","live task status was not merged");
 revision="second";await context.refresh({light:true});assert.equal(snapshotReads,2,"changed catalog was not loaded");
 context.currentReview="review";context.page="review";await context.refresh({light:true});assert.equal(loadCount,1,"watched review was not updated");
 assert.ok(calls.every(path=>!path.startsWith("/history")),"poll downloaded full history");
}

async function progressTests(){
 const messages=[],key="a".repeat(32),parent={postMessage:value=>messages.push(value)};
 let task={id:"task",kind:"review",status:"running",progress:{done:0,total:1}};
 const window={addEventListener(){},fetch:async()=>new Response(JSON.stringify({tasks:[task],runs:[]}),{status:200})};
 const context={window,parent,document:{addEventListener(){}},location:{search:"?account="+key,origin:"http://localhost",href:"http://localhost/tools"},URL,URLSearchParams,Date,setTimeout,clearTimeout};
 vm.createContext(context);vm.runInContext(fs.readFileSync("vghks_bot/static/progress.js","utf8"),context);
 await context.window.fetch("http://localhost/api/accounts/"+key+"/status");
 assert.equal(messages.at(-1).runs[0].status,"running");
 task={...task,status:"completed",finished_at:new Date().toISOString(),progress:{done:1,total:1}};
 await context.window.fetch("http://localhost/api/accounts/"+key+"/status");
 assert.equal(messages.at(-1).runs[0].status,"completed","floating progress did not observe lightweight status");
 assert.equal(messages.at(-1).runs[0].progress.done,1);
}
(async()=>{await refreshTests();await progressTests();console.log("Lightweight polling and progress checks passed");})().catch(error=>{console.error(error);process.exitCode=1;});
