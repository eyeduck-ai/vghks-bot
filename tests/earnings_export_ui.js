"use strict";
// Exercise the shipped export and request lifecycle with synthetic reports and clocks.
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const html=fs.readFileSync("vghks_bot/static/bot.html","utf8"),script=fs.readFileSync("vghks_bot/static/earnings.js","utf8"),bot=fs.readFileSync("vghks_bot/static/bot.js","utf8");
const tick=async()=>{for(let i=0;i<40;i++)await Promise.resolve();};
const deferred=()=>{let resolve,reject;const promise=new Promise((done,fail)=>{resolve=done;reject=fail;});return {promise,resolve,reject};};
const reports=[{id:"PERF_1",kind:"performance",period:"11509",month:"2026-09",version_count:1},{id:"PERF_2",kind:"performance",period:"11510",month:"2026-10",version_count:1},{id:"PAY_1",kind:"payroll",period:"11510",month:"2026-10",version_count:1}];
const names={performance:"醫療績點明細",payroll:"專勤工作獎金明細"};
const overview=()=>({view:"active",has_archive:false,total:reports.length,reports,months:["2026-10","2026-09"],tasks:[],credentials:{configured:true,remembered:true},monitor:null});
const exported=values=>({files:Object.keys(names).flatMap(kind=>{const chosen=reports.filter(row=>row.kind===kind&&values.ids.includes(row.id)),months=[...new Set(chosen.map(row=>row.month.replace("-","")))].sort(),period=months.length>1?`${months[0]}-${months.at(-1)}`:months[0];return chosen.length?[{kind,filename:`VGHKS-${names[kind]}_${period}.${values.format}`,mime:values.format==="json"?"application/json":"text/csv;charset=utf-8",content:JSON.stringify({reports:chosen})}]:[];})});

