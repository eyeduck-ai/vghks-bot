"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs");
const {surface,deferred,tick}=require("./review_ui.js");
function setup({width=960,height=400,imageWidth=2400,imageHeight=1600}={}){
 const s=surface(),c=s.context,observers=[],sizes=[],sources=[],domSources=[],revoked=[],draws=[],comparisons=[],thumbnailFailures=new Map();let blob=0,decodeGate=null;
 c.document.createElement=tag=>{
  const value=s.node(tag);value.tagName=tag.toUpperCase();
  if(tag==="div")Object.assign(value,{clientWidth:width,clientHeight:height});
  if(tag==="img"){
   Object.assign(value,{naturalWidth:imageWidth,naturalHeight:imageHeight});
   let source="";Object.defineProperty(value,"src",{get:()=>source,set:url=>{source=url;if(url.startsWith("/local/verified/"))domSources.push(url);}});
   value.removeEventListener=(name,callback)=>value.events.set(name,(value.events.get(name)||[]).filter(run=>run!==callback));
  }
  if(tag==="canvas"){value.getContext=()=>({drawImage:(image,...bounds)=>{value.failure=thumbnailFailures.get(image.src);if(value.failure==="draw")throw new Error("synthetic canvas failure");draws.push(bounds);}});value.toBlob=callback=>callback(value.failure==="blob"?null:{type:"image/jpeg"});}
  return value;
 };
 c.IntersectionObserver=class{constructor(callback){this.callback=callback;this.targets=new Set();observers.push(this);}observe(value){this.targets.add(value);}unobserve(value){this.targets.delete(value);}disconnect(){this.disconnected=true;this.targets.clear();}};
 c.ResizeObserver=class{constructor(callback){this.callback=callback;sizes.push(this);}observe(target){this.target=target;}disconnect(){this.disconnected=true;}};
 c.Image=class{
  constructor(){
   const image=s.node("IMG");image.naturalWidth=2400;image.naturalHeight=1600;let source="";
   Object.defineProperty(image,"src",{get:()=>source,set:url=>{
    source=url;if(!url)return;sources.push(url);
    Promise.resolve(decodeGate?.promise).then(()=>{
     if(source!==url)return;
     if(thumbnailFailures.get(url)==="missing")image.onerror?.();else image.onload?.();
    });
   }});
   const remove=image.removeAttribute.bind(image);image.removeAttribute=name=>{if(name==="src")source="";else remove(name);};
   return image;
  }
 };
 c.URL={createObjectURL:()=>"blob:local-"+(++blob),revokeObjectURL:url=>revoked.push(url)};
 c.ClinicalUI={pdfURL:url=>url+"#toolbar=1",comparisonChoice:item=>{const checkbox=s.node("input");checkbox.checked=true;comparisons.push(item);return checkbox;},assetTools:options=>{const value=s.node("a","下載原檔");value.href=options.url;return value;}};
 s.run("vghks_bot/static/report-gallery.js");
 const gallery=c.window.ReportGallery.create({urlFor:asset=>"/local/verified/"+asset.digest,identity:{account:"SYNTHETIC",mrn:"TEST001"}});
 return {...s,gallery,observers,sizes,sources,domSources,revoked,draws,comparisons,setGate:value=>{decodeGate=value;},
  failThumbnail:(digest,mode)=>thumbnailFailures.set("/local/verified/"+digest,mode)};
}
const asset=(key,digest,mime="image/jpeg")=>({ref_key:key,digest,mime,size:32});
function payload(items,assets=[]){return {order:{id:"ORDER",date:"2026-10-27",name:"合成檢查"},attachment_items:items,assets};}
async function galleryTests(){
 const s=setup(),g=s.gallery,root=g.element;
 const pdf=asset("pdf","PDF","application/pdf"),image=asset("image","IMAGE"),last=asset("last","LAST");
 const items=[{ref_key:"pdf",ordinal:1,status:"ready",kind:"pdf"},{ref_key:"image",ordinal:2,status:"pending",kind:"image"}];
 g.update(payload(items,[pdf]));
 const viewport=root.querySelector(".report-gallery-viewport"),stage=viewport.firstElementChild,iframe=stage.firstElementChild,tiles=()=>root.querySelectorAll(".report-gallery-tile");
 assert.equal(iframe.tagName,"IFRAME");assert.equal(iframe.src,"/local/verified/PDF#toolbar=1");viewport.scrollTop=123;
 const pdfReset=root.querySelector(".report-gallery-zoom");assert.equal(pdfReset.tagName,"BUTTON");assert.match(pdfReset["aria-label"],/100%/);
 await root.all().find(value=>value.textContent==="＋").emit("click");assert.equal(pdfReset.textContent,"125%");assert.equal(iframe.style.transform,"scale(1.25)");assert.equal(stage.style.width,"125%");
 assert.equal(iframe.src,"/local/verified/PDF#toolbar=1","PDF zoom navigated/reloaded the original document");
 assert.equal(tiles()[0].querySelector(".report-gallery-status").hidden,true);assert.equal(tiles()[0].querySelector(".report-gallery-status").textContent,"");assert.equal(tiles()[0].querySelector(".report-gallery-caption").textContent,"1");
 assert.equal(tiles()[1].querySelector(".report-gallery-status").hidden,false);assert.equal(tiles()[1].querySelector(".report-gallery-status").textContent,"等待下載");
 const checkbox=root.querySelector("input");
 items[1]={...items[1],status:"ready"};g.update(payload(items,[pdf,image]));
 assert.equal(viewport.firstElementChild,stage,"appended ready image recreated the PDF stage");assert.equal(stage.firstElementChild,iframe,"appended ready image recreated the PDF frame");assert.equal(viewport.scrollTop,123);
 assert.equal(root.querySelector(".report-gallery-zoom"),pdfReset);assert.equal(pdfReset.textContent,"125%","progress reset PDF zoom");await pdfReset.emit("click");assert.equal(iframe.style.transform,"scale(1)");assert.equal(pdfReset.textContent,"100%");assert.equal(iframe.src,"/local/verified/PDF#toolbar=1");
 assert.equal(root.querySelector("input"),checkbox,"progress replaced comparison selection control");assert.equal(checkbox.checked,true);
 assert.equal(s.sources.length,0,"offscreen thumbnail eagerly decoded an original image");
 await tiles()[1].emit("click");const mainImage=viewport.firstElementChild;
 assert.equal(iframe.src,"about:blank","old PDF retained a live document after switching");assert.equal(mainImage.src,"/local/verified/IMAGE");
 const plus=root.all().find(value=>value.textContent==="＋");await plus.emit("click");viewport.scrollTop=44;
 items.push({ref_key:"last",ordinal:3,status:"ready",kind:"image"});g.update(payload(items,[pdf,image,last]));
 assert.equal(viewport.firstElementChild,mainImage);assert.equal(mainImage.style.width,"1200px");assert.equal(viewport.scrollTop,44);
 const imageReset=root.querySelector(".report-gallery-zoom");assert.equal(imageReset.tagName,"BUTTON");await imageReset.emit("click");assert.equal(mainImage.style.width,"600px");assert.equal(imageReset.textContent,"25%");assert.equal(viewport.firstElementChild,mainImage,"reset replaced the original image");
 assert.equal(tiles()[1]["aria-pressed"],"true","new attachment stole selection");
 s.observers[0].callback([{target:tiles()[1],isIntersecting:true}]);await tick();
 assert.deepEqual(s.sources,["/local/verified/IMAGE"]);assert.deepEqual(s.draws[0],[0,0,240,160]);
 assert.match(tiles()[1].querySelector(".report-gallery-thumbnail").firstElementChild.src,/^blob:local-/);
 items[1]={...items[1],status:"failed",message:"合成重試失敗"};g.update(payload(items,[pdf,image,last]));
 assert.equal(viewport.firstElementChild,mainImage,"failed refresh discarded the saved original");
 assert.equal(tiles()[1].querySelector(".report-gallery-status").hidden,false);assert.equal(tiles()[1].querySelector(".report-gallery-status").textContent,"未取得");
 assert.match(root.querySelector(".report-gallery-notice").textContent,/已存原檔仍可檢閱/);
 items[1]={...items[1],status:"downloading"};g.update(payload(items,[pdf,image,last]));assert.equal(viewport.firstElementChild,mainImage);
 const gate=deferred();s.setGate(gate);s.observers[0].callback([{target:tiles()[2],isIntersecting:true}]);await tick();
 g.dispose();assert.equal(s.observers[0].disconnected,true);assert.deepEqual(s.revoked,["blob:local-1"]);
 gate.resolve();await tick();assert.equal(s.revoked.length,1,"disposed thumbnail created a late object URL");
 g.dispose();assert.equal(s.revoked.length,1,"resource cleanup was not idempotent");
 const legacy=setup();legacy.gallery.update({order:{},assets:[image,pdf]});assert.equal(legacy.gallery.element.querySelectorAll(".report-gallery-tile").length,2);legacy.gallery.dispose();
 const arriving=setup(),waiting=[{ref_key:"first",ordinal:1,status:"pending",kind:"image"},{ref_key:"later",ordinal:2,status:"pending",kind:"image"}];
 arriving.gallery.update(payload(waiting));const arrivingViewport=arriving.gallery.element.querySelector(".report-gallery-viewport");
 arriving.gallery.update(payload([{...waiting[0],status:"failed"},{...waiting[1],status:"ready"}],[asset("later","FIRST_READY")]));
 assert.equal(arrivingViewport.firstElementChild.src,"/local/verified/FIRST_READY","an unavailable first reference hid the first available attachment");
 const arrivingTiles=arriving.gallery.element.querySelectorAll(".report-gallery-tile");await arrivingTiles[0].emit("click");
 arriving.gallery.update(payload([{...waiting[0],status:"pending"},{...waiting[1],status:"ready"}],[asset("later","FIRST_READY")]));
 assert.equal(arrivingTiles[0]["aria-pressed"],"true","background completion stole an explicitly selected pending reference");
 arriving.gallery.update(payload(waiting.map(item=>({...item,status:"ready"})),[asset("first","SELECTED_READY"),asset("later","FIRST_READY")]));
 assert.equal(arrivingViewport.firstElementChild.src,"/local/verified/SELECTED_READY");arriving.gallery.dispose();
 const css=fs.readFileSync("vghks_bot/static/report-gallery.css","utf8");
 assert.match(css,/grid-template-columns:196px minmax\(0,1fr\)/);assert.match(css,/grid-template-columns:repeat\(2,minmax\(0,1fr\)\)/);
 const mobile=css.slice(css.indexOf("@media(max-width:600px)"));
 assert.match(mobile,/grid-template-columns:minmax\(0,1fr\)/);assert.match(mobile,/flex-direction:row/);assert.match(mobile,/overflow-x:auto;overflow-y:hidden/);assert.match(mobile,/flex:0 0 92px;width:92px/);
 assert.match(css,/#dataDialog\.report-view-dialog\[data-view="history"\]\[open\]\{display:flex/);assert.match(css,/\.report-gallery-viewport\{[^}]*grid-row:3/);
}
async function imageFitTests(){
 const s=setup({width:982,height:499,imageWidth:0,imageHeight:0}),g=s.gallery,root=g.element;
 const image=asset("portrait","PORTRAIT"),small=asset("small","SMALL"),ready=[{...image,status:"ready"},{...small,status:"ready"}];
 g.update(payload(ready,[image,small]));
 const viewport=root.querySelector(".report-gallery-viewport"),tiles=root.querySelectorAll(".report-gallery-tile"),sizeObserver=s.sizes[0];
 const percentage=()=>root.querySelector(".report-gallery-zoom"),step=sign=>root.all().find(node=>node.textContent===sign);
 const resize=(width,height)=>{Object.assign(viewport,{clientWidth:width,clientHeight:height});sizeObserver.callback();};
 const load=async (width,height)=>{const file=viewport.firstElementChild;Object.assign(file,{naturalWidth:width,naturalHeight:height});await file.emit("load");return file;};
 assert.equal(sizeObserver.target,viewport);
 assert.equal(percentage().textContent,"…");assert.equal(step("＋").disabled,true);
 const original=await load(740,1900),initialWidth=parseFloat(original.style.width);
 assert.equal(percentage().textContent,"26%");assert.equal(step("＋").disabled,false);
 assert.ok(initialWidth<=982&&initialWidth*1900/740<=499,"fit cropped the portrait image vertically");
 assert.ok(initialWidth*1900/740>498,"fit did not use the available height");
 for(const expected of ["50%","75%","100%"]){await step("＋").emit("click");assert.equal(percentage().textContent,expected);}
 assert.equal(original.style.width,"740px","100% is not the original image width");
 viewport.scrollTop=111;resize(982,760);assert.equal(original.style.width,"740px","viewport resize discarded manual zoom");
 assert.equal(viewport.scrollTop,111);
 assert.deepEqual(s.domSources,["/local/verified/PORTRAIT"],"zoom reloaded the image");
 await tiles[1].emit("click");const smallImage=await load(120,90);assert.equal(smallImage.style.width,"120px","fit enlarged a small original");
 await tiles[0].emit("click");const reopened=await load(740,1900);
 assert.equal(percentage().textContent,"100%");assert.equal(viewport.scrollTop,111,"returning to an attachment lost the manual scroll position");
 const reset=percentage();assert.match(reset["aria-label"],/回到最適比例/);await reset.emit("click");
 assert.equal(reopened.style.width,"296px");assert.equal(reset.textContent,"40%");assert.equal(viewport.scrollTop,0);
 resize(148,760);assert.equal(reopened.style.width,"148px");assert.equal(reset.textContent,"20%");
 assert.equal(s.context.document.activeElement,reset,"fitting replaced the focused reset button");
 await step("−").emit("click");assert.ok(parseFloat(reopened.style.width)<148,"zoom out from a fit below 25% enlarged the image");
 assert.equal(viewport.firstElementChild,reopened);
 g.dispose();assert.equal(sizeObserver.disconnected,true);
}
async function thumbnailTests(){
 const s=setup(),g=s.gallery,root=g.element;
 const items=Array.from({length:7},(_,index)=>({ref_key:"file-"+(index+1),ordinal:index+1,
  kind:"image",status:index<2?"ready":"pending"}));
 const files=items.map(item=>asset(item.ref_key,"IMG-"+item.ordinal));
 const path=index=>"/local/verified/IMG-"+index;
 s.failThumbnail("IMG-3","blob");s.failThumbnail("IMG-4","draw");s.failThumbnail("IMG-6","missing");
 g.update(payload(items,files.slice(0,2)));
 const tiles=root.querySelectorAll(".report-gallery-tile");
 const thumb=index=>tiles[index-1].querySelector(".report-gallery-thumbnail");
 const observer=s.observers[0];
 observer.callback(tiles.slice(0,6).map(target=>({target,isIntersecting:true})));
 await tick();
 assert.deepEqual(s.sources,[path(1),path(2)],"visible pending attachments requested files prematurely");
 assert.equal(thumb(3).textContent,"影像");

 const gate=deferred();s.setGate(gate);
 const ready=items.map(item=>({...item,status:"ready"}));
 g.update(payload(ready,files));
 g.update(payload(ready,files));
 assert.deepEqual(s.sources,[path(1),path(2),path(3),path(4)],"readiness changes did not honor the two-load limit");
 observer.callback([{target:tiles[5],isIntersecting:false}]);
 gate.resolve();await tick();await tick();
 assert.deepEqual(s.sources,[1,2,3,4,5].map(path),"queued offscreen attachment loaded eagerly or a visible one was skipped");
 assert.equal(thumb(3).firstElementChild.src,path(3),"null thumbnail blob did not fall back to the already loaded original");
 assert.equal(thumb(4).firstElementChild.src,path(4),"canvas exception did not fall back to the already loaded original");
 const brokenBlob=thumb(5).firstElementChild;
 assert.match(brokenBlob.src,/^blob:local-/);
 await brokenBlob.emit("error");
 assert.equal(thumb(5).firstElementChild.src,path(5),"an unavailable blob thumbnail hid the original");
 assert.ok(s.revoked.includes(brokenBlob.src));
 assert.deepEqual(s.sources,[1,2,3,4,5].map(path),"thumbnail fallback requested the original again");

 observer.callback([{target:tiles[5],isIntersecting:true},{target:tiles[6],isIntersecting:true}]);
 await tick();
 assert.deepEqual(s.sources,[1,2,3,4,5,6,7].map(path));
 assert.match(thumb(6)["aria-label"],/縮圖無法讀取/);
 assert.match(thumb(7).firstElementChild.src,/^blob:local-/);
 observer.callback([{target:tiles[5],isIntersecting:false},{target:tiles[5],isIntersecting:true}]);
 await tick();
 assert.equal(s.sources.length,7,"an unavailable local file triggered repeated requests");
 await tiles[3].emit("click");
 const viewport=root.querySelector(".report-gallery-viewport");
 assert.equal(viewport.firstElementChild.src,path(4),"fallback changed the selected original's identity");

 const oldPreview=thumb(7).firstElementChild;
 const refreshed=[...files.slice(0,6),asset("file-7","NEW-7")];
 g.update(payload(ready,refreshed));await tick();
 const newPreview=thumb(7).firstElementChild;
 assert.notEqual(newPreview,oldPreview);
 assert.equal(s.sources.at(-1),"/local/verified/NEW-7","a visible replacement digest did not receive a thumbnail");
 await oldPreview.emit("error");
 assert.equal(thumb(7).firstElementChild,newPreview,"a stale thumbnail fallback overwrote the refreshed image");
 assert.equal(viewport.firstElementChild.src,path(4));
 g.dispose();
}
function assertReportToolbar(header, panel){
 const navigation=header.querySelector(".history-controls"),refresh=navigation.querySelector(".history-report-refresh");
 assert.equal(navigation.classList.contains("dialog-toolbar"),true);
 assert.equal(refresh.classList.contains("dialog-header-control"),true);
 for(const selector of [".history-report-meta",".history-report-state"]){
  const label=navigation.querySelector(selector);
  assert.equal(label.title,label.textContent,"a compact header label lost its full text");
 }
 const galleryHeader=panel.querySelector(".report-gallery-header"),toolbar=galleryHeader.querySelector(".attachment-toolbar");
 assert.equal(galleryHeader.querySelector(".report-gallery-heading").firstElementChild.textContent,"附件 1 / 2");
 const files=toolbar.querySelector(".report-gallery-file-actions"),zoom=toolbar.querySelector(".report-gallery-zoom-group");
 assert.ok(files.querySelector("input").checked);
 const download=new URL(files.children.at(-1).href);
 assert.equal(download.pathname,"/api/accounts/"+"a".repeat(32)+"/analysis/asset");
 assert.equal(download.searchParams.get("id"),"PDF");
 assert.equal(download.searchParams.get("access"),"f".repeat(64));
 assert.equal(zoom["aria-label"],"附件縮放");
}
async function historyTests(){
 const s=setup(),c=s.context,$=s.$,calls=[];s.gallery.dispose();let revision=1,status="running",complete=false,startGate=null,pages=[],shown=[];
 c.account="a".repeat(32);c.location={origin:"http://127.0.0.1:1000",href:"http://127.0.0.1:1000/"};
 c.URL=Object.assign(class extends URL {},c.URL);
 s.run("vghks_bot/static/local-session.js");c.window.LocalSession.accept({session:"S".repeat(43),resource:"f".repeat(64)});
 const pdf=asset("PDFREF","PDF","application/pdf"),image=asset("IMAGEREF","IMG");
 const items=[{ref_key:"PDFREF",ordinal:1,status:"ready",kind:"pdf"},{ref_key:"IMAGEREF",ordinal:2,status:"pending",kind:"image"}];
 const details=[{fields:{檢查項目:"合成眼科醫囑"}}];let data={...payload(items,[pdf]),details};
 c.ClinicalUI.loading=()=>s.node("div","等待","clinical-loading");c.ClinicalUI.textDownload=()=>s.node("button","文字報告");c.ClinicalUI.pacsControl=options=>{const value=s.node("div","本地 PACS","pacs-control");value.options=options;return value;};
 c.window.FileCompare={context(){}};c.work.online=true;c.extensionTask="";c.dialog=()=>{$("#dataDialog").open=true;};c.setPatientDialogContext=()=>{};c.reviewPatientIdentity=()=>({name:"合成病人"});c.JobProgress={pending(){},publish(){}};
 c.Option=function(label,value){const option=s.node("option",label);option.value=value;return option;};
 c.table=(_headers,rows)=>{shown.push(rows);return s.node("table");};c.paginate=(_host,page,total,change)=>{pages.push({page,total,change});};
 c.api=async(path,values)=>{calls.push({path,values});if(path==="/reviews/history/read")return {data,complete,updated_at:"same-second",issues:[],coverage:{orders_start:"2015-11-25"}};if(path==="/reviews/pacs/open")return {status:"HANDOFF_SENT",message:"合成交接"};if(path==="/tasks/start"){if(startGate)await startGate.promise;return {task_id:"TASK"};}if(path.startsWith("/tasks/detail"))return {status,revision,updated_at:"same-second"};throw new Error(path);};
 s.run("vghks_bot/static/review-history.js");const ui=c.window.ReviewHistoryUI;
 await ui.open("order_report","TEST001","ORDER");
 const panel=$("#dataContent"),viewport=panel.querySelector(".report-gallery-viewport"),stage=viewport.firstElementChild,iframe=stage.firstElementChild,detailDisclosure=panel.querySelector("details");viewport.scrollTop=77;
 const controls=()=>[...$("#dataDialogActions").querySelectorAll(".history-action"),...panel.querySelectorAll(".history-action")];
 assert.equal($("#dataDialog").classList.contains("report-view-dialog"),true);assert.ok($("#dataDialogActions").querySelector(".history-controls"));assert.equal(panel.querySelector(".history-controls"),null,"report toolbar occupied another content row");assert.equal(detailDisclosure.open,false,"order details were initially expanded");detailDisclosure.open=true;
 assertReportToolbar($("#dataDialogActions"),panel);
 items[1]={...items[1],status:"ready"};data={...payload(items,[pdf,image]),details:[...details,{fields:{附註:"漸進更新"}}]};revision++;
 const previousReads=calls.filter(value=>value.path==="/reviews/history/read").length;await ui.poll();
 assert.equal(calls.filter(value=>value.path==="/reviews/history/read").length,previousReads+1,"same-second revision failed to read a newly ready image");
 assert.equal(viewport.firstElementChild,stage);assert.equal(stage.firstElementChild,iframe);assert.equal(viewport.scrollTop,77);assert.equal(panel.querySelector("details"),detailDisclosure);assert.equal(detailDisclosure.open,true,"progress collapsed an explicitly expanded detail section");
 const tiles=panel.querySelectorAll(".report-gallery-tile");assert.equal(tiles[1].dataset.status,"ready");
 await tiles[1].emit("click");const mainImage=viewport.firstElementChild;revision++;await ui.poll();assert.equal(viewport.firstElementChild,mainImage);
 startGate=deferred();const force=controls().find(value=>value.textContent==="更新資料").run();await tick();
 assert.equal(viewport.firstElementChild,mainImage,"force refresh cleared previous report before a replacement arrived");
 assert.equal(calls.filter(value=>value.path==="/tasks/start").at(-1).values.force,true);startGate.resolve();await force;startGate=null;
 status="partial";revision++;await ui.poll();await controls().find(value=>value.textContent==="重試未完成").run();
 assert.equal(calls.filter(value=>value.path==="/tasks/start").at(-1).values.force,false,"retry unnecessarily forced successful branches");
 complete=true;status="completed";data=[...Array.from({length:45},(_,index)=>({id:"EYE"+index,name:"Beta "+index,date:"2026-10-27",order_date:"2026-10-27",execution_date:"2026-10-28",ophthalmic:true})),...Array.from({length:60},(_,index)=>({id:"OTHER"+index,name:"Alpha "+index,date:"2026-09-01",order_date:"2026-09-01",ophthalmic:false}))];
 await ui.open("orders","TEST001");assert.equal($("#dataDialog").classList.contains("report-view-dialog"),false,"report-only layout leaked into order list");assert.equal($("#dataDialog").classList.contains("order-list-dialog"),true);assert.ok($("#dataDialogActions").querySelector(".history-controls"));assert.equal(panel.querySelector(".history-controls"),null);assert.equal(pages.at(-1).total,45,"historical orders did not default to ophthalmic checks");
 assert.equal(panel.querySelectorAll(".history-action").find(value=>value.textContent.startsWith("眼科檢查"))["aria-pressed"],"true");
 const grid=panel.querySelector(".history-order-table"),headers=grid.querySelectorAll("th"),names=()=>grid.querySelector("tbody").children.map(row=>row.children[0].textContent);
 assert.equal(panel.querySelectorAll("button").some(value=>value.textContent==="更早醫囑"),false,"historical orders retained the misleading visit-history shortcut");
 assert.deepEqual(names().slice(0,3),["Beta 0","Beta 1","Beta 2"]);
 await panel.querySelectorAll(".history-action").find(value=>value.textContent.startsWith("全部")).run();assert.equal(pages.at(-1).total,105,"all tab hid other reports");
 assert.equal(panel.querySelectorAll("table").length,1,"orders were split into multiple tables");
 assert.equal(headers[0]["aria-sort"],"ascending");assert.deepEqual(names().slice(0,3),["Alpha 0","Alpha 1","Alpha 2"]);
 assert.equal(names().length,40);assert.equal(grid.querySelector("tbody").firstElementChild.children[2].textContent,"—","execution date was inferred from the order date");
 assert.equal(panel.querySelectorAll(".history-order-category").length,0);assert.equal(panel.querySelectorAll("h4").length,0);
 assert.equal(panel.all().some(value=>value.textContent.includes("點選後才讀取")),false);
 const listScroll=panel.querySelector(".history-orders-scroll");listScroll.scrollTop=120;
 await headers[0].firstElementChild.run();assert.equal(headers[0]["aria-sort"],"descending");assert.equal(names()[0],"Beta 44");assert.equal(listScroll.scrollTop,0,"sorting left the new first rows off-screen");
 await headers[1].firstElementChild.run();assert.equal(headers[1]["aria-sort"],"descending");assert.equal(headers[0]["aria-sort"],undefined);assert.ok(names().every(name=>name.startsWith("Beta")));
 listScroll.scrollTop=180;pages.at(-1).change(1);assert.equal(listScroll.scrollTop,0,"pagination retained the previous page's scroll position");assert.equal(names().length,40);assert.ok(names().slice(0,5).every(name=>name.startsWith("Beta")));assert.ok(names().slice(5).every(name=>name.startsWith("Alpha")),"sorting happened after page slicing");
 await headers[1].firstElementChild.run();assert.equal(headers[1]["aria-sort"],"ascending");assert.equal(pages.at(-1).page,0);assert.ok(names().every(name=>name.startsWith("Alpha")));
 const search=panel.querySelector("input");search.value="2026-09";await search.emit("input");assert.equal(pages.at(-1).total,60,"date search discarded matching other orders");
 assert.equal(panel.querySelectorAll(".history-action").find(value=>value.textContent.startsWith("全部"))["aria-pressed"],"true","cross-category search changed the default scope");
 search.value="Alpha 59";await search.emit("input");assert.equal(pages.at(-1).total,1);assert.equal(names()[0],"Alpha 59");
 const direct=panel.querySelector(".pacs-control").options;
 assert.equal(direct.scope.resource,"order_report");assert.equal(direct.scope.reference,"OTHER59");assert.equal(direct.scope.mrn,"TEST001");assert.equal(direct.buttonText,"本地 PACS");assert.equal(direct.current(),true);
 assert.equal(calls.filter(call=>call.path==="/reviews/pacs/open").length,0,"rendering sent a native command");await direct.request(direct.scope);assert.equal(calls.at(-1).path,"/reviews/pacs/open");assert.equal($("#dataDialog").classList.contains("order-list-dialog"),true,"direct PACS opened the JPG report");
 search.value="";await search.emit("input");await panel.querySelectorAll(".history-action").find(value=>value.textContent.startsWith("眼科檢查")).run();assert.equal(pages.at(-1).total,45);
 await panel.querySelectorAll(".history-action").find(value=>value.textContent.startsWith("其他（")).run();assert.equal(pages.at(-1).page,0);assert.equal(pages.at(-1).total,60);
 assert.equal(panel.querySelector("input"),search,"filtering replaced the focused search input");assert.equal(panel.querySelector(".history-order-table"),grid,"sorting replaced the keyboard-focused headers");
 data=[{id:"FIRST",name:"Alpha",order_date:"2025-01-01",execution_date:"2026-02-01"},{id:"SECOND",name:"Beta",order_date:"2026-01-01",execution_date:"2025-02-01"},{id:"MISSING",name:"Gamma"}];
 await ui.open("case_orders","TEST001","VISIT");assert.equal(direct.current(),false,"old list scope remained active after changing visits");
 const caseGrid=panel.querySelector("table"),caseBody=caseGrid.querySelector("tbody");assert.equal(caseBody.firstElementChild.children[0].textContent,"Alpha","case orders did not default to name sorting");
 await caseGrid.querySelectorAll("th")[1].firstElementChild.run();assert.equal(caseBody.firstElementChild.children[0].textContent,"Beta","sorting used execution time instead of order time");assert.equal(caseBody.children.at(-1).children[0].textContent,"Gamma");
 await caseGrid.querySelectorAll("th")[1].firstElementChild.run();assert.equal(caseBody.children.at(-1).children[0].textContent,"Gamma","missing dates sorted before known dates");
 await caseGrid.querySelectorAll("th")[2].firstElementChild.run();assert.equal(caseBody.firstElementChild.children[0].textContent,"Alpha","execution sorting used order time");assert.equal(caseGrid.querySelectorAll("th")[2]["aria-sort"],"descending");assert.equal(caseGrid.querySelectorAll("th")[1]["aria-sort"],undefined);
 await caseGrid.querySelectorAll("th")[2].firstElementChild.run();assert.equal(caseBody.firstElementChild.children[0].textContent,"Beta");assert.equal(caseBody.children.at(-1).children[0].textContent,"Gamma","missing execution dates sorted before known dates");
 c.root.read_only=true;await ui.open("case_orders","TEST001","VISIT");assert.equal(panel.querySelectorAll(".pacs-control").length,0);ui.reset();assert.equal($("#dataDialog").classList.contains("order-list-dialog"),false);
 c.root.read_only=false;data=[
  {id:"A-OLD",name:"Alpha",ophthalmic:true,order_date:"2024-01-01",execution_date:"2026-01-01"},
  {id:"B-NEW",name:"Beta",ophthalmic:true,order_date:"2026-01-01",execution_date:"2024-01-01"},
  {id:"A-NEW",name:"Alpha",ophthalmic:true,order_date:"2025-01-01",execution_date:"2025-01-01"},
  {id:"A-MISSING",name:"Alpha",ophthalmic:true,date:"2027-01-01"},
  {id:"OTHER",name:"Aardvark",ophthalmic:false,order_date:"2027-01-01"}];
 await ui.open("orders","TEST001");
 const groupedGrid=panel.querySelector("table"),orderedIds=()=>groupedGrid.querySelector("tbody").children.map(row=>row.querySelector(".pacs-control").options.scope.reference);
 assert.deepEqual(orderedIds(),["A-NEW","A-OLD","A-MISSING","B-NEW"],"default name groups did not put newest order times first or inferred a missing order date");
 await groupedGrid.querySelectorAll("th")[1].firstElementChild.run();assert.deepEqual(orderedIds(),["B-NEW","A-NEW","A-OLD","A-MISSING"]);
 await groupedGrid.querySelectorAll("th")[2].firstElementChild.run();assert.deepEqual(orderedIds(),["A-OLD","A-NEW","B-NEW","A-MISSING"]);
 await groupedGrid.querySelectorAll("th")[2].firstElementChild.run();assert.deepEqual(orderedIds(),["B-NEW","A-NEW","A-OLD","A-MISSING"]);
 await groupedGrid.querySelectorAll("th")[0].firstElementChild.run();assert.deepEqual(orderedIds(),["A-NEW","A-OLD","A-MISSING","B-NEW"]);ui.reset();
}
(async()=>{await galleryTests();await imageFitTests();await thumbnailTests();await historyTests();process.stdout.write("Report gallery revision polling, classification, fit and manual zoom, lazy thumbnails and cleanup passed\n");})().catch(error=>{console.error(error);process.exitCode=1;});
