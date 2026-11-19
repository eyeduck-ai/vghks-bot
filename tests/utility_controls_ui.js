"use strict";
// Exercise the real controls with synthetic DOM, clocks and attachment identities.
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const accountA = "a".repeat(32), accountB = "b".repeat(32);
const deferred = () => {let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};};

function environment() {
  const observers=[], mutations=[], intervals=new Map(), timeouts=new Map(), requests=[], writes=[];
  let intervalId=0, timeoutId=0, wall=Date.parse("2026-10-27T00:00:00Z"), monotonic=100;
  class Target {
    constructor(){this.listeners=new Map();}
    addEventListener(type,callback,options={}){const list=this.listeners.get(type)||[];list.push({callback,once:!!options.once});this.listeners.set(type,list);}
    dispatchEvent(event){event.target ||= this;event.currentTarget=this;for(const item of [...(this.listeners.get(event.type)||[])]){if(item.once)this.listeners.set(event.type,(this.listeners.get(event.type)||[]).filter(value=>value!==item));item.callback(event);}return true;}
  }
  class Element extends Target {
    constructor(tag,doc){super();this.tagName=tag.toUpperCase();this.ownerDocument=doc;this.children=[];this.parentElement=null;this.dataset={};this.attributes={};this.className="";this.hidden=false;this._text="";this._open=false;this.returnValue="";this.showCount=0;this.scrollTop=0;this.scrollLeft=0;
      const property=name=>name.startsWith("--")?name:name.replace(/-([a-z])/g,(_whole,letter)=>letter.toUpperCase());
      this.style={setProperty(name,value){this[property(name)]=String(value);},removeProperty(name){delete this[property(name)];}};
      this.classList={contains:name=>this.className.split(/\s+/).includes(name),add:(...names)=>{this.className=[...new Set([...this.className.split(/\s+/).filter(Boolean),...names])].join(" ");},remove:(...names)=>{this.className=this.className.split(/\s+/).filter(value=>!names.includes(value)).join(" ");},toggle:(name,force)=>{const present=force??!this.classList.contains(name);if(present)this.classList.add(name);else this.classList.remove(name);return present;}};
    }
    get textContent(){return this._text+this.children.map(child=>child.textContent).join("");}
    set textContent(value){this.replaceChildren();this._text=String(value);writes.push(this);}
    get firstElementChild(){return this.children[0]||null;}
    get isConnected(){return this===this.ownerDocument.body||!!this.parentElement?.isConnected;}
    get open(){return this._open;}
    set open(value){const changed=this._open!==!!value;this._open=!!value;if(changed)mutations.push({target:this,attributeName:"open"});}
    get hidden(){return !!this._hidden;}
    set hidden(value){const changed=!!this._hidden!==!!value;this._hidden=!!value;if(changed)mutations.push({target:this,attributeName:"hidden"});}
    _place(child,index,preserveFocus=false){
      const original=child.ownerDocument,focused=original?.activeElement;
      if(child.parentElement){const previous=child.parentElement;previous.children.splice(previous.children.indexOf(child),1);}
      if(!preserveFocus&&child.contains(focused))original.activeElement=original.body;
      child.parentElement=this;const adopt=value=>{value.ownerDocument=this.ownerDocument;for(const nested of value.children)adopt(nested);};adopt(child);
      this.children.splice(index,0,child);
    }
    append(...values){for(const child of values)this._place(child,this.children.length);}
    insertBefore(child,before){this._place(child,before?this.children.indexOf(before):this.children.length);}
    prepend(...values){for(let index=values.length-1;index>=0;index--)this._place(values[index],0);}
    moveBefore(child,before){this._place(child,before?this.children.indexOf(before):this.children.length,true);}
    remove(){if(this.parentElement){const parent=this.parentElement;parent.children.splice(parent.children.indexOf(this),1);this.parentElement=null;}if(this.contains(this.ownerDocument.activeElement))this.ownerDocument.activeElement=this.ownerDocument.body;}
    replaceChildren(...values){for(const child of [...this.children])child.remove();this._text="";this.append(...values);}
    all(){return this.children.flatMap(child=>[child,...child.all()]);}
    contains(value){return value===this||this.all().includes(value);}
    matches(selector){
      if(selector==="dialog[open]")return this.tagName==="DIALOG"&&this.open;
      if(selector===":first-child")return this.parentElement?.firstElementChild===this;
      if(selector.startsWith("#"))return this.id===selector.slice(1);
      if(selector.startsWith("."))return this.classList.contains(selector.slice(1));
      if(selector.includes(".")){const [tag,cls]=selector.split(".");return this.tagName===tag.toUpperCase()&&this.classList.contains(cls);}
      return this.tagName===selector.toUpperCase();
    }
    querySelectorAll(selector){
      const matches=(child,part)=>{
        if(part.includes(" > ")){const [left,right]=part.split(" > ");return child.matches(right)&&!!child.parentElement?.matches(left);}
        if(part.includes(" ")){const split=part.lastIndexOf(" "),left=part.slice(0,split),right=part.slice(split+1);if(!child.matches(right))return false;for(let parent=child.parentElement;parent;parent=parent.parentElement)if(matches(parent,left))return true;return false;}
        return child.matches(part);
      };
      return this.all().filter(child=>selector.split(",").some(part=>matches(child,part.trim())));
    }
    querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
    closest(selector){for(let current=this;current;current=current.parentElement)if(current.matches(selector))return current;return null;}
    setAttribute(name,value){this.attributes[name]=String(value);if(name==="id")this.id=String(value);}
    getAttribute(name){return this.attributes[name]??null;}
    hasAttribute(name){return name in this.attributes;}
    removeAttribute(name){delete this.attributes[name];}
    getBoundingClientRect(){return this.rect||{left:0,top:0,right:1280,bottom:900,width:1280,height:this.hidden?0:48};}
    getClientRects(){if(!this.isConnected)return [];for(let current=this;current;current=current.parentElement)if(current.hidden||current.tagName==="DIALOG"&&!current.open)return [];return [{}];}
    focus(){this.ownerDocument.activeElement=this;}
    showModal(){assert.equal(this.open,false,"attempted to open the same dialog twice");this.showCount++;this.returnValue="";this.open=true;(this.all().find(child=>child.autofocus)||this).focus();}
    close(value){if(!this.open)return;if(value!==undefined)this.returnValue=String(value);this.open=false;this.beforeClose?.();this.dispatchEvent({type:"close"});}
  }
  class Document extends Target {
    constructor(){super();this.body=new Element("body",this);this.activeElement=this.body;this.documentElement={clientWidth:1280,clientHeight:900};this.defaultView={innerWidth:1280,innerHeight:900};}
    createElement(tag){return new Element(tag,this);}
    querySelectorAll(selector){return this.body.querySelectorAll(selector);}
    getElementById(id){return this.body.all().find(value=>value.id===id)||null;}
  }
  const document=new Document(),window=new Target();window.parent=window;
  class Clock extends Date {static now(){return wall;}}
  const context={window,parent:window,document,Date:Clock,Intl,URL,URLSearchParams,Set,Map,AbortController,
    performance:{now:()=>monotonic},location:{origin:"http://synthetic.invalid",pathname:"/",search:"",href:"http://synthetic.invalid/"},
    CustomEvent:class {constructor(type,options={}){this.type=type;this.detail=options.detail;}},
    MutationObserver:class {constructor(callback){this.callback=callback;observers.push(this);}observe(target){this.target=target;}disconnect(){this.target=null;this.disconnected=true;}},
    ResizeObserver:class {constructor(callback){this.callback=callback;}observe(){}},
    setInterval:(callback,delay)=>{intervals.set(++intervalId,{callback,delay});return intervalId;},clearInterval:id=>intervals.delete(id),
    setTimeout:(callback,delay)=>{timeouts.set(++timeoutId,{callback,delay});return timeoutId;},clearTimeout:id=>timeouts.delete(id),
    fetch:(...args)=>{requests.push(args);throw new Error("Synthetic utility test must not make network requests");},
    api:(...args)=>{requests.push(args);throw new Error("Synthetic utility test must not call the API");},
  };
  window.fetch=context.fetch;vm.createContext(context);
  const load=path=>vm.runInContext(fs.readFileSync(path,"utf8"),context);
  const flush=()=>{const records=mutations.splice(0);for(const observer of observers){const visible=records.filter(record=>observer.target?.contains(record.target));if(visible.length)observer.callback(visible);}};
  const advance=(ms,wallDelta=ms)=>{monotonic+=ms;wall+=wallDelta;for(const {callback} of [...intervals.values()])callback();};
  return {context,document,window,load,flush,advance,intervals,timeouts,requests,writes,observers,Document,
    element:(tag,parent=document.body)=>{const value=parent.ownerDocument.createElement(tag);parent.append(value);return value;},now:()=>wall};
}

