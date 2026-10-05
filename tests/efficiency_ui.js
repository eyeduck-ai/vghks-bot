"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const key="a".repeat(32),id="b".repeat(32);

function progressMemory(){
 let now=Date.parse("2026-10-04T08:00:00Z");const sent=[];
 class Clock extends Date{static now(){return now;}}
 const context={window:{fetch:async()=>{},addEventListener(){}},parent:{postMessage:value=>sent.push(value)},document:{addEventListener(){}},
  location:{search:"?account="+key,origin:"http://localhost",href:"http://localhost/tools"},Date:Clock,URL,URLSearchParams,setTimeout,clearTimeout};
 vm.createContext(context);vm.runInContext(fs.readFileSync("vghks_bot/static/progress.js","utf8"),context);
 const progress=context.window.JobProgress;
 progress.publish({id:"large",status:"running",members:[{soap:"x".repeat(1000000)}],items:[{payload:"large"}],progress:{done:1,total:2,records:["large"]}});
 assert.equal(progress.size(),1);assert.ok(JSON.stringify(sent.at(-1)).length<2000);
 assert.equal(sent.at(-1).runs[0].members,undefined);assert.equal(sent.at(-1).runs[0].items,undefined);
 assert.equal(sent.at(-1).runs[0].progress.records,undefined);
 progress.publish({id:"large",status:"completed",finished_at:new Date(now).toISOString()});
 now+=4999;progress.prune();assert.equal(progress.size(),1);
 now+=2;progress.prune();assert.equal(progress.size(),0);
 progress.publish({id:"failed",status:"failed",finished_at:new Date(now).toISOString()});
 now+=14999;progress.prune();assert.equal(progress.size(),1);
 now+=2;progress.prune();assert.equal(progress.size(),0);
 for(let i=0;i<100;i++)progress.publish({id:String(i),status:"completed",finished_at:new Date(now-10000).toISOString()});
 assert.equal(progress.size(),0,"expired terminal jobs were retained");
}

function parentSharing(){
 const source=fs.readFileSync("vghks_bot/static/bot.js","utf8"),start=source.indexOf("window.WorkspacePoll=(()=>{"),end=source.indexOf("})();",start)+5;
 const messages=[],listeners=new Map(),frames=[0,1].map((_,i)=>({hidden:!!i,dataset:{account:key},getClientRects(){return this.hidden?[]:[{}];},contentWindow:{postMessage:(value,origin)=>messages.push({value,origin})}}));
 const context={window:{addEventListener:(type,handler)=>listeners.set(type,handler)},document:{hidden:false,getElementById:name=>frames[name==="moduleFrame"?0:1]},location:{origin:"http://localhost"},account:key};
 vm.createContext(context);vm.runInContext(source.slice(start,end),context);
 const poll=context.window.WorkspacePoll;
 poll.publish({active_count:1,tasks:[],runs:[]},key);
 assert.equal(messages.filter(row=>row.value.type==="bot:workspace-status").length,1);
 assert.equal(poll.active(),true);
 const event={origin:"http://localhost",source:frames[0].contentWindow,data:{type:"bot:workspace-watch",account:key,ids:[id]}};
 listeners.get("message")({...event,origin:"http://evil"});assert.equal(poll.watched().length,0);
 listeners.get("message")({...event,data:{...event.data,account:"c".repeat(32)}});assert.equal(poll.watched().length,0);
 listeners.get("message")(event);assert.equal(poll.watched()[0],id);
 context.document.hidden=true;poll.visibility();
 assert.equal(messages.filter(row=>row.value.type==="bot:workspace-status").length,1,"hidden frames received a polling update");
 poll.publish({active_count:0,tasks:[],runs:[]},key);assert.equal(poll.active(),false);
}

async function pollingCadence(){
 const source=fs.readFileSync("vghks_bot/static/bot.js","utf8"),start=source.indexOf("async function poll(){"),end=source.indexOf("async function initialize()",start);
 const delays=[],listeners=new Map();let reads=0,busy=false;
 const context={pollTimer:0,stopped:false,account:key,pollBusy:false,document:{hidden:false,addEventListener:(type,handler)=>listeners.set(type,handler)},
  page:"patients",window:{WorkspacePoll:{active:()=>busy,visibility(){}}},refresh:async()=>{reads++;},maybeRecoverConnection:async()=>{},applyReadonly(){},say(){},
  setTimeout:(_,delay)=>{delays.push(delay);return delays.length;},clearTimeout(){}};
 vm.createContext(context);vm.runInContext(source.slice(start,end),context);
 await context.poll();assert.equal(delays.at(-1),10000);
 busy=true;await context.poll();assert.equal(delays.at(-1),2000);
 context.document.hidden=true;await context.poll();assert.equal(reads,2);
}

