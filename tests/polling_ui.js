"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const {surface,deferred,tick}=require("./review_ui.js");

async function refreshTests(){
 const source=fs.readFileSync("vghks_bot/static/bot.js","utf8"),start=source.indexOf("async function refresh("),end=source.indexOf("function addManualPatient",start);
 const calls=[],nodes=new Map();let snapshotReads=0,revision="first",loadCount=0;
 const status={catalog_revision:"first",tasks:[{id:"review",kind:"review",status:"completed",updated_at:"later"}],revisions:{task:1},runs:[],online:true,active_count:0};
 const context={account:"account",work:null,currentReview:"",extensionTask:"",activeListRun:"",listIntent:{},page:"patients",catalogRevision:"",activityRevision:"",reviewSignature:"",recoverySeen:new Map(),URLSearchParams,
  api:async path=>{calls.push(path);if(path.startsWith("/status?"))return {...status,catalog_revision:revision};if(path==="/workbench?compact=1"){snapshotReads++;return {tasks:[{id:"review",kind:"review",status:"running"}],sets:[]};}throw new Error("unexpected full history read "+path);},
  $:selector=>{if(!nodes.has(selector))nodes.set(selector,{textContent:"",open:false,classList:{toggle(){}}});return nodes.get(selector);},
  categoryOptions(){},renderSets(){},say(){},readList:async()=>{},currentListIntent:()=>true,draftSave(){},
  loadReview:async()=>{loadCount++;},inProgress:new Set(["running","queued","cancelling"]),
  window:{Approvals:{badge(){}},ToolWorkspace:{watched:()=>[],poll:async()=>{}},TaskActivityUI:{invalidate(){},load:async()=>{}},ReviewHistoryUI:{poll:async()=>{}},ReviewPaging:{poll:async()=>{loadCount++;}}}};
 vm.createContext(context);vm.runInContext(source.slice(start,end),context);
 await context.refresh({light:true});assert.equal(snapshotReads,1,"initial snapshot was not loaded");
 await context.refresh({light:true});assert.equal(snapshotReads,1,"unchanged catalog was reloaded");
 assert.equal(context.work.tasks[0].status,"completed","live task status was not merged");
 revision="second";await context.refresh({light:true});assert.equal(snapshotReads,2,"changed catalog was not loaded");
 context.currentReview="review";context.page="review";await context.refresh({light:true});assert.equal(loadCount,1,"watched review was not updated");
 context.activeListRun="new-list";
 context.window.JobProgress={watchedRuns:key=>{assert.equal(key,"account");return ["old-list","new-list"];}};
 context.window.WorkspacePoll={watched:()=>["old-list","embedded-list"],publish(){}};
 await context.refresh({light:true});
 assert.equal(new URL(calls.at(-1),"http://localhost").searchParams.get("run"),"new-list,old-list,embedded-list",
   "changing the foreground list dropped a run still displayed in the progress dock");
 status.missing_run_ids=["new-list"];await context.refresh({light:true});
 assert.equal(context.activeListRun,"","an unavailable run blocked future list polling");
 assert.ok(calls.every(path=>!path.startsWith("/history")),"poll downloaded full history");
}

async function listIntentTests(){
 const source=fs.readFileSync("vghks_bot/static/bot.js","utf8"),s=surface(),c=s.context,$=s.$;
 const reads=[],starts=[],status={catalog_revision:"same",tasks:[],revisions:{task:1},runs:[],online:true},pending=[];
 let saved=0;
 Object.assign(c,{epoch:1,listIntent:null,activeListRun:"",listing:null,listTab:"own",selected:new Set(),manual:[],collapsedDays:new Set(),
  extensionTask:"",page:"patients",catalogRevision:"same",activityRevision:"",recoverySeen:new Map(),work:{online:true,tasks:[]},
  dateMode(){},categoryOptions(){},renderSets(){},renderList(){},draftSave(){saved++;},
  JobProgress:{pending(){const value={removed:false,remove(){this.removed=true;}};pending.push(value);return value;},show(){}},
  api:async(path,values)=>{
   if(path.startsWith("/status?"))return status;
   const gate=deferred();
   if(path==="/lists")reads.push({values,...gate});
   else if(path==="/lists/browse")starts.push({values,...gate});
   else throw new Error(path);
   return gate.promise;
  }
 });
 $("#dateForm").reportValidity=()=>true;$("#dateMode").value="single";
 const listStart=source.indexOf("const ranges="),refreshStart=source.indexOf("async function refresh(");
 vm.runInContext(source.slice(listStart,source.indexOf("function renderList()",listStart))+
  source.slice(refreshStart,source.indexOf("function addManualPatient",refreshStart)),c);
 const rows=(...ids)=>({days:[{rows:ids.map(id=>({id,mrn:"SYN-"+id,ownership:"DEDICATED"}))}]});
 const browse=date=>{$("#startDate").value=date;return c.browse();};

 const first=browse("2026-11-07"),second=browse("2026-11-08");
 reads[1].resolve(rows("B"));await tick();
 assert.equal(starts.length,1);assert.equal(starts[0].values.ranges.ACCOUNT.start,"2026-11-08");
 starts[0].resolve({run_ids:["B-run"]});await second;
 reads[0].resolve(rows("A"));await first;
 assert.equal(starts.length,1,"a superseded local read started a hospital query");
 assert.deepEqual([...c.selected],["B"]);assert.equal(c.listing.days[0].rows[0].id,"B");

 c.selected.delete("B");status.runs=[{id:"B-run",status:"completed"}];
 const terminal=c.refresh({light:true});await tick();
 assert.equal(reads.at(-1).values.start,"2026-11-08");
 reads.at(-1).resolve(rows("B","new"));await terminal;
 assert.deepEqual([...c.selected],["new"],"terminal refresh undid the user's deselection");

 const old=browse("2026-11-09");
 reads.at(-1).resolve(rows("C"));await tick();
 const oldStart=starts.at(-1),newer=browse("2026-11-10");
 reads.at(-1).resolve(rows("D"));await tick();
 starts.at(-1).resolve({run_ids:["D-run"]});await newer;
 oldStart.resolve({run_ids:["C-run"]});await old;
 assert.equal(c.activeListRun,"D-run","a late dispatch took over newer task tracking");
 status.runs=[{id:"D-run",status:"completed"}];
 const lateTerminal=c.refresh({light:true});await tick();
 const stale=reads.at(-1);
 const latest=browse("2026-11-11");
 reads.at(-1).resolve(rows("E"));await tick();
 starts.at(-1).resolve({run_ids:["E-run"]});await latest;
 stale.resolve(rows("STALE"));await lateTerminal;
 assert.equal(c.listing.days[0].rows[0].id,"E");assert.deepEqual([...c.selected],["E"]);assert.equal(c.activeListRun,"E-run");

 const switching=browse("2026-11-12"),count=starts.length;
 c.epoch++;c.account="OTHER";reads.at(-1).resolve(rows("OTHER-SCOPE"));await switching;
 assert.equal(starts.length,count,"an old account read started a query after a switch");
 assert.ok(pending.every(value=>value.removed),"obsolete placeholders survived");
 assert.equal(saved,5,"stale requests changed the saved selection");
}