async function clocksWhilePending(){
  const s=environment();s.load("vghks_bot/static/clinical-ui.js");const ui=s.window.ClinicalUI;
  const waiting=deferred();let settled=false;const operation=waiting.promise.then(()=>{settled=true;});
  const indicator=ui.loading("載入合成報告"),seconds=indicator.querySelector(".clinical-loading-elapsed");s.document.body.append(indicator);
  const accessible=node=>node.getAttribute("aria-hidden")==="true"?"":node._text+node.children.map(accessible).join("");
  assert.equal(indicator.getAttribute("role"),"status");assert.equal(seconds.getAttribute("aria-hidden"),"true");
  assert.equal(seconds.getAttribute("aria-live"),null);assert.equal(seconds.textContent,"0 秒");
  const announcement=accessible(indicator);s.writes.length=0;
  s.advance(1000);assert.equal(seconds.textContent,"1 秒");assert.equal(settled,false);
  s.advance(2000,-120000);assert.equal(seconds.textContent,"3 秒","wall clock correction reset the elapsed seconds");
  s.advance(2000,3600000);assert.equal(seconds.textContent,"5 秒","wall clock jump skipped elapsed seconds");
  assert.equal(accessible(indicator),announcement,"a one-second tick changed the live status announcement");
  assert.ok(s.writes.every(node=>node===seconds),"elapsed updates rewrote the loading label or live container");
  assert.equal(settled,false,"the slow request unexpectedly completed while advancing the UI clock");
  indicator.remove();s.advance(1000);assert.equal(s.intervals.size,0,"detached loading indicator retained a timer");
  waiting.resolve();await operation;

  const modal=s.element("dialog");modal.showModal();const second=ui.loading("模態報告"),third=ui.elapsed({startedAt:s.now()-4000});modal.append(second);s.document.body.append(third);
  assert.equal(s.intervals.size,1,"simultaneous indicators created independent perpetual intervals");
  assert.equal(third.textContent,"4 秒");modal.close();s.advance(1000);
  assert.equal(third.textContent,"5 秒");assert.equal(second.querySelector(".clinical-loading-elapsed").textContent,"0 秒","closed dialog clock kept ticking");
  third.dispose();assert.equal(s.intervals.size,0,"closing/disposal did not release the final interval");
  const final=ui.loading();s.document.body.append(final);final.dispose();final.dispose();assert.equal(s.intervals.size,0,"dispose was not idempotent");
}