function surface(){
 const ids=new Map(),calls=[],created=new Map(),revoked=[],timers=new Map(),clicks=[];let timerId=0,urlId=0,handler=async(path,values)=>{if(path.endsWith("/overview"))return overview();if(path.endsWith("/export"))return exported(values);throw new Error("Unexpected synthetic request "+path);};
 class Element{
  constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.parentElement=null;this.dataset={};this.attributes={};this.events=new Map();this._text="";this.value="";this.checked=false;this.disabled=false;this.hidden=false;}
  get id(){return this._id;}set id(value){this._id=value;ids.set(value,this);}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join("");}set textContent(value){this.replaceChildren();this._text=String(value);}
  get isConnected(){return this===body||!!this.parentElement?.isConnected;}
  setAttribute(name,value){this.attributes[name]=String(value);if(name==="id")this.id=value;}
  getAttribute(name){return this.attributes[name]??null;}
  removeAttribute(name){delete this.attributes[name];}
  append(...children){for(const child of children){if(child.parentElement)child.remove();child.parentElement=this;this.children.push(child);}}
  replaceChildren(...children){for(const child of this.children)child.parentElement=null;this.children=[];this._text="";this.append(...children);}
  remove(){if(this.parentElement){const parent=this.parentElement;parent.children.splice(parent.children.indexOf(this),1);this.parentElement=null;}}
  after(child){const parent=this.parentElement;child.parentElement=parent;parent.children.splice(parent.children.indexOf(this)+1,0,child);}
  contains(child){return this===child||this.all().includes(child);}
  all(){return this.children.flatMap(child=>[child,...child.all()]);}
  querySelectorAll(selector){return this.all().filter(child=>selector==="a[download]"?child.tagName==="A"&&!!child.download:child.tagName===selector.toUpperCase());}
  addEventListener(type,callback){this.events.set(type,[...(this.events.get(type)||[]),callback]);}
  async emit(type){if(type==="click"&&this.disabled)return;document.activeElement=this;await Promise.all((this.events.get(type)||[]).map(callback=>callback({target:this,preventDefault(){}})));await tick();}
  click(){clicks.push(this);return this.emit("click");}
 }
 const body=new Element("body"),document={body,activeElement:body};
 for(const match of html.matchAll(/<([\w-]+)[^>]*\bid="([^"]+)"[^>]*>/g)){const element=new Element(match[1]);element.id=match[2];body.append(element);}
 const node=(tag,text,cls)=>{const element=new Element(tag);if(text!==undefined)element.textContent=text;if(cls)element.className=cls;return element;};
 const context={window:{LocalSession:{headers:()=>({"X-Local-Session":"synthetic"})}},document,root:{read_only:false,csrf:"synthetic",context:"DB_A"},work:{online:false,tasks:[]},account:"ACCOUNT_A",epoch:0,DOMException,AbortController,Blob,console,
  URL:{createObjectURL(blob){const url="blob:synthetic/"+(++urlId);created.set(url,blob);return url;},revokeObjectURL(url){revoked.push(url);}},
  setTimeout(callback,delay){timers.set(++timerId,{callback,delay});return timerId;},clearTimeout(id){timers.delete(id);},
  $:selector=>ids.get(selector.slice(1)),node,Node:Element,Option:function(text,value){const option=node("option",text);option.value=value;return option;},
  btn:(text,callback,cls)=>{const button=node("button",text,cls);button.addEventListener("click",callback);return button;},
  check:(label,checked,change)=>{const input=node("input");input.type="checkbox";input.checked=checked;input.setAttribute("aria-label",label);input.addEventListener("change",()=>change(input.checked));return input;},
  table:(headers,rows)=>{const table=node("table");for(const values of rows){const row=node("tr");for(const value of values)row.append(value instanceof Element?value:node("td",value));table.append(row);}return table;},
  empty:text=>node("p",text),Choices:{sync(){}},JobProgress:{visible:()=>false},inProgress:new Set(["running","queued"]),labels:{},
  cataractScroll:null,draftTimer:null,manualLookup:{reset(){}},manualErrors:new Map(),$$:()=>[],loginFields:(key,reveal)=>{context.entryOptions={key,reveal};},
  say:(message,error=false)=>{const target=ids.get(error?"notice":"toast");target.textContent=message;target.hidden=false;},
  fetch:async(path,options)=>{const values=options.body===undefined?undefined:JSON.parse(options.body);calls.push({path,values,options});const result=await handler(path,values,options);return {ok:result?.syntheticError===undefined,json:async()=>result?.syntheticError?{error:result.syntheticError}:result};},
 };
 vm.createContext(context);
 const first=bot.indexOf("async function act("),last=bot.indexOf("const time=",first);
 assert.ok(first>=0&&last>first,"Could not locate the production request/action helpers");
 const entryFirst=bot.indexOf("function showEntry("),entryLast=bot.indexOf("async function login(",entryFirst);
 assert.ok(entryFirst>=0&&entryLast>entryFirst,"Could not locate the production login-entry helper");
 vm.runInContext(bot.slice(first,last),context);vm.runInContext(script,context);vm.runInContext(bot.slice(entryFirst,entryLast),context);
 const s={context,ui:context.window.Earnings,q:id=>ids.get(id),calls,created,revoked,timers,clicks,handle:callback=>{handler=callback;},exports:()=>calls.filter(call=>call.path.endsWith("/export")),links:()=>ids.get("earningsExportDownloads").querySelectorAll("a[download]"),async select(chosen){for(const row of reports){const box=ids.get("earningsReports").querySelectorAll("input").find(input=>input.attributes["aria-label"]===`選取 ${names[row.kind]} ${row.period}`);box.checked=chosen.includes(row.id);await box.emit("change");}},async timeout(){const timeout=[...timers].find(([,value])=>value.delay===60000);assert.ok(timeout,"Export did not install its 60-second timeout");timers.delete(timeout[0]);timeout[1].callback();await tick();}};
 return s;
}