async function embeddedRefresh(){
 const source=fs.readFileSync("vghks_bot/static/app.js","utf8"),start=source.indexOf("async function refresh()"),end=source.indexOf('window.addEventListener("jobprogressaction"',start);
 let requests=0,updates=0;
 const context={embedded:true,sharedStatus:{runs:[{id,status:"running"}]},stopped:false,history:[],activePane:"analysis",selectedRun:"",config:{},embeddedAccount:key,
  active:new Set(["running","queued","cancelling"]),watchedRuns:()=>[],parent:{postMessage(){}},window:{JobProgress:{publish(){}}},location:{origin:"http://localhost"},
  api:async()=>{requests++;throw new Error("shared status caused another HTTP request");},updateAccountProgress(){},refreshWorkspace:async()=>{updates++;},notify(){},URLSearchParams};
 vm.createContext(context);vm.runInContext(source.slice(start,end),context);
 await context.refresh();assert.equal(requests,0);assert.equal(updates,1);
}

async function detailVisibility(){
 const events=new Map(),timers=[];
 const context={window:{fetch:async()=>{},addEventListener:(type,callback)=>events.set(type,callback)},parent:{},
  document:{hidden:true,addEventListener:(type,callback)=>events.set(type,callback)},
  location:{search:"?account="+key,origin:"http://localhost",href:"http://localhost/tools"},Date,URL,URLSearchParams,
  setTimeout:(callback,delay)=>{timers.push({callback,delay});},clearTimeout(){}};
 vm.createContext(context);vm.runInContext(fs.readFileSync("vghks_bot/static/progress.js","utf8"),context);
 const progress=context.window.JobProgress;let resumed=false;
 const pending=progress.ready().then(()=>{resumed=true;});await Promise.resolve();assert.equal(resumed,false);
 const visible={origin:context.location.origin,source:context.parent,data:{type:"bot:workspace-visibility",account:key,visible:false}};
 events.get("message")(visible);context.document.hidden=false;events.get("visibilitychange")();
 await Promise.resolve();assert.equal(resumed,false,"a hidden tool continued polling");
 events.get("message")({...visible,origin:"http://evil",data:{...visible.data,visible:true}});
 await Promise.resolve();assert.equal(resumed,false);
 events.get("message")({...visible,data:{...visible.data,account:id,visible:true}});
 await Promise.resolve();assert.equal(resumed,false);
 events.get("message")({...visible,data:{...visible.data,visible:true}});await pending;assert.equal(resumed,true);
 const waiting=progress.wait();assert.equal(timers.at(-1).delay,2000);timers.at(-1).callback();await waiting;
}

async function incrementalResolution(){
 const requests=[],resolved=[],failures=[],nodes=new Map();
 const summaries=[{status:"running",done:0},{status:"running",done:1},{status:"running",done:1},{status:"completed",done:2}];
 let current;
 const document={getElementById(name){if(!nodes.has(name))nodes.set(name,{value:"",addEventListener(){},replaceChildren(){}});return nodes.get(name);}};
 const context={window:{},document,JobProgress:{pending(){},show(){},ready:async()=>{},wait:async()=>{}},setTimeout,clearTimeout};
 vm.createContext(context);vm.runInContext(fs.readFileSync("vghks_bot/static/adaptive-identifiers.js","utf8"),context);
 let finish;const completed=new Promise(resolve=>{finish=resolve;});
 const tokens=context.window.PatientTokens.create({input:"input",status:"status",kind:()=>"mrn",canLookup:()=>true,
  request:async(path)=>{requests.push(path);if(path==="/tasks/start")return {task_id:id};
   if(path.includes("summary=1")){assert.ok(summaries.length);current=summaries.shift();return {...current,id};}
   return {...current,id,items:[{input:"1",mrn:"1",status:"resolved"},...(current.done===2?[{input:"2",mrn:"2",status:"resolved"}]:[])]};},
  onResolved:item=>{resolved.push({input:item.input,status:current.status});if(item.input==="2")finish();},
  onFailed:(...values)=>failures.push(values)});
 tokens.add(["1","2"]);await completed;await Promise.resolve();
 assert.deepEqual(resolved,[{input:"1",status:"running"},{input:"2",status:"completed"}],"patient tokens lost incremental resolution");
 assert.equal(failures.length,0);assert.equal(requests.filter(path=>path.includes("summary=1")).length,4);
 assert.equal(requests.filter(path=>path.startsWith("/tasks/detail?id=")).length,2,"unchanged progress fetched full patient items");
}

(async()=>{progressMemory();parentSharing();await pollingCadence();await embeddedRefresh();await detailVisibility();await incrementalResolution();console.log("Shared polling, detail summaries, visibility, cadence and memory checks passed");})().catch(error=>{console.error(error);process.exitCode=1;});