function dockPlacement(){
  const s=environment();s.load("vghks_bot/static/utility-dock.js");const dockUI=s.window.UtilityDock;
  const comparison=s.element("div"),progress=s.element("div"),feedback=s.element("div"),focus=s.document.createElement("button");comparison.append(focus);comparison.id="selection";progress.id="progress";feedback.id="feedback";feedback.hidden=true;
  dockUI.attach("comparison",comparison,20);dockUI.attach("progress",progress,10);dockUI.attach("feedback",feedback,30);
  const dock=s.document.getElementById("utilityDock");
  assert.deepEqual([...dock.children].sort((a,b)=>Number(a.style.order)-Number(b.style.order)).map(value=>value.id),["progress","selection","feedback"]);
  assert.equal(dock.parentElement,s.document.body);assert.equal(dock.hidden,false);
  comparison.scrollTop=87;focus.focus();dockUI.sync();assert.equal(s.document.activeElement,focus);assert.equal(dock.children.find(value=>value.id==="selection"),comparison);
  const reader=s.element("dialog"),nested=s.element("dialog");reader.id="reader";nested.id="nested";
  reader.rect={left:200,top:100,right:800,bottom:600,width:600,height:500};
  reader.showModal();s.flush();assert.equal(dock.parentElement,reader);focus.focus();
  assert.equal(reader.children.at(-1),dock,"modal controls must occupy a dedicated trailing footer");
  assert.equal(dock.classList.contains("utility-dock-modal"),true);
  assert.equal(reader.classList.contains("utility-dock-active"),true);
  assert.equal(dock.style.right,undefined);assert.equal(dock.style.bottom,undefined);assert.equal(dock.style.maxWidth,undefined,"fixed modal coordinates would overlay report content");
  const reserve=reader.style["--utility-dock-height"];assert.match(reserve,/^[1-9]\d*px$/);
  nested.showModal();s.flush();assert.equal(dock.parentElement,nested,"controls did not follow the most recently opened modal");
  assert.equal(reader.classList.contains("utility-dock-active"),false,"underlying reader cannot retain an active footer after it moves");
  assert.equal(reader.style["--utility-dock-height"],reserve,"an overlay collapsed the still-open reader's reserved area");
  assert.equal(comparison.scrollTop,87,"moving the dock recreated or reset its contents");
  focus.focus();dockUI.sync();assert.equal(s.document.activeElement,focus,"unchanged host update lost focus");
  nested.close();s.flush();assert.equal(dock.parentElement,reader);assert.equal(s.document.activeElement,focus,"same-document move lost the existing focused control");
  assert.equal(nested.style["--utility-dock-height"],undefined,"closed reader retained a stale layout reservation");
  const observer=s.observers.find(observer=>observer.target===s.document.body);
  reader.dataset.view="diagnostics";
  observer.callback([{target:reader,attributeName:"data-view"}]);
  assert.equal(dock.hidden,true,"DEBUG retained the shared modal footer");
  assert.equal(reader.classList.contains("utility-dock-host"),false,"DEBUG retained a blank footer");
  reader.dataset.view="history";
  observer.callback([{target:reader,attributeName:"data-view"}]);
  assert.equal(dock.hidden,false,"switching the same dialog back to a report lost its controls");
  const confirmation=s.element("dialog");confirmation.id="confirmDialog";confirmation.showModal();s.flush();
  assert.equal(dock.hidden,true,"confirmation left clinical controls actionable above it");assert.equal(reader.style["--utility-dock-height"],reserve);
  confirmation.close();s.flush();assert.equal(dock.hidden,false);assert.equal(dock.parentElement,reader);assert.equal(dock.children.find(value=>value.id==="selection"),comparison);
  reader.close();s.flush();assert.equal(dock.parentElement,s.document.body);assert.equal(reader.style["--utility-dock-height"],undefined);assert.equal(reader.classList.contains("utility-dock-host"),false);
  assert.equal(dock.classList.contains("utility-dock-modal"),false,"body dock retained its modal flow layout");
  assert.equal(dock.style.right,undefined);assert.equal(dock.style.bottom,undefined);assert.equal(dock.style.maxWidth,undefined,"dialog bounds leaked into the body dock");
  comparison.hidden=progress.hidden=feedback.hidden=true;dockUI.sync();assert.equal(dock.hidden,true);assert.equal(s.document.body.style["--utility-dock-height"],"0px");

  const iframe=s.element("iframe");iframe.id="moduleFrame";iframe.dataset.account=accountA;iframe.contentDocument=new s.Document();
  const inner=iframe.contentDocument.createElement("dialog");iframe.contentDocument.body.append(inner);inner.showModal();progress.hidden=false;
  dockUI.context(accountB);assert.equal(dock.parentElement,s.document.body,"a different account's frame captured the dock");
  dockUI.context(accountA);assert.equal(dock.parentElement,inner,"same-origin active tool modal did not receive the dock");assert.equal(dock.children.find(value=>value.id==="selection"),comparison);
  inner.close();s.flush();assert.equal(dock.parentElement,s.document.body);assert.equal(inner.style["--utility-dock-height"],undefined);
  inner.showModal();s.flush();const oldDocument=iframe.contentDocument,oldObserver=s.observers.find(observer=>observer.target===oldDocument.body);
  assert.equal(inner.isConnected,true);assert.equal(inner.open,true);assert.ok(inner.style["--utility-dock-height"]);
  iframe.contentDocument=new s.Document();const replacement=iframe.contentDocument.createElement("dialog");iframe.contentDocument.body.append(replacement);replacement.showModal();dockUI.sync();
  assert.equal(oldObserver.disconnected,true,"navigated iframe document retained its MutationObserver");
  assert.equal(inner.style["--utility-dock-height"],undefined,"old but connected iframe document retained its layout reservation");
  assert.equal(inner.classList.contains("utility-dock-host"),false);assert.equal(dock.parentElement,replacement);assert.equal(dock.children.find(value=>value.id==="selection"),comparison);
  inner.close();s.flush();assert.equal(dock.parentElement,replacement,"retired iframe mutation displaced the live dock");

  replacement.close();s.flush();
  const compare=s.element("dialog");compare.id="fileCompareDialog";
  progress.hidden=feedback.hidden=true;comparison.hidden=false;dockUI.sync();
  compare.showModal();s.flush();
  assert.equal(dock.parentElement,compare);
  assert.ok(comparison.classList.contains("utility-dock-slot-hidden"),"comparison dialog exposed its duplicate shortcut");
  assert.ok(dock.hidden,"comparison-only dock reserved room for a suppressed shortcut");
  assert.equal(compare.classList.contains("utility-dock-host"),false);
  progress.hidden=false;dockUI.sync();
  assert.equal(dock.hidden,false,"comparison hid active task progress");
  assert.equal(progress.classList.contains("utility-dock-slot-hidden"),false);
  compare.close();s.flush();
  assert.equal(dock.parentElement,s.document.body);
  assert.equal(comparison.classList.contains("utility-dock-slot-hidden"),false,"closing comparison did not restore its shortcut");
  assert.equal(comparison.hidden,false);
}

function patientDockPlacement(){
  const s=environment(),page=s.element("section");page.id="patientsPage";page.className="page";
  const collection=s.element("div",page);collection.className="collection-bar";
  const summary=s.element("strong",collection),actions=s.element("div",collection),launch=s.element("button",actions);
  launch.textContent="病歷檢閱";
  s.load("vghks_bot/static/utility-dock.js");const ui=s.window.UtilityDock;
  const progress=s.element("aside"),comparison=s.element("div");
  ui.attach("progress",progress,10);ui.attach("comparison",comparison,20);
  const dock=s.document.getElementById("utilityDock");
  assert.equal(dock.parentElement,collection,"patient progress did not share the shortcut surface");
  assert.equal(collection.firstElementChild,dock,"progress must precede shortcuts even on the mobile block layout");
  assert.equal(dock.classList.contains("utility-dock-inline"),true);
  assert.equal(summary.parentElement,collection);assert.equal(launch.parentElement,actions);
  page.hidden=true;s.flush();assert.equal(dock.parentElement,s.document.body,"navigation left utilities in a hidden patient page");
  assert.equal(dock.classList.contains("utility-dock-inline"),false);
  assert.equal(collection.classList.contains("utility-dock-host"),false);
  assert.equal(collection.style["--utility-dock-height"],undefined);
  page.hidden=false;s.flush();assert.equal(dock.parentElement,collection);
  assert.equal(s.document.body.classList.contains("utility-dock-host"),false,"returning to inline status retained floating padding");
  assert.equal(s.document.body.style["--utility-dock-height"],undefined);
  const reader=s.element("dialog");reader.showModal();s.flush();assert.equal(dock.parentElement,reader);
  assert.equal(dock.classList.contains("utility-dock-inline"),false);assert.equal(dock.classList.contains("utility-dock-modal"),true);
  reader.close();s.flush();assert.equal(dock.parentElement,collection);assert.equal(collection.firstElementChild,dock);
  const settings=s.element("dialog");settings.dataset.utilityDock="hide";settings.showModal();s.flush();assert.equal(dock.hidden,true);
  settings.close();s.flush();assert.equal(dock.parentElement,collection);assert.equal(dock.hidden,false);
  progress.hidden=comparison.hidden=true;ui.sync();assert.equal(dock.hidden,true);assert.equal(collection.classList.contains("utility-dock-host"),false);
}

