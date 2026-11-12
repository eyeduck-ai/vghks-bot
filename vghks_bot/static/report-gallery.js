"use strict";

window.ReportGallery=(()=>{
 const labels={pending:"等待下載",downloading:"下載中",ready:"已取得",failed:"未取得"};
 function element(tag,text,cls){const value=document.createElement(tag);if(text!==undefined)value.textContent=text;if(cls)value.className=cls;return value;}
 function button(label,run){const value=element("button",label,"attachment-control");value.type="button";value.addEventListener("click",run);return value;}
 function create({urlFor,identity}){
  const root=element("section",undefined,"report-gallery"),rail=element("div",undefined,"report-gallery-rail"),main=element("div",undefined,"report-gallery-main");
  const heading=element("div",undefined,"report-gallery-heading"),title=element("strong"),tools=element("div",undefined,"attachment-toolbar"),viewport=element("div",undefined,"report-gallery-viewport"),notice=element("p",undefined,"caption report-gallery-notice");
  const header=element("div",undefined,"report-gallery-header dialog-toolbar");
  rail.setAttribute("aria-label","報告附件");heading.append(title);header.append(heading,tools);main.append(header,notice,viewport);root.append(rail,main);
  const entries=new Map(),positions=new Map(),queue=[];let selected="",userSelected=false,selectedDigest="",controlsKey="",file=null,pdfStage=null,zoom=1,imageFit=true,zoomControl=null,zoomSteps=[],stopImageLoad=null,disposed=false,active=0,order={};
  const observer=typeof IntersectionObserver==="function"?new IntersectionObserver(changes=>{
   for(const change of changes){
    const entry=entries.get(change.target.dataset.refKey);
    if(entry?.tile!==change.target)continue;
    entry.visible=change.isIntersecting;
    if(entry.visible)schedule(entry);
   }
  },{root:rail,rootMargin:"80px"}):null;
  function releasePreview(entry){if(entry.objectURL){URL.revokeObjectURL(entry.objectURL);entry.objectURL="";}}
  function releaseSource(entry,image=entry.source){
   if(!image||entry.source!==image)return;
   entry.source=null;image.removeAttribute("src");
  }
  function release(entry){
   const job=entry.thumbnail;entry.thumbnail=null;
   job?.cancel?.();releasePreview(entry);releaseSource(entry);
   entry.thumb.removeAttribute("aria-label");
   entry.thumb.replaceChildren(element("span",entry.asset?.mime==="application/pdf"?"PDF":"影像","report-gallery-placeholder"));
  }
  function currentThumbnail(job){
   const {entry,digest}=job;
   return !disposed&&entries.get(entry.key)===entry&&entry.thumbnail===job&&entry.asset?.digest===digest;
  }
  function schedule(entry){
   const asset=entry.asset;
   if(disposed||!entry.visible||!asset?.digest||!asset.mime?.startsWith("image/")||entry.thumbnail?.digest===asset.digest)return;
   const job={entry,digest:asset.digest};entry.thumbnail=job;queue.push(job);drain();
  }
  function drain(){
   while(!disposed&&active<2&&queue.length){
    const job=queue.shift();
    if(!currentThumbnail(job))continue;
    if(!job.entry.visible){job.entry.thumbnail=null;continue;}
    active++;thumbnail(job).finally(()=>{active--;drain();});
   }
  }
  function setThumbnailImage(image,entry){
   image.alt="附件 "+entry.ordinal+" 縮圖";image.width=120;image.height=90;
  }
  function loadThumbnailSource(job,image,url){
   return new Promise(resolve=>{
    const finish=loaded=>{image.onload=image.onerror=null;job.cancel=null;resolve(loaded);};
    image.onload=()=>finish(true);image.onerror=job.cancel=()=>finish(false);image.src=url;
   });
  }
  async function thumbnail(job){
   const {entry}=job;let image,canvas,loaded=false,keepSource=false;
   function useSource(){
    releasePreview(entry);entry.thumb.replaceChildren(image);keepSource=true;
   }
   try{
    image=new Image();entry.source=image;image.decoding="async";setThumbnailImage(image,entry);
    loaded=await loadThumbnailSource(job,image,urlFor(entry.asset));
    if(!currentThumbnail(job))return;
    if(!loaded)throw new Error("Local attachment image unavailable");
    canvas=document.createElement("canvas");
    const scale=Math.min(1,240/image.naturalWidth,180/image.naturalHeight);
    canvas.width=Math.max(1,Math.round(image.naturalWidth*scale));canvas.height=Math.max(1,Math.round(image.naturalHeight*scale));
    canvas.getContext("2d").drawImage(image,0,0,canvas.width,canvas.height);
    const blob=await new Promise(resolve=>canvas.toBlob(resolve,"image/jpeg",.8));
    if(!currentThumbnail(job))return;
    if(!blob){useSource();return;}
    const preview=element("img"),url=URL.createObjectURL(blob);setThumbnailImage(preview,entry);
    entry.objectURL=url;
    preview.addEventListener("load",()=>{if(currentThumbnail(job)&&entry.objectURL===url)releaseSource(entry,image);},{once:true});
    preview.addEventListener("error",()=>{if(currentThumbnail(job)&&entry.objectURL===url&&entry.source===image)useSource();},{once:true});
    preview.src=url;entry.thumb.replaceChildren(preview);keepSource=true;
   }catch(_){
    if(currentThumbnail(job)){
     if(loaded)useSource();
     else entry.thumb.setAttribute("aria-label","縮圖無法讀取，仍可開啟原檔");
    }
   }finally{
    job.cancel?.();
    if(canvas)canvas.width=canvas.height=0;
    if(!keepSource)releaseSource(entry,image);
   }
  }
  function visibleFallback(){
   if(observer||disposed)return;
   const bounds=rail.getBoundingClientRect?.();
   for(const entry of entries.values()){
    const rect=entry.tile.getBoundingClientRect?.();
    entry.visible=bounds&&rect?rect.bottom>=bounds.top&&rect.top<=bounds.bottom&&rect.right>=bounds.left&&rect.left<=bounds.right:entry.ordinal<=6;
    if(entry.visible)schedule(entry);
   }
  }
  rail.addEventListener("scroll",visibleFallback,{passive:true});
  function savePosition(){if(selected)positions.set(selected,{zoom,imageFit,left:viewport.scrollLeft,top:viewport.scrollTop});}
  function restorePosition(position){viewport.scrollLeft=position.left||0;viewport.scrollTop=position.top||0;}
  function fitScale(){
   if(!file?.naturalWidth||!file.naturalHeight||!viewport.clientWidth||!viewport.clientHeight)return null;
   if(imageFit==="width")return viewport.clientWidth/file.naturalWidth;
   if(imageFit==="height")return viewport.clientHeight/file.naturalHeight;
   return Math.min(1,viewport.clientWidth/file.naturalWidth,viewport.clientHeight/file.naturalHeight);
  }
  function updateZoomControl(){
   if(!zoomControl)return;
   const percent=Math.round(zoom*100),reset=pdfStage?"重設為 100%":"回到最適比例";
   const waiting=!pdfStage&&!file?.dataset.imageSized;
   for(const step of zoomSteps){step.disabled=waiting;if(step.dataset.fit)step.setAttribute("aria-pressed",String(imageFit===step.dataset.fit));}
   zoomControl.textContent=waiting?"…":(percent||+(zoom*100).toPrecision(2))+"%";
   zoomControl.title=reset;
   zoomControl.setAttribute("aria-label",zoomControl.textContent==="…"?reset:"目前縮放 "+zoomControl.textContent+"，"+reset);
  }
  function applyZoom(){
   if(!file)return;
   if(pdfStage){pdfStage.style.width=zoom*100+"%";pdfStage.style.height=zoom*100+"%";file.style.width=100/zoom+"%";file.style.height=100/zoom+"%";file.style.transform="scale("+zoom+")";}
   else if(file.naturalWidth&&file.naturalHeight){
    if(imageFit){const fit=fitScale();if(fit===null)return;zoom=fit;}
    file.style.width=Math.floor(file.naturalWidth*zoom*100)/100+"px";
    file.dataset.imageSized="true";
   }
   updateZoomControl();
  }
  const refit=()=>{if(!disposed&&file&&!pdfStage&&imageFit)applyZoom();};
  const fitObserver=typeof ResizeObserver==="function"?new ResizeObserver(refit):null;
  if(fitObserver)fitObserver.observe(viewport);else window.addEventListener?.("resize",refit);
  function stepImage(direction){
   if(zoom<.25||(zoom===.25&&direction<0))return direction<0?zoom*.8:Math.min(.25,zoom*1.25);
   const quarters=zoom*4;
   // Step through fixed scales above 25% so original size is always reachable.
   return (direction>0?Math.floor(quarters+1e-9)+1:Math.ceil(quarters-1e-9)-1)/4;
  }
  function clearOriginal(){
   stopImageLoad?.();stopImageLoad=null;
   if(file?.tagName==="IFRAME")file.src="about:blank";
   file=pdfStage=zoomControl=null;zoomSteps=[];viewport.replaceChildren();
  }
  function controls(entry){
   const key=selected+"/"+(entry?.asset?.digest||"");
   if(key===controlsKey){if(entry?.comparison)entry.comparison.fileCount=entries.size;return;}
   controlsKey=key;zoomControl=null;zoomSteps=[];tools.replaceChildren();if(!entry?.asset?.digest)return;
   const asset=entry.asset,url=urlFor(asset),name=[order.date,order.name||"醫囑附件",entry.ordinal].filter(Boolean).join(" ");
   entry.comparison={...identity,digest:asset.digest,mime:asset.mime,name:order.name||"醫囑附件",source:"醫囑報告",date:order.date||"",fileIndex:entry.ordinal,fileCount:entries.size,reportId:order.id};
   const fileActions=element("div",undefined,"report-gallery-file-actions dialog-toolbar");
   fileActions.append(ClinicalUI.comparisonChoice(entry.comparison),ClinicalUI.assetTools({url,asset,name,image:()=>file}));
   const link=element("a","獨立開啟原檔","attachment-control");link.href=url;link.target="_blank";link.rel="noopener";fileActions.append(link);tools.append(fileActions);
   if(asset.mime?.startsWith("image/")||asset.mime==="application/pdf"){
    const reset=()=>{if(pdfStage)zoom=1;else{imageFit=true;restorePosition({});}applyZoom();savePosition();};
    const resize=direction=>{
     if(pdfStage)zoom=Math.max(.25,Math.min(4,zoom+direction*.25));
     else{if(!file?.dataset.imageSized)return;const minimum=Math.min(.01,(fitScale()??1)/2);imageFit=false;zoom=Math.max(minimum,Math.min(4,stepImage(direction)));}
     applyZoom();savePosition();
    };
    zoomControl=button("",reset);zoomControl.classList.add("report-gallery-zoom");
    const less=button("−",()=>resize(-1)),more=button("＋",()=>resize(1));less.setAttribute("aria-label","縮小附件");more.setAttribute("aria-label","放大附件");
    zoomSteps=[less,more];
    const group=element("div",undefined,"report-gallery-zoom-group");group.setAttribute("role","group");group.setAttribute("aria-label","附件縮放");group.append(less,zoomControl,more);tools.append(group);
    if(asset.mime?.startsWith("image/"))for(const [mode,label] of [["width","最適寬度"],["height","最適高度"]]){
     const fit=button(label,()=>{imageFit=mode;restorePosition({});applyZoom();savePosition();});fit.dataset.fit=mode;
     fit.classList.add("report-gallery-fit");zoomSteps.push(fit);group.append(fit);
    }
    updateZoomControl();
   }
  }
  function show(){
   const entry=entries.get(selected);if(!entry)return;
   title.textContent="附件 "+entry.ordinal+" / "+entries.size;
   notice.textContent=entry.status==="failed"?(entry.message||"此附件未能取得，可重試未完成項目。")+(entry.asset?.digest?" 已存原檔仍可檢閱。":""):entry.status!=="ready"?(entry.asset?.digest?"正在更新；可繼續檢閱已存原檔。":labels[entry.status]||"等待取得附件"):"";
   notice.hidden=!notice.textContent;
   const digest=entry.asset?.digest||"";
   if(selectedDigest!==selected+"/"+digest){
    selectedDigest=selected+"/"+digest;const position=positions.get(selected)||{};zoom=position.zoom||1;imageFit=position.imageFit??true;
    clearOriginal();
    if(digest){
     const pdf=entry.asset.mime==="application/pdf";file=element(pdf?"iframe":"img");const url=urlFor(entry.asset);file.title="報告附件 "+entry.ordinal;file.className="report-gallery-original";
     if(pdf){file.src=ClinicalUI.pdfURL(url);pdfStage=element("div",undefined,"report-gallery-pdf-stage");pdfStage.append(file);viewport.append(pdfStage);}
     else{
      const image=file;image.alt=image.title;
      const loaded=()=>{if(disposed||file!==image)return;stopImageLoad=null;applyZoom();if(!imageFit)restorePosition(position);};
      image.addEventListener("load",loaded,{once:true});stopImageLoad=()=>image.removeEventListener?.("load",loaded);
      image.src=url;viewport.append(image);
     }
     applyZoom();
    }
    else viewport.append(element("p",entry.status==="failed"?"附件尚未取得。":"附件取得後會顯示於此。","empty"));
    restorePosition(position);
   }
   controls(entry);for(const item of entries.values())item.tile.setAttribute("aria-pressed",String(item.key===selected));
  }
  function select(key){if(disposed||!entries.has(key))return;userSelected=true;if(key===selected)return;savePosition();selected=key;show();}
  function update(data){
   if(disposed)return;order=data.order||{};
   const assets=new Map((data.assets||[]).map(asset=>[asset.ref_key||asset.digest,asset]));
   const items=data.attachment_items?.length?data.attachment_items:(data.assets||[]).map((asset,index)=>({...asset,ref_key:asset.ref_key||asset.digest,ordinal:asset.ordinal||index+1,status:"ready"}));
   const keys=new Set();
   for(const [index,item] of items.entries()){
    const key=item.ref_key||item.id||item.digest;if(!key||keys.has(key))continue;keys.add(key);
    let entry=entries.get(key);
    if(!entry){const tile=element("button",undefined,"report-gallery-tile"),thumb=element("span",undefined,"report-gallery-thumbnail"),caption=element("span",undefined,"report-gallery-caption"),status=element("span",undefined,"report-gallery-status");tile.type="button";tile.dataset.refKey=key;tile.append(thumb,caption,status);tile.addEventListener("click",()=>select(key));entry={key,tile,thumb,caption,statusNode:status,objectURL:"",visible:false};entries.set(key,entry);rail.append(tile);observer?.observe(tile);}
    const asset=assets.get(key)||(item.digest?item:null);
    if(entry.asset?.digest!==asset?.digest)release(entry);
    entry.asset=asset;entry.ordinal=item.ordinal||index+1;entry.status=item.status||"ready";entry.message=item.message||"";
    entry.caption.textContent=String(entry.ordinal);entry.statusNode.textContent=entry.status==="ready"?"":labels[entry.status]||entry.status;entry.statusNode.hidden=entry.status==="ready";entry.tile.title=entry.message||"附件 "+entry.ordinal;entry.tile.setAttribute("aria-label","附件 "+entry.ordinal+(entry.status==="ready"?"":"，"+entry.statusNode.textContent));entry.tile.dataset.status=entry.status;
    if(!entry.objectURL&&!entry.source)entry.thumb.textContent=asset?.mime==="application/pdf"||item.kind==="pdf"?"PDF":"影像";
    if(observer)schedule(entry);
   }
   for(const [key,entry] of entries)if(!keys.has(key)){observer?.unobserve(entry.tile);release(entry);entry.tile.remove();entries.delete(key);positions.delete(key);}
   if(!entries.has(selected)){selected="";userSelected=false;}
   if(!selected||!userSelected&&!entries.get(selected)?.asset?.digest){savePosition();selected=[...entries.values()].find(entry=>entry.asset?.digest)?.key||entries.keys().next().value||"";}
   root.hidden=!entries.size;if(selected)show();else{selectedDigest=controlsKey="";clearOriginal();tools.replaceChildren();title.textContent="";}
   visibleFallback();
  }
  function dispose(){if(disposed)return;disposed=true;if(fitObserver)fitObserver.disconnect();else window.removeEventListener?.("resize",refit);observer?.disconnect();queue.length=0;for(const entry of entries.values())release(entry);clearOriginal();entries.clear();positions.clear();}
  return {element:root,update,dispose};
 }
 return {create};
})();