async function progressTests(){
 const messages=[],key="a".repeat(32),parent={postMessage:value=>messages.push(value)};
 let task={id:"task",kind:"review",status:"running",progress:{done:0,total:1}};
 const response=data=>new Response(JSON.stringify(data),{status:200});
 let fetchSnapshot=async()=>response({tasks:[task],runs:[]});
 const window={addEventListener(){},fetch:()=>fetchSnapshot()};
 const context={window,parent,document:{addEventListener(){}},location:{search:"?account="+key,origin:"http://localhost",href:"http://localhost/tools"},URL,URLSearchParams,Date,setTimeout,clearTimeout};
 vm.createContext(context);vm.runInContext(fs.readFileSync("vghks_bot/static/progress.js","utf8"),context);
 await context.window.fetch("http://localhost/api/accounts/"+key+"/status");
 assert.equal(messages.at(-1).runs[0].status,"running");
 task={...task,status:"completed",finished_at:new Date().toISOString(),progress:{done:1,total:1}};
 await context.window.fetch("http://localhost/api/accounts/"+key+"/status");
 assert.equal(messages.at(-1).runs[0].status,"completed","floating progress did not observe lightweight status");
 assert.equal(messages.at(-1).runs[0].progress.done,1);
 const progress=context.window.JobProgress,oldRun="b".repeat(32),other="c".repeat(32);
 progress.publish({id:oldRun,source:"run",status:"running",revision:4},key);
 progress.publish({id:other,source:"run",status:"running",revision:3},"d".repeat(32));
 assert.deepEqual(Array.from(progress.watchedRuns(key)),[oldRun],"watch IDs included another account or a placeholder");
 const requestCount=messages.length;
 progress.publish({id:oldRun,source:"run",status:"partial",revision:5,finished_at:new Date(Date.now()-332000).toISOString()},key);
 assert.equal(messages.length,requestCount+1,"an expired terminal result was dropped before informing the parent");
 assert.equal(messages.at(-1).runs[0].status,"partial");
 progress.publish({id:oldRun,source:"run",status:"running",revision:4},key);
 progress.publish({id:oldRun,source:"run",status:"queued"},key);
 assert.deepEqual(Array.from(progress.watchedRuns(key)),[],"stale polls or late start responses revived a finished run");
 progress.publish({id:oldRun,source:"run",status:"running",revision:6},key);
 assert.deepEqual(Array.from(progress.watchedRuns(key)),[oldRun],"a newer resume was rejected");
 const missingURL="http://localhost/api/accounts/"+key+"/status?run="+oldRun;
 let releaseMissing;
 fetchSnapshot=()=>new Promise(resolve=>{releaseMissing=resolve;});
 const slowMissing=window.fetch(missingURL);
 progress.publish({id:oldRun,source:"run",status:"partial",revision:7,finished_at:new Date().toISOString()},key);
 const count=progress.size();
 releaseMissing(response({missing_run_ids:[oldRun]}));
 await slowMissing;
 assert.equal(progress.size(),count,"an older missing-run response erased a newer terminal result");
 const fresh="e".repeat(32),before=messages.length;
 progress.publish({id:fresh,source:"run",status:"partial",revision:3,finished_at:new Date(Date.now()-332000).toISOString()},key);
 assert.equal(messages.length,before+1,"a newly loaded iframe did not report the terminal state to its parent");
}
(async()=>{await refreshTests();await listIntentTests();await progressTests();console.log("Lightweight polling and progress checks passed");})().catch(error=>{console.error(error);process.exitCode=1;});