function progressClockCleanup(){
  const s=environment();s.load("vghks_bot/static/clinical-ui.js");s.load("vghks_bot/static/utility-dock.js");s.load("vghks_bot/static/progress.js");
  const progress=s.window.JobProgress;progress.context(accountA);
  progress.publish({id:"SYNTHETIC",kind:"history",name:"合成報告",status:"running",created_at:new Date(s.now()).toISOString(),progress:{done:0,total:1}},accountA);
  const card=s.document.getElementById("floatingJobProgress"),firstClock=card.querySelector(".clinical-loading-elapsed");
  assert.equal(card.hidden,false);assert.equal(s.intervals.size,1);s.advance(1000);assert.equal(firstClock.textContent,"1 秒");
  const expand=card.querySelector("button");expand.focus();
  progress.publish({id:"SYNTHETIC",status:"running",progress:{done:1,total:2}},accountA);
  const currentClock=card.querySelector(".clinical-loading-elapsed");assert.notEqual(currentClock,firstClock);assert.equal(s.intervals.size,1,"rebuilding a job card leaked its previous elapsed timer");
  assert.equal(s.document.activeElement,card.querySelector("button"),"job status refresh dropped keyboard focus");
  progress.context("");assert.equal(card.hidden,true);assert.equal(card.children.length,0);assert.equal(s.intervals.size,0,"hidden empty-account progress card retained a live elapsed timer");
  progress.context(accountA);assert.equal(s.intervals.size,1,"returning to a running task failed to restart its elapsed display");
  progress.publish({id:"SYNTHETIC",status:"completed",finished_at:new Date(s.now()).toISOString()},accountA);assert.equal(s.intervals.size,0,"terminal task retained its elapsed timer");
  progress.publish({id:"LATE",kind:"list",status:"running",revision:4},accountA);
  assert.equal(s.intervals.size,1);
  s.advance(332000);
  progress.publish({id:"LATE",kind:"list",status:"partial",revision:5,finished_at:new Date(s.now()-330000).toISOString()},accountA);
  assert.equal(s.intervals.size,0,"a late terminal snapshot failed to stop the active clock immediately");
  progress.publish({id:"LATE",status:"running",revision:4},accountA);
  assert.equal(card.hidden,true,"a stale running snapshot revived an expired progress card");
  assert.equal(s.requests.length,0);
}

function compareSetup({embedded=false}={}){
  const s=environment();s.messages=[];s.sizes=[];
  if(embedded)s.window.parent={postMessage:(value,origin)=>s.messages.push({value,origin})};
  s.load("vghks_bot/static/utility-dock.js");
  const download=(url,name)=>{const link=s.document.createElement("a");link.className="attachment-control";link.href=url;link.download=name+".pdf";return link;};
  s.context.ClinicalUI={comparisonName:item=>item.date,pdfURL:url=>url,fileName:name=>name+".pdf",downloadLink:download,
    assetTools:({url,name})=>{const tools=s.document.createElement("span");tools.append(download(url,name));return tools;}};
  s.load("vghks_bot/static/local-session.js");
  s.context.ResizeObserver=class{constructor(callback){this.callback=callback;this.targets=new Set();s.sizes.push(this);}observe(target){this.targets.add(target);}unobserve(target){this.targets.delete(target);}};
  s.load("vghks_bot/static/file-compare.js");
  const ui=s.window.FileCompare;s.window.UtilityDock.context(accountA);ui.context(accountA,"SYN001","合成病人");
  const item=(digit,overrides={})=>({account:accountA,mrn:"SYN001",digest:digit.repeat(64),mime:"application/pdf",name:"合成附件 "+digit,date:"2026-10-27",...overrides});
  return {...s,ui,item,prompt:()=>s.document.getElementById("fileCompareClearDialog")};
}

async function comparisonPatientDetails(){
  const s=compareSetup(),{ui,item}=s,details=(id)=>s.document.getElementById(id);
  s.document.createTextNode=text=>{const node=s.document.createElement("span");node.textContent=text;return node;};
  s.load("vghks_bot/static/cataract-numeric.js");
  s.window.SOAPView={create:record=>{const node=s.document.createElement("div");node.textContent=record.soap;return node;}};
  const frame=s.element("iframe"), first=deferred(), second=deferred();frame.id="cataractFrame";
  frame.setAttribute("src","/tool");frame.dataset.account=accountA;frame.dataset.cohort="SYNTHETIC";
  const response=(mrn,numeric)=>({member:{account_id:accountA,mrn},numeric,
    latest_soap:{record:{date:"2026-11-01",section:"眼科",soap:"合成 SOAP " + mrn}}});
  const measurement=[{exams:["Va"],cells:[{exam:"Va",side:"OD",raw:0}],date:"2026-11-01"}];
  const placeholders=[{exams:["Va"],cells:[],unparsed:true,values:["2026-11-01","OD","N/A"]}];
  const requests=[first.promise,second.promise,Promise.resolve(response("SYN001",measurement)),
    Promise.resolve(response("SYN002",placeholders))];
  s.context.api=async(path,payload)=>{assert.equal(path,"/analysis/results");assert.equal(payload.cohort_id,"SYNTHETIC");return requests.shift();};
  const settle=()=>new Promise(resolve=>setImmediate(resolve));
  ui.add(item("1"));ui.open();
  assert.equal(details("fileCompareDetailChoices").hidden,true);
  details("fileCompareDialog").close();ui.open();
  second.resolve(response("SYN001",measurement));await settle();
  const [numeric,soap]=details("fileCompareDetailChoices").children;
  assert.equal(numeric.getAttribute("aria-pressed"),"true","actual measurements should take priority, including zero");
  const pane=details("fileComparePanes").querySelector("iframe");
  first.resolve(response("SYN001",[]));await settle();
  assert.equal(numeric.getAttribute("aria-pressed"),"true","a previous opening replaced the current patient details");
  soap.dispatchEvent({type:"click"});
  assert.equal(details("fileCompareNumeric").hidden,true);assert.equal(details("fileCompareSoap").hidden,false);
  assert.match(details("fileCompareSoap").textContent,/合成 SOAP SYN001/);
  const rail=details("fileCompareDetailRail"),toggle=details("fileCompareRailToggle");
  assert.ok(rail.contains(toggle),"the expanded toggle should fit inside the rail");
  toggle.focus();toggle.dispatchEvent({type:"click"});
  assert.equal(toggle.getAttribute("aria-expanded"),"false");assert.equal(rail.hidden,true);
  assert.equal(toggle.hidden,false,"collapse hid its only expand control");
  assert.ok(toggle.parentElement.classList.contains("file-compare-heading"),"the collapsed toggle should use the dialog header");
  assert.equal(s.document.activeElement,toggle,"collapsing moved keyboard focus off the toggle");
  toggle.dispatchEvent({type:"click"});assert.equal(toggle.getAttribute("aria-expanded"),"true");
  assert.ok(rail.contains(toggle),"the toggle should return to the reopened rail");
  assert.equal(s.document.activeElement,toggle,"expanding moved keyboard focus off the toggle");
  assert.equal(details("fileComparePanes").querySelector("iframe"),pane,"patient detail switching reloaded a PDF");
  details("fileCompareDialog").close();ui.open();await settle();
  assert.equal(soap.getAttribute("aria-pressed"),"true","reopening comparison discarded the chosen detail view");
  numeric.dispatchEvent({type:"click"});
  ui.context(accountA,"SYN002");ui.add(item("2",{mrn:"SYN002"}));ui.open();await settle();
  assert.equal(soap.getAttribute("aria-pressed"),"true","placeholder-only data should default to SOAP for a new patient");
  assert.match(details("fileCompareSoap").textContent,/合成 SOAP SYN002/);
  assert.equal(requests.length,0,"changing detail tabs incurred an extra analysis request");
}

