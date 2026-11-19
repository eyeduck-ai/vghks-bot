"use strict";
// Query only connected nodes: a detached toolbar must disappear from lookups.
const assert=require("node:assert/strict"),{surface,deferred,tick}=require("./review_ui.js");
const categories=[["biometry","生物測量（Biometry）"],["iol","水晶體度數結果"],["all","全部"]];
function setup({atomic=false,assets=[]}={}){
 const s=surface(),c=s.context,body=s.node("body"),prototype=Object.getPrototypeOf(body),messages=new Map(),reads=[],moves=[],sources=[];
 Object.defineProperties(prototype,{
  isConnected:{get(){return this===body||!!this.parentElement?.isConnected;}},
  previousSibling:{get(){return this.parentElement?.children[this.parentElement.children.indexOf(this)-1]||null;}}
 });
 prototype.append=function(...children){for(const child of children){child.remove();child.parentElement=this;this.children.push(child);}};
 prototype.prepend=function(...children){for(const child of [...children].reverse()){child.remove();child.parentElement=this;this.children.unshift(child);}};
 prototype.after=function(...children){if(!this.parentElement)return;const parent=this.parentElement;let index=parent.children.indexOf(this)+1;for(const child of children){child.remove();child.parentElement=parent;parent.children.splice(index++,0,child);}};
 prototype.replaceWith=function(child){this.before(child);this.remove();};
 if(atomic)prototype.moveBefore=function(child,anchor){assert.ok(this.isConnected&&child.isConnected,"atomic header move detached its controls");moves.push(child);this.insertBefore(child,anchor);};
 const $=selector=>selector.split(/\s+/).reduce((parents,part)=>parents.flatMap(parent=>parent.querySelectorAll(part)),[body])[0]||null;
 const add=(parent,tag,id,cls)=>{const node=s.node(tag,undefined,cls);if(id)node.id=id;parent.append(node);return node;};
 const pane=add(body,"div","analysisPane"),layout=add(pane,"div","cataractLayout"),main=add(layout,"div");
 add(layout,"aside","cataractPatients");const toolbar=add(main,"div",null,"analysis-view-toolbar");
 add(toolbar,"div","analysisSelectedPatient");
 for(const id of ["analysisPatient","analysisView"]){const label=add(toolbar,"label");add(label,"select",id);}
 for(const id of ["cataractUpdate","reloadAnalysis"])add(toolbar,"button",id);
 const coverage=add(main,"div","analysisCoverage");add(main,"div","analysisResults");
 $("#analysisPatient").value="TEST001";$("#analysisView").value="cataract";
 const element=(tag,text,cls)=>{
  const node=s.node(tag,text,cls);
  if(cls==="cataract-image-viewport")Object.assign(node,{clientWidth:400,clientHeight:600});
  if(tag==="img"){Object.assign(node,{naturalWidth:0,naturalHeight:0});let source="";Object.defineProperty(node,"src",{get:()=>source,set:value=>{source=value;sources.push(value);}});}
  return node;
 };
 Object.assign(c,{$,$$:(selector,parent=body)=>parent.querySelectorAll(selector),el:element,
  button:(text,run,cls)=>{const node=s.node("button",text,cls);node.run=run;return node;},
  embeddedAccount:"b".repeat(32),currentCohort:{id:"a".repeat(32),members:["TEST001","TEST002"].map(mrn=>({mrn,account_id:"b".repeat(32)}))},
  analysisState:{modules:{cataract:{orders:["DBR"]}}},config:{read_only:false},sharedStatus:{online:true},
  performance,location:{origin:"http://synthetic.invalid"},action:run=>run(),notify(){},scopedPath:path=>path,
  document:{activeElement:null,createComment:()=>s.node("comment"),addEventListener(){}},
  window:{addEventListener:(name,run)=>messages.set(name,[...(messages.get(name)||[]),run]),
   LocalSession:{resourceURL:path=>path},FileCompare:{count:()=>0,has:()=>false},CataractNumeric:{exams:[],create:()=>s.node("section")}},
  ClinicalUI:{mrnBadge:()=>s.node("span"),ageBadge:()=>s.node("span"),patientAge:()=>"未提供",pdfURL:(url,page)=>url.split("#")[0]+"#toolbar=1"+(page?"&page="+page:""),assetTools:()=>s.node("a"),pacsControl:options=>{assert.equal($("#analysisPatient").value,options.scope.mrn);return s.node("div");}},
  api:async(path,request)=>{reads.push({path,request});return {data:{order:{id:request.reference,name:"DBR"},assets},updated_at:"saved"};}
 });
 c.parent=c.window;s.run("vghks_bot/static/cataract.js");
 const data=mrn=>({member:{mrn},numeric:[],orders:[{id:mrn+"-order",name:"DBR",date:"2026-09-01",attachments:assets.map((asset,index)=>({...asset,name:"檔案 "+(index+1)}))}]});
 const controls=()=>{assert.equal($("#analysisView")?.value,"cataract");assert.ok(toolbar.isConnected);assert.equal($("#analysisCoverage"),coverage);};
 return {s,c,$,data,reads,moves,sources,toolbar,coverage,controls,messages,ui:c.window.CataractUI};
}
async function imageZoomTests(){
 const assets=[{digest:"1".repeat(64),mime:"image/png"},{digest:"2".repeat(64),mime:"application/pdf"}];
 const s=setup({assets}),{ui,$,data}=s;
 ui.render(data("TEST001"));await $(".cataract-order-row").run();
 const box=$(".cataract-image-preview"),image=box.querySelector("img"),viewport=box.querySelector(".cataract-image-viewport"),zoom=box.querySelector(".cataract-image-zoom"),reset=box.querySelector(".cataract-image-fit");
 const less=zoom.children[0],more=zoom.children[2];
 assert.equal(more.disabled,true,"zoom enabled before image dimensions were known");
 Object.assign(image,{naturalWidth:1000,naturalHeight:2000});await image.emit("load");
 assert.equal(image.style.width,"300px");assert.equal(reset.textContent,"30%","portrait did not initially fit both dimensions");assert.equal(more.disabled,false);
 more.run();assert.equal(image.style.width,"500px");assert.equal(reset.textContent,"50%");
 viewport.scrollTop=171;viewport.scrollLeft=45;await viewport.emit("scroll");
 const updated=data("TEST001");updated.orders[0].report_loaded=true;updated.orders[0].saved_at="new";ui.render(updated);await tick();
 assert.equal($(".cataract-image-preview"),box);assert.equal(box.querySelector("img"),image);assert.equal(box.querySelector(".cataract-image-fit"),reset);
 assert.equal(viewport.scrollTop,171);assert.equal(viewport.scrollLeft,45);assert.equal(image.style.width,"500px");assert.equal(s.sources.length,1,"background update reloaded the image");
 viewport.clientWidth=500;viewport.clientHeight=1000;for(const run of s.messages.get("resize"))run();
 assert.equal(reset.textContent,"50%","resizing discarded manual image zoom");
 less.run();assert.equal(image.style.width,"250px");reset.run();assert.equal(image.style.width,"500px");assert.equal(viewport.scrollTop,0);assert.equal(viewport.scrollLeft,0);
 viewport.clientWidth=300;for(const run of s.messages.get("resize"))run();assert.equal(reset.textContent,"30%","fit mode did not follow the container size");
 more.run();assert.equal(reset.textContent,"50%");viewport.scrollTop=111;await viewport.emit("scroll");
 const readCount=s.reads.length;
 const choose=digest=>$(".cataract-file-choices").children.find(node=>node.dataset.digest===digest).querySelector("button").run();
 await choose(assets[1].digest);assert.equal($(".cataract-image-zoom"),null,"PDF displayed image zoom controls");assert.match($(".cataract-asset").querySelector("iframe").src,/#toolbar=1$/);
 await choose(assets[0].digest);const restored=$(".cataract-image-preview").querySelector("img");Object.assign(restored,{naturalWidth:1000,naturalHeight:2000});await restored.emit("load");
 assert.equal($(".cataract-image-fit").textContent,"50%");assert.equal($(".cataract-image-viewport").scrollTop,111);assert.equal(s.reads.length,readCount,"attachment switching fetched the report again");
 $("#analysisPatient").value="TEST002";ui.render(data("TEST002"));await $(".cataract-order-row").run();
 const other=$(".cataract-image-preview").querySelector("img");Object.assign(other,{naturalWidth:1000,naturalHeight:2000});await other.emit("load");assert.equal($(".cataract-image-fit").textContent,"30%","another patient inherited image zoom");
 $("#analysisPatient").value="TEST001";ui.render(data("TEST001"));const previous=$(".cataract-image-preview").querySelector("img");Object.assign(previous,{naturalWidth:1000,naturalHeight:2000});await previous.emit("load");assert.equal($(".cataract-image-fit").textContent,"50%");
 assert.equal(s.sources.length,4,"zoom and reset navigated the existing image");
 const controls=$(".cataract-image-zoom"),fitWidth=controls.children[3],fitHeight=controls.children[4],currentViewport=$(".cataract-image-viewport");
 Object.assign(currentViewport,{clientWidth:500,clientHeight:600});fitWidth.run();assert.equal(previous.style.width,"500px");
 assert.equal(fitWidth["aria-pressed"],"true");currentViewport.clientWidth=700;for(const run of s.messages.get("resize"))run();assert.equal(previous.style.width,"700px");
 fitHeight.run();assert.equal(previous.style.width,"300px");currentViewport.clientHeight=800;for(const run of s.messages.get("resize"))run();assert.equal(previous.style.width,"400px");
 controls.children[2].run();assert.equal(previous.style.width,"500px");assert.equal(fitHeight["aria-pressed"],"false");
 currentViewport.clientHeight=400;for(const run of s.messages.get("resize"))run();assert.equal(previous.style.width,"500px","fitting controls discarded subsequent manual zoom");
 assert.equal(s.sources.length,4,"fitting reloaded the cataract image");
}
async function scanTabTests(){
 const {s,c,$,ui,data,messages}=setup(),pending=deferred(),calls=[],comparison=new Map(),documents=[],selected=[],sent=[];
 const file={digest:"d".repeat(64),mime:"application/pdf"};
 c.document.createElement=tag=>s.node(tag);
 c.window.JobProgress={publish(){},ready:async()=>{},wait:async()=>{}};
 c.window.CataractBiometry={categories,create:({mrn,source})=>({
  element:s.node("div"),setDocuments:(items,options)=>documents.push({mrn,source,items:[...items],options}),
  readPages:async doc=>({count:2,digest:doc.digest}),
  subscribeDocument(doc,listener){listener({});return ()=>{};},
  startDocument:async (doc,pages,category)=>selected.push({id:doc.id,digest:doc.digest,pages:[...pages],category}),clear(){}
 })};
 c.window.FileCompare={count:()=>comparison.size,has:digest=>comparison.has(digest),
  add:item=>sent.push(item),remove:digest=>comparison.delete(digest)};
 let revised=false;
 c.api=async(path,values)=>{
  calls.push({path,values});
  if(path==="/api/tasks/start"){revised=true;return {task_id:"REFRESH"};}
  if(path.startsWith("/api/tasks/detail"))return {status:"completed"};
  if(values.resource==="scans")return {complete:true,data:{records:[
   {id:"eye",category_label:"眼科紀錄",date:"2025-02-01",eye:true,asset:file},
   {id:"uncached",category_label:"眼科紀錄",date:"2024-02-01",eye:true},
   {id:"other",category_label:"其他病歷",date:"2023-02-01",eye:false,asset:file},
   {id:"not-pdf",category_label:"其他病歷",date:"2022-02-01",eye:false,asset:{digest:"f".repeat(64),mime:"image/png"}}
  ].filter(row=>!revised||row.id!=="eye")}};
  if(values.resource==="scan_asset"&&values.reference==="uncached")return pending.promise;
  throw new Error(`unexpected scan request ${path}`);
 };
 s.run("vghks_bot/static/scan-browser.js");
 s.run("vghks_bot/static/cataract-scans.js");
 ui.render(data("TEST001"));
 assert.equal(calls.length,0,"scan index should load on entering its tab");
 const tabs=$(".cataract-view-nav").querySelectorAll("button").filter(control=>control.dataset.view);
 assert.deepEqual(tabs.map(control=>control.textContent),["SOAP+數值","數值+醫囑報告","SOAP+掃描病歷"]);
 const tab=tabs.find(control=>control.dataset.view==="scans");
 tab.run();await tick();
 assert.equal(calls[0].values.mrn,"TEST001");
 assert.equal(calls[0].values.cohort_id,c.currentCohort.id);
 assert.equal(documents[0].source,"scans");
 assert.equal(documents[0].options.allowAll,true);
 assert.deepEqual(documents[0].items.map(item=>item.id),["eye","uncached","other"],"PDF extraction scope must include the full index across filters");
 const page=$(".cataract-scans-view");
 assert.ok(page.querySelector(".cataract-soap"));
 assert.equal(page.querySelectorAll(".cataract-scans-row").length,2);
 const [eye]=page.querySelectorAll("[data-scan-reference]");
 eye.run();await tick();
 const viewer=page.querySelector("iframe"),head=page.querySelector(".cataract-scans-preview-head");
 assert.match(viewer.src,new RegExp(file.digest));
 assert.equal(head.querySelector("a"),null,"scan PDF preview should use its native download control");
 assert.equal(head.querySelector("[data-scan-compare]"),null,"scan PDF comparison belongs in the catalog");
 const [whole,first,second]=head.querySelectorAll("input");
 assert.deepEqual([whole,first,second].map(input=>input["aria-label"]),["整份 PDF","第 1 頁","第 2 頁"]);
 second.checked=true;await second.emit("change");
 assert.equal(whole.checked,false);
 await head.querySelector("button").run();
 assert.deepEqual(selected,[{id:"eye",digest:file.digest,pages:[2],category:"all"}]);
 eye.run();await tick();
 assert.equal(page.querySelector("iframe"),viewer,"inline choices should preserve scan PDF previews");
 const [check]=page.querySelectorAll("[data-scan-compare]");
 const changed=()=>{for(const handler of messages.get("filecomparechange"))handler();};
 check.checked=true;const checking=check.emit("change");
 assert.equal(sent[0]?.mrn,"TEST001","a cached comparison should reach the parent synchronously");
 assert.equal(sent[0]?.account,c.embeddedAccount);
 changed();
 assert.ok(check.checked&&check.disabled,"keep comparison intent visible until the parent acknowledges it");
 comparison.set(file.digest,sent[0]);changed();await checking;
 assert.ok(check.checked&&!check.disabled);
 comparison.clear();changed();
 assert.equal(check.checked,false,"scan checkbox must reflect comparison drawer changes");
 check.checked=true;await check.emit("change");
 for(const handler of messages.get("filecomparemessage"))handler({detail:"比較清單已滿"});
 assert.ok(!check.checked&&!check.disabled,"parent rejection should restore the comparison checkbox");
 const update=page.querySelector(".cataract-scans-controls").querySelector("button");
 assert.equal(update.textContent,"↻");assert.equal(update["aria-label"],"更新歷年掃描索引");
 update.run();await tick();
 assert.equal(page.querySelector("iframe"),null,"refresh should clear a preview whose reference no longer exists");
 const previousCalls=calls.length;
 eye.run();await tick();
 assert.equal(calls.length,previousCalls,"a removed reference should not initiate another PDF request");
 page.querySelector("[data-scan-reference]").run();await tick();
 assert.equal(calls.at(-1).values.reference,"uncached");
 const waitingCheck=page.querySelector("[data-scan-compare]");
 waitingCheck.checked=true;
 const waitingComparison=waitingCheck.emit("change");
 assert.ok(waitingCheck.checked&&waitingCheck.disabled);
 assert.equal(calls.filter(call=>call.values?.resource==="scan_asset").length,1,"preview and comparison must share a PDF download");
 $("#analysisPatient").value="TEST002";ui.render(data("TEST002"));
 pending.resolve({data:file});await waitingComparison;await tick();
 assert.equal(page.querySelector("iframe"),null,"an old patient PDF completed after switching patients");
 assert.equal($(".cataract-scans-view").querySelector("iframe"),null);
 assert.equal(sent.length,2,"an old patient PDF was added to the comparison after switching patients");
}
async function orderInlineBiometryTests(){
 const assets=[{digest:"1".repeat(64),mime:"application/pdf"},{digest:"2".repeat(64),mime:"application/pdf"}];
 const {s,c,$,ui,data}=setup({assets}),pending=deferred(),reads=[],starts=[],listeners=new Set(),draft=new Map(),reviews=[];
 const emit=value=>{for(const listen of listeners)listen(value);};
 c.window.CataractBiometry={categories,create:()=>({
  element:s.node("div"), setDocuments(){}, clear(){}, dismiss(){},
  readPages(doc,stillCurrent){
   reads.push({doc,stillCurrent});
   return doc.digest===assets[0].digest ? pending.promise : Promise.resolve({count:2,digest:doc.digest});
  },
  subscribeDocument(doc,listener){listeners.add(listener);listener({});return ()=>listeners.delete(listener);},
  reviewOptions:()=>({draft}),
  async startDocument(doc,pages,category){
   starts.push({id:doc.id,digest:doc.digest,pages:pages&&[...pages],category});
   emit({busy:true,message:"擷取中"});
  }
 })};
 c.window.CataractBiometryReview={create:options=>{
  reviews.push(options);return {element:s.node("div","review"),header:s.node("div")};
 }};
 const record=data("TEST001");record.member.account_id=c.embeddedAccount;
 ui.render(record);await $(".cataract-order-row").run();
 const original=$(".cataract-pdf-extraction");
 assert.equal(original.querySelector("button").disabled,true);
 const choose=digest=>$(".cataract-file-choices").children.find(node=>node.dataset.digest===digest).querySelector("button").run();
 await choose(assets[1].digest);await tick();
 assert.ok(!reads[0].stillCurrent());assert.equal(listeners.size,1);
 const toolbar=$(".cataract-pdf-extraction"),viewer=$(".cataract-asset").querySelector("iframe");
 const [whole,first,second]=toolbar.querySelectorAll("input");
 pending.resolve({count:10,digest:assets[0].digest});await tick();
 assert.equal(toolbar.querySelectorAll("input").length,3,"page counts from the prior PDF must not change the current controls");
 const start=toolbar.querySelector("button");
 assert.equal(start.className,"quiet attachment-control");
 second.checked=true;await second.emit("change");
 assert.equal(whole.checked,false);
 await choose(assets[1].digest);
 assert.equal($(".cataract-pdf-extraction"),toolbar,"reselecting an unchanged attachment discarded its selections");
 toolbar.querySelector("select").value="iol";
 await start.run();
 assert.deepEqual(starts,[{id:"TEST001-order",digest:assets[1].digest,pages:[2],category:"iol"}]);
 assert.ok(start.disabled);
 assert.match(toolbar.querySelector("p").textContent,/擷取中/);
 const outcome={documents:[{id:"TEST001-order",digest:assets[1].digest}]};
 emit({saved:true,jobId:"other",result:{documents:[{id:"TEST001-order",digest:assets[0].digest}]}});
 assert.ok($(".cataract-inline-review").hidden,"a different PDF must not reuse the saved review");
 emit({saved:true,jobId:"saved",hasResult:true,result:outcome});
 assert.equal($(".cataract-inline-review").hidden,false);
 assert.equal(reviews.length,1);
 assert.equal(reviews[0].draft,draft);
 assert.match(toolbar.querySelector("p").textContent,/已載入本機擷取與核對結果/);
 reviews[0].onPageChange(2);
 assert.match(viewer.src,/#toolbar=1&page=2$/);
 assert.equal($(".cataract-asset").querySelector("iframe"),viewer);
 emit({changed:true});
 assert.equal($(".cataract-inline-review").hidden,true);
 assert.match(toolbar.querySelector("p").textContent,/原始 PDF 已變更/);
 whole.checked=true;await whole.emit("change");
 assert.ok(!first.checked&&!second.checked);
 await start.run();
 assert.equal(starts[1].pages,undefined,"whole-file extraction should not send a subset");
}
async function basicVariantTests(){
 const {s,c,$,ui,data}=setup();
 c.config.biometry_variant="basic";
 c.document.createElement=tag=>s.node(tag);
 c.window.CataractBiometry={create:()=>{throw new Error("basic edition mounted extraction controls");}};
 const file={digest:"a".repeat(64),mime:"application/pdf"},compared=[];
 c.window.FileCompare={count:()=>compared.length,has:digest=>compared.some(item=>item.digest===digest),
  add:item=>compared.push(item)};
 c.api=async(path,values)=>{
  assert.equal(path,"/api/reviews/history/read");
  assert.equal(values.resource,"scans");
  return {complete:true,data:{records:[{id:"scan",category_label:"眼科紀錄",eye:true,asset:file}]}};
 };
 s.run("vghks_bot/static/cataract-scans.js");
 const record=data("TEST001");record.member.account_id=c.embeddedAccount;
 ui.render(record);
 $(".cataract-view-nav").querySelectorAll("button").find(control=>control.dataset.view==="scans").run();
 await tick();
 $(".cataract-scans-row").querySelector("button").run();await tick();
 assert.match($(".cataract-scans-preview").querySelector("iframe").src,new RegExp(file.digest));
 const check=$(".cataract-scans-row").querySelector("input");check.checked=true;await check.emit("change");
 assert.equal(compared[0].digest,file.digest);
}
async function main(){
 for(const atomic of [false,true]){
  const s=setup({atomic}),{c,$,ui,data,controls}=s;
  ui.render(data("TEST001"));controls();
  await $(".cataract-order-row").run();
  const requestCount=s.reads.length;
  $("#analysisPatient").value="TEST002";ui.render(data("TEST002"));controls();
  $("#analysisPatient").value="TEST001";
  assert.doesNotThrow(()=>ui.render(data("TEST001")),"returning to a selected report detached the patient/view controls");controls();
  assert.equal(s.reads.length,requestCount,"restoring a selected report fetched it again");
  assert.equal($(".cataract-view-nav").querySelectorAll("button").find(node=>node.dataset.view==="work")["aria-pressed"],"true");
  if(atomic)assert.ok(s.moves.includes(s.toolbar)&&s.moves.includes(s.coverage));
  else assert.equal(s.moves.length,0);
  // A synchronous preview/render error must leave navigation available.
  $("#analysisPatient").value="TEST002";
  const create=c.window.CataractNumeric.create;
  c.window.CataractNumeric.create=()=>{throw new Error("synthetic numeric render failure");};
  assert.throws(()=>ui.render(data("TEST002")),/synthetic numeric render failure/);controls();
  c.window.CataractNumeric.create=create;assert.doesNotThrow(()=>ui.render(data("TEST002")));controls();
  $("#analysisPatient").value="TEST001";ui.render(data("TEST001"));controls();
  for(const receive of s.messages.get("filecomparechange"))assert.doesNotThrow(()=>receive());
  $("#analysisView").value="retina";ui.layout();
  assert.equal(s.toolbar.parentElement,$("#analysisCoverage").parentElement);
  assert.equal($("#analysisView").value,"retina");
 }
 await imageZoomTests();
 await scanTabTests();
 await orderInlineBiometryTests();
 await basicVariantTests();
 console.log("Cataract report and scan switching, shared comparison, inline PDF extraction, image fit/zoom and render-error recovery passed");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
