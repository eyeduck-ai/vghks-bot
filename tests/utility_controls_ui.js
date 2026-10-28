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
    _place(child,index,preserveFocus=false){
      const original=child.ownerDocument,focused=original?.activeElement;
      if(child.parentElement){const previous=child.parentElement;previous.children.splice(previous.children.indexOf(child),1);}
      if(!preserveFocus&&child.contains(focused))original.activeElement=original.body;
      child.parentElement=this;const adopt=value=>{value.ownerDocument=this.ownerDocument;for(const nested of value.children)adopt(nested);};adopt(child);
      this.children.splice(index,0,child);
    }
    append(...values){for(const child of values)this._place(child,this.children.length);}
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
  const context={window,parent:window,document,Date:Clock,Intl,URL,URLSearchParams,Set,Map,
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
  assert.equal(s.requests.length,0);
}

function compareSetup({embedded=false}={}){
  const s=environment();s.messages=[];
  if(embedded)s.window.parent={postMessage:(value,origin)=>s.messages.push({value,origin})};
  s.load("vghks_bot/static/utility-dock.js");
  const download=(url,name)=>{const link=s.document.createElement("a");link.className="attachment-control";link.href=url;link.download=name+".pdf";return link;};
  s.context.ClinicalUI={comparisonName:item=>item.date,pdfURL:url=>url,fileName:name=>name+".pdf",downloadLink:download,
    assetTools:({url,name})=>{const tools=s.document.createElement("span");tools.append(download(url,name));return tools;}};
  s.load("vghks_bot/static/file-compare.js");
  const ui=s.window.FileCompare;s.window.UtilityDock.context(accountA);ui.context(accountA,"SYN001","合成病人");
  const item=(digit,overrides={})=>({account:accountA,mrn:"SYN001",digest:digit.repeat(64),mime:"application/pdf",name:"合成附件 "+digit,date:"2026-10-27",...overrides});
  return {...s,ui,item,prompt:()=>s.document.getElementById("fileCompareClearDialog")};
}

function comparisonZoomReset(){
  const s=compareSetup(),{ui,item}=s;ui.add(item("1"));ui.add(item("2",{mime:"image/png"}));ui.open();
  const panes=s.document.getElementById("fileComparePanes"),pdf=panes.children[0].querySelector("iframe"),image=panes.children[1].querySelector("img");
  const scroll=pdf.parentElement,reset=panes.children[0].querySelector(".file-compare-zoom");scroll.scrollTop=173;scroll.scrollLeft=31;reset.focus();
  panes.children[0].querySelectorAll(".file-compare-pane-actions button")[4].dispatchEvent({type:"click"});
  assert.equal(reset.textContent,"125%");assert.equal(pdf.style.width,"125%");assert.equal(pdf.style.height,"125%");
  reset.dispatchEvent({type:"click"});assert.equal(reset.textContent,"100%");assert.equal(reset.tagName,"BUTTON");assert.equal(reset.type,"button");assert.match(reset.getAttribute("aria-label"),/100%/);
  assert.equal(pdf.style.width,"100%");assert.equal(pdf.style.height,"100%");assert.equal(panes.children[0].querySelector("iframe"),pdf,"reset recreated the PDF browsing context");
  assert.equal(scroll.scrollTop,173);assert.equal(scroll.scrollLeft,31);assert.equal(s.document.activeElement,reset,"zoom dropped the reset button's focus");
  panes.children[1].querySelectorAll(".file-compare-pane-actions button")[2].dispatchEvent({type:"click"});
  panes.children[1].querySelector(".file-compare-zoom").dispatchEvent({type:"click"});assert.equal(image.style.width,"100%");assert.equal(image.style.height,"auto");
  const frame={contentWindow:{postMessage(){}}},message={origin:s.context.location.origin,source:null,data:{type:"bot:compare-zoom",account:accountA,mrn:"SYN001",digest:item("1").digest,reset:true}};
  panes.children[0].querySelectorAll(".file-compare-pane-actions button")[4].dispatchEvent({type:"click"});
  ui.receive(message,frame,accountA);assert.equal(reset.textContent,"125%","untrusted source reset a comparison");
  message.source=frame.contentWindow;message.data.mrn="OTHER";ui.receive(message,frame,accountA);assert.equal(reset.textContent,"125%");
  message.data.mrn="SYN001";message.data.account=accountB;ui.receive(message,frame,accountA);assert.equal(reset.textContent,"125%");
  message.data.account=accountA;ui.receive(message,frame,accountA);assert.equal(reset.textContent,"100%");assert.equal(s.requests.length,0);

  const inner=compareSetup({embedded:true}),host=inner.element("div"),emit=(rows,overrides={})=>inner.window.dispatchEvent({type:"message",origin:inner.context.location.origin,source:inner.window.parent,data:{type:"bot:compare-state",account:accountA,mrn:"SYN001",items:rows,...overrides}});
  inner.ui.renderInline(host);const one=inner.item("3",{zoom:1.5}),two=inner.item("4",{mime:"image/png",zoom:1});emit([one,two]);
  const inlinePane=host.querySelectorAll(".file-compare-pane")[0],inlinePDF=inlinePane.querySelector("iframe"),inlineReset=inlinePane.querySelector(".file-compare-zoom");
  inlinePDF.parentElement.scrollTop=219;inlinePDF.parentElement.scrollLeft=45;inlineReset.focus();
  inlineReset.dispatchEvent({type:"click"});assert.equal(inner.messages.at(-1).value.reset,true);assert.equal(inner.messages.at(-1).value.type,"bot:compare-zoom");assert.equal(inner.messages.at(-1).value.digest,one.digest);
  emit([{...one,zoom:1},two]);assert.equal(inlineReset.textContent,"100%");assert.equal(inlinePDF.style.width,"100%");assert.equal(inlinePDF.style.height,"100%");
  assert.equal(inlinePane.querySelector("iframe"),inlinePDF,"parent zoom state rebuilt an inline PDF");assert.equal(inlinePDF.parentElement.scrollTop,219);assert.equal(inlinePDF.parentElement.scrollLeft,45);assert.equal(inner.document.activeElement,inlineReset);
  emit([two,{...one,zoom:1.25}]);assert.equal(inlinePane.style.order,"1");assert.equal(inlinePane.querySelector("iframe"),inlinePDF);assert.equal(inlineReset.textContent,"125%");
  inlinePane.querySelectorAll(".file-compare-pane-actions button")[2].dispatchEvent({type:"click"});assert.equal(inner.messages.at(-1).value.delta,-.25);
  host.querySelector(".file-compare-inline-tabs").children[0].dispatchEvent({type:"click"});assert.equal(inlinePane.querySelector("iframe"),inlinePDF,"switching inline tabs recreated the PDF");
  emit([{...one,zoom:3}],{account:accountB});assert.equal(inlineReset.textContent,"125%","foreign account state changed inline zoom");assert.equal(inner.requests.length,0);
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

(async()=>{await clocksWhilePending();dockPlacement();progressClockCleanup();comparisonZoomReset();await confirmationSafety();process.stdout.write("Utility control clocks, dock placement, confirmations and selection isolation checks passed\nModal footer and persistent PDF/image zoom reset checks passed\n");})().catch(error=>{console.error(error);process.exitCode=1;});