function comparisonZoomReset(){
  const s=compareSetup(),{ui,item}=s;ui.add(item("1"));ui.add(item("2",{mime:"image/png"}));ui.open();
  const panes=s.document.getElementById("fileComparePanes"),pdf=panes.children[0].querySelector("iframe"),image=panes.children[1].querySelector("img");
  const pdfPane=panes.children[0],scroll=pdf.parentElement,source=pdf.src;
  assert.equal(pdfPane.querySelector(".file-compare-zoom"),null,"PDF retained an outer zoom control");
  assert.match(pdfPane.querySelector(".file-compare-pdf-hint").textContent,/檢閱器/);
  assert.ok(pdfPane.querySelectorAll(".file-compare-pane-actions button").every(button=>!["放大檔案","縮小檔案"].includes(button.getAttribute("aria-label"))));
  scroll.scrollTop=173;scroll.scrollLeft=31;
  const reset=panes.children[1].querySelector(".file-compare-zoom");reset.focus();
  panes.children[1].querySelectorAll(".file-compare-pane-actions button")[4].dispatchEvent({type:"click"});
  assert.equal(reset.textContent,"125%");assert.equal(image.style.width,"125%");assert.equal(image.style.height,"auto");
  reset.dispatchEvent({type:"click"});assert.equal(reset.textContent,"100%");assert.equal(reset.tagName,"BUTTON");assert.equal(reset.type,"button");assert.match(reset.getAttribute("aria-label"),/100%/);
  assert.equal(pdf.style.width,"100%");assert.equal(pdf.style.height,"100%");assert.equal(panes.children[0].querySelector("iframe"),pdf,"image zoom recreated the PDF browsing context");assert.equal(pdf.src,source);
  assert.equal(scroll.scrollTop,173);assert.equal(scroll.scrollLeft,31);assert.equal(s.document.activeElement,reset,"zoom dropped the reset button's focus");
  panes.children[1].querySelectorAll(".file-compare-pane-actions button")[2].dispatchEvent({type:"click"});
  panes.children[1].querySelector(".file-compare-zoom").dispatchEvent({type:"click"});assert.equal(image.style.width,"100%");assert.equal(image.style.height,"auto");
  const frame={contentWindow:{postMessage(){}}},message={origin:s.context.location.origin,source:null,data:{type:"bot:compare-zoom",account:accountA,mrn:"SYN001",digest:item("2").digest,reset:true}};
  panes.children[1].querySelectorAll(".file-compare-pane-actions button")[4].dispatchEvent({type:"click"});
  ui.receive(message,frame,accountA);assert.equal(reset.textContent,"125%","untrusted source reset a comparison");
  message.source=frame.contentWindow;message.data.mrn="OTHER";ui.receive(message,frame,accountA);assert.equal(reset.textContent,"125%");
  message.data.mrn="SYN001";message.data.account=accountB;ui.receive(message,frame,accountA);assert.equal(reset.textContent,"125%");
  message.data.account=accountA;ui.receive(message,frame,accountA);assert.equal(reset.textContent,"100%");assert.equal(s.requests.length,0);
  message.data.digest=item("1").digest;message.data.reset=false;message.data.delta=.25;ui.receive(message,frame,accountA);
  assert.equal(pdf.style.width,"100%","a stale PDF zoom message resized its browsing context");assert.equal(pdf.src,source);

  const inner=compareSetup({embedded:true}),host=inner.element("div"),emit=(rows,overrides={})=>inner.window.dispatchEvent({type:"message",origin:inner.context.location.origin,source:inner.window.parent,data:{type:"bot:compare-state",account:accountA,mrn:"SYN001",items:rows,...overrides}});
  inner.ui.renderInline(host);const one=inner.item("3",{zoom:1.5}),two=inner.item("4",{mime:"image/png",zoom:1});emit([one,two]);
  const inlinePane=host.querySelectorAll(".file-compare-pane")[0],inlinePDF=inlinePane.querySelector("iframe"),imagePane=host.querySelectorAll(".file-compare-pane")[1],inlineReset=imagePane.querySelector(".file-compare-zoom");
  assert.equal(inlinePane.querySelector(".file-compare-zoom"),null);assert.equal(inlinePDF.style.width,"100%","legacy PDF zoom state resized the native viewer");
  inlinePDF.parentElement.scrollTop=219;inlinePDF.parentElement.scrollLeft=45;inlineReset.focus();
  inlineReset.dispatchEvent({type:"click"});assert.equal(inner.messages.at(-1).value.reset,true);assert.equal(inner.messages.at(-1).value.type,"bot:compare-zoom");assert.equal(inner.messages.at(-1).value.digest,two.digest);
  emit([{...one,zoom:1},two]);assert.equal(inlineReset.textContent,"100%");assert.equal(inlinePDF.style.width,"100%");assert.equal(inlinePDF.style.height,"100%");
  assert.equal(inlinePane.querySelector("iframe"),inlinePDF,"parent zoom state rebuilt an inline PDF");assert.equal(inlinePDF.parentElement.scrollTop,219);assert.equal(inlinePDF.parentElement.scrollLeft,45);assert.equal(inner.document.activeElement,inlineReset);
  emit([{...two,zoom:1.25},{...one,zoom:2}]);assert.equal(inlinePane.style.order,"1");assert.equal(inlinePane.querySelector("iframe"),inlinePDF);assert.equal(inlineReset.textContent,"125%");assert.equal(inlinePDF.style.width,"100%");
  imagePane.querySelectorAll(".file-compare-pane-actions button")[2].dispatchEvent({type:"click"});assert.equal(inner.messages.at(-1).value.delta,-.25);
  host.querySelector(".file-compare-inline-tabs").children[0].dispatchEvent({type:"click"});assert.equal(inlinePane.querySelector("iframe"),inlinePDF,"switching inline tabs recreated the PDF");
  emit([{...one,zoom:3}],{account:accountB});assert.equal(inlineReset.textContent,"125%","foreign account state changed inline zoom");assert.equal(inner.requests.length,0);
}

