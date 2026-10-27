"use strict";

window.ReportGallery=(()=>{
 const labels={pending:"等待下載",downloading:"下載中",ready:"已取得",failed:"未取得"};
 function element(tag,text,cls){const value=document.createElement(tag);if(text!==undefined)value.textContent=text;if(cls)value.className=cls;return value;}
 function button(label,run){const value=element("button",label,"attachment-control");value.type="button";value.addEventListener("click",run);return value;}
 function create({urlFor,identity}){
  const root=element("section",undefined,"report-gallery"),rail=element("div",undefined,"report-gallery-rail"),main=element("div",undefined,"report-gallery-main");
  const heading=element("div",undefined,"report-gallery-heading"),title=element("strong"),tools=element("div",undefined,"attachment-toolbar"),viewport=element("div",undefined,"report-gallery-viewport"),notice=element("p",undefined,"caption report-gallery-notice");
  rail.setAttribute("aria-label","報告附件");main.append(heading,tools,notice,viewport);heading.append(title);root.append(rail,main);
  const entries=new Map(),positions=new Map(),queue=[];let selected="",userSelected=false,selectedDigest="",controlsKey="",file=null,zoom=1,disposed=false,active=0,order={};
  const observer=typeof IntersectionObserver==="function"?new IntersectionObserver(changes=>{for(const change of changes)if(change.isIntersecting){const entry=entries.get(change.target.dataset.refKey);if(entry)schedule(entry);}},{root:rail,rootMargin:"80px"}):null;
  function release(entry){if(entry.objectURL){URL.revokeObjectURL(entry.objectURL);entry.objectURL="";}if(entry.source){entry.source.src="";entry.source=null;}entry.thumb.replaceChildren(element("span",entry.asset?.mime==="application/pdf"?"PDF":"影像","report-gallery-placeholder"));entry.thumbDigest="";}
  function schedule(entry){
   const asset=entry.asset;if(disposed||!asset?.digest||!asset.mime?.startsWith("image/")||entry.thumbDigest===asset.digest)return;
   entry.thumbDigest=asset.digest;queue.push({entry,digest:asset.digest});drain();
  }
  function drain(){while(!disposed&&active<2&&queue.length){const item=queue.shift();if(entries.get(item.entry.key)!==item.entry||item.entry.asset?.digest!==item.digest)continue;active++;thumbnail(item.entry,item.digest).finally(()=>{active--;drain();});}}
  async function thumbnail(entry,digest){
   let image;
   try{
    image=new Image();entry.source=image;image.decoding="async";image.src=urlFor(entry.asset);await image.decode();
    if(disposed||entries.get(entry.key)!==entry||entry.asset?.digest!==digest)return;
    const canvas=document.createElement("canvas"),scale=Math.min(1,240/image.naturalWidth,180/image.naturalHeight);
    canvas.width=Math.max(1,Math.round(image.naturalWidth*scale));canvas.height=Math.max(1,Math.round(image.naturalHeight*scale));
    canvas.getContext("2d").drawImage(image,0,0,canvas.width,canvas.height);
    const blob=await new Promise(resolve=>canvas.toBlob(resolve,"image/jpeg",.8));canvas.width=canvas.height=0;
    if(!blob||disposed||entries.get(entry.key)!==entry||entry.asset?.digest!==digest)return;
    const preview=element("img");preview.alt="附件 "+entry.ordinal+" 縮圖";preview.width=120;preview.height=90;
    entry.objectURL=URL.createObjectURL(blob);preview.src=entry.objectURL;entry.thumb.replaceChildren(preview);
   }catch(_){if(!disposed&&entries.get(entry.key)===entry)entry.thumb.setAttribute("aria-label","縮圖無法產生，仍可開啟原檔");}
   finally{if(image){image.src="";if(entry.source===image)entry.source=null;}}
  }
  function visibleFallback(){if(observer||disposed)return;const bounds=rail.getBoundingClientRect?.();for(const entry of entries.values()){const rect=entry.tile.getBoundingClientRect?.();if(bounds&&rect?rect.bottom>=bounds.top&&rect.top<=bounds.bottom&&rect.right>=bounds.left&&rect.left<=bounds.right:entry.ordinal<=6)schedule(entry);}}
  rail.addEventListener("scroll",visibleFallback,{passive:true});
  function savePosition(){if(selected)positions.set(selected,{zoom,left:viewport.scrollLeft,top:viewport.scrollTop});}
  function controls(entry){
   const key=selected+"/"+(entry?.asset?.digest||"");
   if(key===controlsKey){if(entry?.comparison)entry.comparison.fileCount=entries.size;return;}
   controlsKey=key;tools.replaceChildren();if(!entry?.asset?.digest)return;
   const asset=entry.asset,url=urlFor(asset),name=[order.date,order.name||"醫囑附件",entry.ordinal].filter(Boolean).join(" ");
   entry.comparison={...identity,digest:asset.digest,mime:asset.mime,name:order.name||"醫囑附件",source:"醫囑報告",date:order.date||"",fileIndex:entry.ordinal,fileCount:entries.size,reportId:order.id};
   tools.append(ClinicalUI.comparisonChoice(entry.comparison),ClinicalUI.assetTools({url,asset,name,image:()=>file}));
   const link=element("a","獨立開啟原檔","attachment-control");link.href=url;link.target="_blank";link.rel="noopener";tools.append(link);
   if(asset.mime?.startsWith("image/")){
    const value=element("span",Math.round(zoom*100)+"%","report-gallery-zoom");
    const resize=next=>{zoom=Math.max(.25,Math.min(4,next));file.style.width=zoom*100+"%";value.textContent=Math.round(zoom*100)+"%";savePosition();};
    tools.append(button("−",()=>resize(zoom-.25)),value,button("＋",()=>resize(zoom+.25)),button("符合寬度",()=>resize(1)));
   }
  }
  function show(){
   const entry=entries.get(selected);if(!entry)return;
   title.textContent="附件 "+entry.ordinal+" / "+entries.size;
   notice.textContent=entry.status==="failed"?(entry.message||"此附件未能取得，可重試未完成項目。")+(entry.asset?.digest?" 已存原檔仍可檢閱。":""):entry.status!=="ready"?(entry.asset?.digest?"正在更新；可繼續檢閱已存原檔。":labels[entry.status]||"等待取得附件"):"";
   notice.hidden=!notice.textContent;
   const digest=entry.asset?.digest||"";
   if(selectedDigest!==selected+"/"+digest){
    selectedDigest=selected+"/"+digest;const position=positions.get(selected)||{};zoom=position.zoom||1;
    if(file?.tagName==="IFRAME")file.src="about:blank";
    file=null;viewport.replaceChildren();
    if(digest){const pdf=entry.asset.mime==="application/pdf";file=element(pdf?"iframe":"img");const url=urlFor(entry.asset);file.src=pdf?ClinicalUI.pdfURL(url):url;file.title="報告附件 "+entry.ordinal;if(!pdf){file.alt=file.title;file.style.width=zoom*100+"%";}file.className="report-gallery-original";viewport.append(file);}
    else viewport.append(element("p",entry.status==="failed"?"附件尚未取得。":"附件取得後會顯示於此。","empty"));
    viewport.scrollLeft=position.left||0;viewport.scrollTop=position.top||0;
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
    if(!entry){const tile=element("button",undefined,"report-gallery-tile"),thumb=element("span",undefined,"report-gallery-thumbnail"),caption=element("span",undefined,"report-gallery-caption"),status=element("span",undefined,"report-gallery-status");tile.type="button";tile.dataset.refKey=key;tile.append(thumb,caption,status);tile.addEventListener("click",()=>select(key));entry={key,tile,thumb,caption,statusNode:status,objectURL:"",thumbDigest:""};entries.set(key,entry);rail.append(tile);observer?.observe(tile);}
    const asset=assets.get(key)||(item.digest?item:null);
    if(entry.asset?.digest!==asset?.digest)release(entry);
    entry.asset=asset;entry.ordinal=item.ordinal||index+1;entry.status=item.status||"ready";entry.message=item.message||"";
    entry.caption.textContent="附件 "+entry.ordinal;entry.statusNode.textContent=labels[entry.status]||entry.status;entry.tile.title=entry.message||entry.caption.textContent;entry.tile.dataset.status=entry.status;
    if(!entry.objectURL&&!entry.source)entry.thumb.textContent=asset?.mime==="application/pdf"||item.kind==="pdf"?"PDF":"影像";
   }
   for(const [key,entry] of entries)if(!keys.has(key)){observer?.unobserve(entry.tile);release(entry);entry.tile.remove();entries.delete(key);positions.delete(key);}
   if(!entries.has(selected)){selected="";userSelected=false;}
   if(!selected||!userSelected&&!entries.get(selected)?.asset?.digest){savePosition();selected=[...entries.values()].find(entry=>entry.asset?.digest)?.key||entries.keys().next().value||"";}
   root.hidden=!entries.size;if(selected)show();else{selectedDigest=controlsKey="";if(file?.tagName==="IFRAME")file.src="about:blank";file=null;viewport.replaceChildren();tools.replaceChildren();title.textContent="";}
   visibleFallback();
  }
  function dispose(){if(disposed)return;disposed=true;observer?.disconnect();queue.length=0;for(const entry of entries.values())release(entry);if(file?.tagName==="IFRAME")file.src="about:blank";viewport.replaceChildren();entries.clear();positions.clear();}
  return {element:root,update,dispose};
 }
 return {create};
})();
