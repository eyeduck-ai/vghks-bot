"use strict";
// Query only connected nodes: a detached toolbar must disappear from lookups.
const assert=require("node:assert/strict"),{surface,tick}=require("./review_ui.js");
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
  ClinicalUI:{mrnBadge:()=>s.node("span"),ageBadge:()=>s.node("span"),patientAge:()=>"未提供",pdfURL:url=>url+"#toolbar=1",assetTools:()=>s.node("a"),pacsControl:options=>{assert.equal($("#analysisPatient").value,options.scope.mrn);return s.node("div");}},
  api:async(path,request)=>{reads.push({path,request});return {data:{order:{id:request.reference,name:"DBR"},assets},updated_at:"saved"};}
 });
 c.parent=c.window;s.run("vghks_bot/static/cataract.js");
 const data=mrn=>({member:{mrn},numeric:[],orders:[{id:mrn+"-order",name:"DBR",date:"2026-09-01",attachments:assets.map((asset,index)=>({...asset,name:"檔案 "+(index+1)}))}]});
 const controls=()=>{assert.equal($("#analysisView")?.value,"cataract");assert.ok(toolbar.isConnected);assert.equal($("#analysisCoverage"),coverage);};
 return {c,$,data,reads,moves,sources,toolbar,coverage,controls,messages,ui:c.window.CataractUI};
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
 console.log("Cataract report switching, connected controls, image fit/zoom state, native PDF controls and render-error recovery passed");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