function comparisonFitModes(){
 const s=compareSetup(),{ui,item}=s;ui.add(item("1",{mime:"image/png"}));ui.add(item("2"));ui.open();
 const panes=s.document.getElementById("fileComparePanes"),pane=panes.children[0],image=pane.querySelector("img"),scroll=image.parentElement,pdf=panes.children[1].querySelector("iframe"),source=image.src;
 const observer=s.sizes[0],fit=mode=>pane.querySelectorAll(".file-compare-fit").find(button=>button.dataset.fit===mode),percent=pane.querySelector(".file-compare-zoom");
 assert.equal(fit("height").disabled,true);Object.assign(image,{naturalWidth:1000,naturalHeight:2000});Object.assign(scroll,{clientWidth:400,clientHeight:600});image.dispatchEvent({type:"load"});
 assert.equal(image.style.width,"400px");assert.equal(percent.textContent,"40%");assert.equal(fit("height").disabled,false);
 scroll.scrollTop=171;fit("height").dispatchEvent({type:"click"});assert.equal(image.style.width,"300px");assert.equal(percent.textContent,"30%");assert.equal(scroll.scrollTop,0);
 assert.equal(fit("height").getAttribute("aria-pressed"),"true");scroll.clientHeight=800;observer.callback([{target:scroll}]);assert.equal(image.style.width,"400px");
 pane.querySelectorAll(".file-compare-pane-actions button")[4].dispatchEvent({type:"click"});assert.equal(image.style.width,"500px");assert.equal(fit("height").getAttribute("aria-pressed"),"false");
 scroll.scrollTop=121;scroll.clientHeight=400;observer.callback([{target:scroll}]);assert.equal(image.style.width,"500px");assert.equal(scroll.scrollTop,121);
 percent.dispatchEvent({type:"click"});assert.equal(image.style.width,"1000px","100% did not restore the original image dimensions");
 fit("width").dispatchEvent({type:"click"});scroll.clientWidth=600;observer.callback([{target:scroll}]);assert.equal(image.style.width,"600px");
 const states=[],frame={contentWindow:{postMessage:value=>states.push(value)}},event={origin:s.context.location.origin,source:null,data:{type:"bot:compare-zoom",account:accountA,mrn:"SYN001",digest:item("1").digest,fit:"height"}};
 ui.receive(event,frame,accountA);assert.equal(image.style.width,"600px","an untrusted frame changed the fitting mode");
 event.source=frame.contentWindow;ui.receive(event,frame,accountA);assert.equal(image.style.width,"200px");assert.equal(states.at(-1).items[0].fit,"height");
 const inner=compareSetup({embedded:true}),host=inner.element("div");inner.ui.renderInline(host);
 const emit=()=>inner.window.dispatchEvent({type:"message",origin:inner.context.location.origin,source:inner.window.parent,data:states.at(-1)});emit();
 const inlinePane=host.querySelector(".file-compare-pane"),inlineImage=inlinePane.querySelector("img"),inlineScroll=inlineImage.parentElement;
 Object.assign(inlineImage,{naturalWidth:1000,naturalHeight:2000});Object.assign(inlineScroll,{clientWidth:800,clientHeight:600});inlineImage.dispatchEvent({type:"load"});assert.equal(inlineImage.style.width,"300px");
 inlineScroll.scrollTop=150;inlinePane.querySelectorAll(".file-compare-fit")[0].dispatchEvent({type:"click"});assert.equal(inlineScroll.scrollTop,0);assert.equal(inner.messages.at(-1).value.fit,"width");
 event.data=inner.messages.at(-1).value;ui.receive(event,frame,accountA);emit();assert.equal(inlineImage.style.width,"800px");assert.equal(image.style.width,"600px");
 inlinePane.querySelectorAll(".file-compare-pane-actions button")[4].dispatchEvent({type:"click"});assert.equal(inner.messages.at(-1).value.scale,.8);
 event.data=inner.messages.at(-1).value;ui.receive(event,frame,accountA);emit();assert.equal(inlineImage.style.width,"1000px");assert.equal(image.style.width,"1000px","embedded zoom used the modal's viewport dimensions");
 assert.equal(image.src,source);assert.equal(pane.querySelector("img"),image);assert.equal(pdf.style.width,"100%");assert.equal(observer.targets.size,1);
 ui.remove(item("1").digest);assert.equal(observer.targets.size,0,"removed image retained a resize observer");inner.ui.clear();assert.equal(inner.sizes[0].targets.size,0);
}

function comparisonSwitchFocus(){
  const s=compareSetup(),{ui,item}=s;
  ui.add(item("1"));ui.add(item("2"));ui.open();
  const buttons=s.document.getElementById("fileCompareTabs"),[first,second]=buttons.children;
  assert.equal(buttons.getAttribute("role"),"group");
  assert.equal(buttons.getAttribute("aria-label"),"比較檔案切換");
  first.focus();first.dispatchEvent({type:"click"});
  assert.equal(buttons.children[0],first);assert.equal(s.document.activeElement,first);
  assert.equal(first.getAttribute("aria-pressed"),"true");assert.equal(second.getAttribute("aria-pressed"),"false");
  const panes=s.document.getElementById("fileComparePanes");
  panes.children[0].querySelectorAll(".file-compare-pane-actions button")[1].dispatchEvent({type:"click"});
  assert.equal(buttons.children[1],first);assert.equal(s.document.activeElement,first,"reordering replaced or blurred a focused control");
  ui.remove(item("1").digest);
  assert.equal(s.document.activeElement,second,"removal left keyboard focus outside the remaining comparison choices");

  const inner=compareSetup({embedded:true}),host=inner.element("div"),one=inner.item("3"),two=inner.item("4");
  inner.ui.renderInline(host);
  const send=items=>inner.window.dispatchEvent({type:"message",origin:inner.context.location.origin,source:inner.window.parent,
    data:{type:"bot:compare-state",account:accountA,mrn:"SYN001",items}});
  send([one,two]);
  const switches=host.querySelector(".file-compare-inline-tabs"),inline=switches.children[1];
  inline.focus();inline.dispatchEvent({type:"click"});send([two,one]);
  assert.equal(switches.children[0],inline);assert.equal(inner.document.activeElement,inline);
  assert.equal(inline.getAttribute("aria-pressed"),"true","a reordered frame changed the selected attachment");
}

async function scanPreviewLifecycle(){
  const s=environment(),ready=deferred(),older=deferred(),newer=deferred(),stale=deferred(),secondPoll=deferred(),calls=[];
  let indexReads=0,polls=0;
  s.context.matchMedia=()=>({matches:false});
  s.context.ClinicalUI={pdfURL:url=>url,assetTools:()=>s.document.createElement("a")};
  s.window.JobProgress={ready:()=>ready.promise,wait:()=>secondPoll.promise,pending(){},publish(){}};
  s.load("vghks_bot/static/local-session.js");
  s.load("vghks_bot/static/scan-browser.js");
  const scans=s.window.ScanBrowser, options={account:accountA,mrn:"SYN001",review_task_id:"SYN-TASK",resource:"case_scans",
    allowFetch:()=>true,request:async(path,values)=>{
      calls.push({path,values});
      if(path==="/tasks/start")return {task_id:"SYN-INDEX"};
      if(path.startsWith("/tasks/detail"))return {status:++polls<3?"running":"completed",revision:polls<3?1:2};
      if(values.resource==="scan_asset")return {A:older,B:newer,C:stale}[values.reference].promise;
      indexReads++;
      return {complete:indexReads>1,data:{scans:["A","B","C"].map(id=>({id,category_label:id,date:indexReads>2?"2026-11-08":"2026-11-07"}))}};
    }};
  const opening=scans.open(options);
  const tick=async()=>{for(let index=0;index<14;index++)await Promise.resolve();};
  await tick();
  const dialog=s.document.getElementById("scanBrowserDialog"),catalog=dialog.querySelector(".scan-browser-catalog");
  const [first,second]=catalog.querySelectorAll(".scan-row").map(row=>row.querySelector("button"));
  first.dispatchEvent({type:"click"});second.dispatchEvent({type:"click"});
  const file=digit=>({data:{mime:"application/pdf",digest:digit.repeat(64)}});
  newer.resolve(file("b"));await tick();
  const frame=dialog.querySelector("iframe"),host=frame.parentElement.parentElement;
  assert.match(frame.src,/b{64}/);older.resolve(file("a"));await tick();
  assert.equal(dialog.querySelector("iframe"),frame,"an older selection replaced a more recent preview");
  const contents=s.document.getElementById("scanBrowserContent"),replace=contents.replaceChildren.bind(contents);
  let previewDetached=false;
  contents.replaceChildren=(...children)=>{replace(...children);previewDetached ||= !frame.isConnected;};
  const oldRow=catalog.querySelector(".scan-row");
  ready.resolve();await tick();
  assert.equal(catalog.querySelector(".scan-row"),oldRow,"unchanged scan index was rendered again");
  const finish=deferred();s.window.JobProgress.wait=()=>finish.promise;
  secondPoll.resolve();await tick();
  assert.equal(indexReads,2,"unchanged task revision reloaded the scan index");
  finish.resolve();await opening;
  assert.notEqual(catalog.querySelector(".scan-row"),oldRow,"updated scan index was discarded");
  assert.equal(host.querySelector("iframe"),frame);assert.equal(previewDetached,false,"refresh detached the selected PDF iframe");

  const late=catalog.querySelectorAll(".scan-row")[2].querySelector("button");
  late.dispatchEvent({type:"click"});dialog.close();
  await scans.open({...options,mrn:"SYN002",request:async()=>({complete:true,data:{scans:[]}})});
  stale.resolve({data:null});await tick();
  assert.equal(calls.filter(call=>call.path==="/tasks/start").length,1,"closed preview started a stale attachment download");
  assert.equal(dialog.querySelector(".scan-error"),null);
}