async function splitAndRepeat(){
 const s=surface();await s.ui.open();await s.select(["PERF_1","PAY_1"]);await s.q("earningsExportJson").emit("click");
 assert.deepEqual(s.links().map(link=>link.download),["VGHKS-醫療績點明細_202609.json","VGHKS-專勤工作獎金明細_202610.json"]);assert.equal(s.clicks.length,0,"Export attempted an automatic download after async preparation");assert.match(s.q("earningsExportStatus").textContent,/2/);
 const old=s.links().map(link=>link.href);for(const link of s.links()){const payload=JSON.parse(await s.created.get(link.href).text());assert.equal(new Set(payload.reports.map(row=>row.kind)).size,1);}
 await s.select(["PERF_2"]);await s.q("earningsExportJson").emit("click");assert.deepEqual(s.exports().map(call=>call.values.ids),[["PERF_1","PAY_1"],["PERF_2"]]);assert.equal(s.links().length,1);assert.equal(JSON.parse(await s.created.get(s.links()[0].href).text()).reports[0].id,"PERF_2");for(const url of old)assert.ok(s.revoked.includes(url),"Old export object URL leaked");
 await s.select(["PERF_2","PERF_1","PAY_1"]);await s.q("earningsExportJson").emit("click");assert.deepEqual(s.links().map(link=>link.download),["VGHKS-醫療績點明細_202609-202610.json","VGHKS-專勤工作獎金明細_202610.json"]);
 await s.select(["PAY_1"]);await s.q("earningsExportCsv").emit("click");assert.equal(s.links()[0].download,"VGHKS-專勤工作獎金明細_202610.csv");assert.equal(s.q("earningsExportJson").dataset.busy,undefined);assert.equal(s.q("earningsExportCsv").dataset.busy,undefined);
 const urls=s.links().map(link=>link.href);s.ui.reset();assert.equal(s.links().length,0);assert.equal(s.q("earningsExportStatus").textContent,"");for(const url of urls)assert.ok(s.revoked.includes(url));
}

async function pendingRenderAndErrorRetry(){
 const s=surface(),gate=deferred();let waiting=true;
 s.handle(async(path,values)=>{if(path.endsWith("/overview"))return overview();if(waiting)return gate.promise;return exported(values);});
 await s.ui.open();await s.select(["PERF_1"]);const pending=s.q("earningsExportJson").emit("click");await tick();
 assert.ok(s.exports()[0].options.signal instanceof AbortSignal,"Request helper dropped the export abort signal");assert.match(s.q("earningsExportStatus").textContent,/準備|處理|產生|匯出/);assert.equal(s.q("earningsExportJson").disabled,true);assert.equal(s.q("earningsExportCsv").disabled,true);
 await s.ui.poll();await s.select(["PAY_1"]);assert.equal(s.q("earningsExportJson").disabled,true,"Polling/render cleared in-flight JSON state");assert.equal(s.q("earningsExportCsv").disabled,true,"Selection render cleared in-flight CSV state");await s.q("earningsExportCsv").emit("click");assert.equal(s.exports().length,1,"A second format raced the pending export");
 gate.resolve({syntheticError:"合成匯出失敗"});await pending;assert.match(s.q("notice").textContent+s.q("earningsExportStatus").textContent,/合成匯出失敗/);assert.equal(s.q("earningsExportJson").disabled,false);assert.equal(s.q("earningsExportCsv").disabled,false);assert.equal(s.q("earningsExportJson").dataset.busy,undefined);
 waiting=false;await s.q("earningsExportJson").emit("click");assert.equal(s.exports().length,2);assert.deepEqual(s.exports()[1].values.ids,["PAY_1"]);assert.equal(s.links().length,1);
}