async function scanArchiveSelection(){
  const s=environment(), reads=[], archives=[], pending=deferred();
  const asset={mime:"application/pdf",digest:"c".repeat(64)}, rows=[
    {id:"A",category_label:"已存 A",eye:true,asset},
    {id:"A",category_label:"另一類 A",eye:true,asset},
    {id:"B",category_label:"缺檔 B",eye:true},
  ];
  let fetched=false, block=false;
  s.window.ReviewArchive={limit:100,download:async ({scope,ids,current})=>{
    assert.equal(current(),true);
    archives.push({scope,ids:Array.from(ids)});
    return {files:ids.length-Number(!fetched),missing:Number(!fetched)};
  }};
  s.window.JobProgress={ready:async()=>{},publish(){}};
  s.load("vghks_bot/static/scan-browser.js");
  const options={archive:true,account:accountA,mrn:"SYN001",review_task_id:"SYN-TASK",resource:"scans",allowFetch:()=>true,
    request:async(path,values,settings)=>{
      reads.push({path,values,settings});
      if(path==="/tasks/start"){fetched=true;return {task_id:"SYN-SCAN"};}
      if(path.startsWith("/tasks/detail"))return {status:"completed"};
      if(values.resource==="scan_asset")return block?pending.promise:{data:fetched?asset:null};
      return {data:{records:rows,scans:rows.slice(1),links_complete:true},complete:true};
    }};
  const scans=s.window.ScanBrowser;
  await scans.open(options);
  const dialog=s.document.getElementById("scanBrowserDialog"),catalog=dialog.querySelector(".scan-browser-catalog");
  const set=(node,key,value,type)=>{node[key]=value;node.dispatchEvent({type});};
  const tick=async()=>{for(let index=0;index<20;index++)await Promise.resolve();};
  const toolbar=()=>dialog.querySelector(".review-archive-toolbar"), action=()=>toolbar().querySelector("button");
  const choices=()=>dialog.querySelectorAll(".scan-row input");
  const status=()=>toolbar().querySelector(".review-archive-status").textContent;
  assert.equal(action().disabled,true);
  set(choices()[0],"checked",true,"change");
  assert.equal(choices()[1].checked,true,"the same scan under two labels must share one selection");
  assert.equal(toolbar().querySelector("input").indeterminate,true);
  set(choices()[2],"checked",true,"change");
  const search=catalog.querySelector(".scan-filters input");set(search,"value","已存","input");
  assert.equal(choices().length,1);assert.match(toolbar().querySelector(".review-archive-count").textContent,/已選 2 份/);
  action().dispatchEvent({type:"click"});await tick();
  assert.deepEqual(archives[0].ids,["A","B"]);
  assert.equal(archives[0].scope.resource,"scans");
  assert.equal(reads.filter(call=>call.path==="/tasks/start").length,1);
  assert.equal(reads.find(call=>call.path==="/tasks/start").values.reference,"B");
  assert.match(status(),/已下載 2 個原檔/);

  dialog.close();fetched=false;
  await scans.open({...options,resource:"case_scans",reference:"SYN-VISIT",allowFetch:()=>false});
  assert.equal(action().disabled,true,"an old patient list selection must reset on reopen");
  set(toolbar().querySelector("input"),"checked",true,"change");
  action().dispatchEvent({type:"click"});await tick();
  assert.deepEqual(archives[1].ids,["A","B"]);
  assert.equal(archives[1].scope.reference,"SYN-VISIT");
  assert.equal(reads.filter(call=>call.path==="/tasks/start").length,1,"offline archive attempted a source fetch");
  assert.match(status(),/1 筆未取得/);
  toolbar().querySelectorAll("button")[1].dispatchEvent({type:"click"});
  assert.equal(action().disabled,true);assert.equal(choices()[0].checked,false);

  block=true;
  await scans.open(options);set(choices()[2],"checked",true,"change");
  action().dispatchEvent({type:"click"});await tick();
  dialog.close();await scans.open({...options,archive:false,mrn:"SYN002"});
  pending.resolve({data:null});await tick();
  assert.equal(reads.findLast(call=>call.values.resource==="scan_asset").settings.signal.aborted,true);
  assert.equal(dialog.querySelector(".review-archive-toolbar"),null);
  assert.equal(archives.length,2,"a closed scan list must not download a stale archive");
  assert.equal(reads.filter(call=>call.path==="/tasks/start").length,1);
}

async function confirmationSafety(){
  const s=compareSetup(),{ui,item}=s;ui.add(item("1"));ui.add(item("2"));
  const first=ui.requestClear(),prompt=s.prompt();
  assert.equal(ui.requestClear(),first,"double click created a second confirmation promise");assert.equal(prompt.showCount,1);
  const cancel=prompt.all().find(node=>node.tagName==="BUTTON"&&node.value==="cancel"),confirm=prompt.all().find(node=>node.tagName==="BUTTON"&&node.value==="clear");
  assert.equal(cancel.autofocus,true);assert.notEqual(confirm.autofocus,true);assert.equal(s.document.activeElement,cancel,"the destructive action received default focus");
  assert.match(s.document.getElementById("fileCompareClearMessage").textContent,/2 份.*已保存.*保留/);
  assert.equal(s.document.getElementById("utilityDock").hidden,true);
  prompt.close("cancel");assert.equal(await first,false);assert.equal(ui.count(),2);assert.equal(prompt.returnValue,"");
  const escape=ui.requestClear();prompt.close();assert.equal(await escape,false);assert.equal(ui.count(),2,"Escape-style close cleared the selection");
  const confirmed=ui.requestClear();prompt.close("clear");assert.equal(await confirmed,true);assert.equal(ui.count(),0);assert.equal(ui.patient(),"SYN001");
  assert.equal(s.requests.length,0,"clearing local selection called an asset or mutation API");ui.add(item("1"));assert.equal(ui.has(item("1").digest),true,"previously selected saved asset could not be reselected");
  assert.throws(()=>ui.add(item("2",{account:accountB})),/帳號/);assert.throws(()=>ui.add(item("2",{mrn:"SYN002"})),/病人/);

  for(const change of ["different-patient","different-account","switch-back","queue-add","queue-roundtrip"]){
    ui.context(accountA,"SYN001");ui.clear();ui.add(item("1"));const clearing=ui.requestClear();
    if(change==="different-patient"){ui.context(accountA,"SYN002");ui.add(item("3",{mrn:"SYN002"}));}
    if(change==="different-account"){ui.context(accountB,"SYN001");ui.add(item("3",{account:accountB}));}
    if(change==="switch-back"){ui.context(accountB,"SYN002");ui.context(accountA,"SYN001");ui.add(item("1"));}
    if(change.startsWith("queue-")){ui.add(item("2"));if(change==="queue-roundtrip")ui.remove(item("2").digest);}
    const retained=ui.count();prompt.close("clear");assert.equal(await clearing,false,change+" accepted an outdated confirmation");assert.equal(ui.count(),retained,change+" cleared a changed selection");
  }
  assert.equal(s.requests.length,0);
  const frame=s.element("iframe");frame.id="cataractFrame";frame.dataset.account=accountA;frame.contentDocument=new s.Document();
  const reader=frame.contentDocument.createElement("dialog");frame.contentDocument.body.append(reader);reader.showModal();s.window.UtilityDock.context(accountA);s.flush();
  const dock=reader.querySelector(".utility-dock"),group=dock.querySelector(".compare-tray-group");
  assert.ok(group,"compare controls did not enter the embedded reader");assert.equal(s.document.getElementById("fileCompareTrayGroup"),null,"synthetic DOM did not model cross-document adoption");
  ui.context(accountA,"SYN001");ui.clear();ui.add(item("4"));ui.add(item("5"));ui.remove(item("5").digest);
  assert.equal(ui.count(),1);assert.equal(group.querySelector(".compare-tray").hidden,false,"adopted compare control stopped rendering");
  const embeddedPrompt=ui.requestClear();assert.equal(s.prompt().open,true);s.prompt().close("cancel");assert.equal(await embeddedPrompt,false);assert.equal(ui.count(),1);
  const tray=group.querySelector(".compare-tray"),compare=s.document.getElementById("fileCompareDialog");
  // Native dialog restoration can lose focus when its old trigger has been adopted.
  compare.beforeClose=()=>{s.document.activeElement=s.document.body;frame.contentDocument.activeElement=frame.contentDocument.body;};
  tray.focus();tray.dispatchEvent({type:"click"});assert.equal(compare.open,true);assert.equal(tray.ownerDocument,s.document);
  compare.close();assert.equal(tray.ownerDocument,frame.contentDocument);assert.equal(frame.contentDocument.activeElement,tray,"closing compare lost the original adopted tray trigger");
  assert.equal(group.querySelector(".compare-tray"),tray,"restoring focus recreated the original comparison control");
  tray.dispatchEvent({type:"click"});ui.context(accountA,"SYN002");
  assert.equal(compare.open,false);assert.notEqual(frame.contentDocument.activeElement,tray,"patient switch restored focus to an old patient's trigger");
  assert.equal(s.requests.length,0,"cross-document updates unexpectedly fetched an attachment");
}

async function comparisonWorkspaceVisibility(){
  const s=compareSetup(),{ui,item}=s;ui.add(item("1"));
  const tray=s.document.getElementById("fileCompareTray"),group=s.document.getElementById("fileCompareTrayGroup"),feedback=s.document.getElementById("fileCompareFeedback");
  const showPageSource=fs.readFileSync("vghks_bot/static/bot.js","utf8").match(/^function showPage\(name\).*$/m)[0];
  Object.assign(s.context,{$$:()=>[],Choices:{sync(){}},updateReviewLayout(){},rememberAccountView(){},captureCataractScroll(){}});
  vm.runInContext("let page='patients';"+showPageSource,s.context);
  const navigate=name=>vm.runInContext("showPage("+JSON.stringify(name)+")",s.context);
  assert.equal(group.hidden,false);assert.equal(feedback.hidden,false);
  for(const destination of ["approvals","earnings","surgery","operatingRoom","sheetManagement","tasks"]){
    navigate(destination);
    assert.equal(group.hidden,true,destination+" retained the comparison shortcut");assert.equal(tray.hidden,true);
    assert.equal(feedback.hidden,true,destination+" retained an outpatient notification");
    assert.equal(ui.count(),1,destination+" cleared the retained queue");assert.equal(ui.has(item("1").digest),true);
    const modal=s.element("dialog");modal.showModal();s.flush();
    assert.equal(group.hidden,true,destination+" dialog reintroduced the comparison shortcut");
    ui.open();assert.equal(s.document.getElementById("fileCompareDialog").open,false,"hidden context opened a stale comparison");
    modal.close();s.flush();navigate("patients");assert.equal(group.hidden,false);assert.equal(tray.hidden,false);
    assert.equal(feedback.hidden,true,"returning to outpatient resurrected an expired notification");
  }
  navigate("earnings");ui.add(item("2"));
  assert.equal(ui.count(),2);assert.equal(group.hidden,true,"late attachment completion exposed the shortcut in another system");assert.equal(feedback.hidden,true);
  for(const destination of ["patients","tool","review","module","library","tags"]){
    navigate("approvals");navigate(destination);assert.equal(group.hidden,false,destination+" did not restore the outpatient shortcut");
    assert.equal(ui.count(),2);assert.equal(ui.has(item("2").digest),true);
  }
  ui.open();const compare=s.document.getElementById("fileCompareDialog");assert.equal(compare.open,true);
  navigate("surgery");assert.equal(compare.open,false,"system switch retained an open outpatient comparison");assert.equal(ui.count(),2);
  navigate("review");const clearing=ui.requestClear();assert.equal(s.prompt().open,true);
  navigate("earnings");assert.equal(s.prompt().open,false);assert.equal(await clearing,false);assert.equal(ui.count(),2,"system switch confirmed queue removal");
  navigate("review");assert.equal(group.hidden,false);assert.equal(feedback.hidden,true);
  assert.equal(s.requests.length,0,"presentation changes issued attachment or mutation requests");
}

(async()=>{await clocksWhilePending();dockPlacement();patientDockPlacement();progressClockCleanup();await comparisonPatientDetails();comparisonZoomReset();comparisonFitModes();comparisonSwitchFocus();await scanPreviewLifecycle();await scanArchiveSelection();await confirmationSafety();await comparisonWorkspaceVisibility();process.stdout.write("Utility control clocks, dock placement, confirmations and selection isolation checks passed\nModal footer, native PDF zoom guidance and persistent image zoom reset checks passed\nComparison patient detail switching, shortcut visibility and retained selection checks passed\nPatient progress and shortcut surface lifecycle checks passed\n");})().catch(error=>{console.error(error);process.exitCode=1;});