async function timeoutAndLateResponse(){
 const s=surface(),gate=deferred();let waiting=true;
 s.handle(async(path,values,options)=>{if(path.endsWith("/overview"))return overview();if(!waiting)return exported(values);return new Promise((resolve,reject)=>{options.signal.addEventListener("abort",()=>reject(options.signal.reason||new DOMException("Aborted","AbortError")),{once:true});gate.promise.then(resolve,reject);});});
 await s.ui.open();await s.select(["PERF_1"]);const pending=s.q("earningsExportJson").emit("click");await tick();await s.timeout();await pending;assert.equal(s.exports()[0].options.signal.aborted,true);assert.match(s.q("notice").textContent+s.q("earningsExportStatus").textContent,/匯出逾時/);assert.equal(s.q("earningsExportJson").disabled,false);assert.equal(s.q("earningsExportJson").dataset.busy,undefined);
 waiting=false;await s.select(["PAY_1"]);await s.q("earningsExportJson").emit("click");const current=s.links()[0].href;gate.resolve(exported({ids:["PERF_1"],format:"json"}));await tick();assert.equal(s.links()[0].href,current,"Late timed-out response replaced the retried export");assert.equal(s.timers.size,0,"Completed export left a cancellation timer running");
}

async function resetDiscardsOldScope(){
 const s=surface(),gate=deferred();let waiting=true;
 s.handle(async(path,values)=>{if(path.endsWith("/overview"))return overview();return waiting?gate.promise:exported(values);});
 await s.ui.open();await s.select(["PERF_1"]);const pending=s.q("earningsExportJson").emit("click");await tick();s.ui.reset();assert.equal(s.exports()[0].options.signal.aborted,true,"Reset did not cancel the old scope's export");assert.equal(s.links().length,0);
 gate.resolve(exported({ids:["PERF_1"],format:"json"}));await pending;assert.equal(s.links().length,0,"Late old-scope response restored salary data");assert.equal(s.q("earningsExportStatus").textContent,"");waiting=false;await s.ui.open();await s.select(["PAY_1"]);await s.q("earningsExportJson").emit("click");assert.equal(s.links().length,1);assert.equal(s.links()[0].download,"VGHKS-專勤工作獎金明細_202610.json");
}

async function loginEntryClearsExports(){
 const ready=surface();await ready.ui.open();await ready.select(["PERF_1","PAY_1"]);await ready.q("earningsExportJson").emit("click");const urls=ready.links().map(link=>link.href);
 ready.context.showEntry("ACCOUNT_A",false);assert.equal(ready.links().length,0,"Logging out retained salary download links");for(const url of urls)assert.ok(ready.revoked.includes(url),"Logging out retained a live salary object URL");assert.equal(ready.q("earningsExportPanel").hidden,true);assert.equal(ready.q("earningsExportStatus").textContent,"");assert.equal(ready.context.account,"");assert.equal(ready.context.epoch,1);assert.deepEqual(ready.context.entryOptions,{key:"ACCOUNT_A",reveal:false});assert.equal(ready.q("shell").hidden,true);assert.equal(ready.q("entry").hidden,false);
 const waiting=surface();waiting.handle(async(path,_values,options)=>{if(path.endsWith("/overview"))return overview();return new Promise((_resolve,reject)=>options.signal.addEventListener("abort",()=>reject(options.signal.reason),{once:true}));});
 await waiting.ui.open();await waiting.select(["PERF_1"]);const pending=waiting.q("earningsExportJson").emit("click");await tick();waiting.context.showEntry();assert.equal(waiting.exports()[0].options.signal.aborted,true,"Login entry did not cancel a pending salary export");await pending;assert.equal(waiting.links().length,0);assert.equal(waiting.q("earningsExportStatus").textContent,"");assert.equal(waiting.q("notice").textContent,"","A cancelled old-account export displayed an error on the login screen");assert.equal(waiting.q("earningsExportJson").dataset.busy,undefined);assert.equal(waiting.timers.size,0);
}

(async()=>{await splitAndRepeat();await pendingRenderAndErrorRetry();await timeoutAndLateResponse();await resetDiscardsOldScope();await loginEntryClearsExports();console.log("Earnings export splitting, reselection, cancellation and retry checks passed");})().catch(error=>{console.error(error);process.exitCode=1;});
