"use strict";
window.JobProgress = (() => {
  const tasks=new Map(),activeStatuses=new Set(["queued","running","cancelling"]);
  const scopedAccount=new URLSearchParams(location.search).get("account");
  const embedded=parent!==window&&/^[a-f0-9]{32}$/.test(scopedAccount||"");
  let owner=scopedAccount||"local",card=null,minimized=false,expanded=false,serial=0,readOnly=false;
  const dialogStack=[];
  const taskNames={list:"門診清單",review:"病歷檢閱",resolve:"病人基本資料",history:"歷史資料",analysis:"進階工具",surgery_schedule:"手術排程",approval_sync:"審查同步",approval_case:"審查明細",earnings_capture:"薪資業績",earnings_options:"薪資期別"};
  function publish(run,account=owner){
    if(!run?.id||!account)return;
    const key=account+":"+run.id,old=tasks.get(key);
    const terminal=!activeStatuses.has(run.status);
    const time=Date.parse(run.finished_at||run.updated_at||"");
    const ended=terminal?(old?.status===run.status?old._ended:(Number.isFinite(time)?time:old?Date.now():0)):0;
    const next={...old,...run,_owner:account,_ended:ended};
    if(!run.progress&&run.counts)delete next.progress;
    tasks.set(key,next);
    if(embedded){parent.postMessage({type:"bot:progress",account,runs:[next]},location.origin);return;}
    renderCard();
  }
  function pendingRun(name,stage,account=owner){
    const run={id:"pending-"+(++serial)+"-"+Date.now(),name,status:"queued",_pending:true,progress:{done:0,total:null,unit:"項",stage}};
    publish(run,account);return run;
  }
  function context(account,readonly=false){owner=account;readOnly=readonly;minimized=false;expanded=false;renderCard();}
  function live(run){return activeStatuses.has(run.status)||Date.now()-(run._ended||0)<(run.status==="completed"?5000:15000);}
  function element(tag,text,cls){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;}
  function control(label,callback){const button=element("button",label);button.type="button";button.addEventListener("click",callback);return button;}
  function mountCard(){
    if(embedded||!document.body)return;
    if(!card){card=element("aside",undefined,"floating-job-progress");card.id="floatingJobProgress";card.hidden=true;card.setAttribute("aria-label","資料抓取進度");}
    let dialog=dialogStack.filter(item=>item.open&&item.isConnected).at(-1)||[...document.querySelectorAll("dialog[open]")].at(-1);
    if(!dialog)for(const frame of document.querySelectorAll("#moduleFrame,#cataractFrame")){
      if(frame.hidden||!frame.getClientRects().length||frame.dataset.account!==owner)continue;
      try{dialog=[...frame.contentDocument.querySelectorAll("dialog[open]")].at(-1);}catch(_){}
      if(dialog)break;
    }
    const host=dialog||document.body;if(card.parentElement!==host)host.append(card);
    const heading=dialog?.querySelector(".dialog-heading,.file-compare-heading,.scan-browser-head,.drawer-header,h2")||document.querySelector(".topbar");
    const box=heading?.getBoundingClientRect();
    card.style.top=Math.max(8,box&&box.height&&box.bottom<140?box.bottom+5:8)+"px";
  }
  function renderCard(){
    if(embedded)return;mountCard();if(!card)return;
    const rows=[...tasks.values()].filter(run=>run._owner===owner&&live(run)).sort((a,b)=>Number(activeStatuses.has(b.status))-Number(activeStatuses.has(a.status)));
    card.hidden=!rows.length;if(!rows.length)return;
    const focused=card.ownerDocument.activeElement;
    const focus=card.contains(focused)?focused.dataset.action:null;
    card.replaceChildren();card.classList.toggle("minimized",minimized);
    const head=element("div",undefined,"floating-job-head");
    const title=control((rows[0].name||taskNames[rows[0].kind]||"資料抓取")+(rows.length>1?` · ${rows.length} 項`:""),()=>{expanded=!expanded;minimized=false;renderCard();});
    title.dataset.action="expand";title.setAttribute("aria-expanded",String(expanded));
    const toggle=control(minimized?"＋":"−",()=>{minimized=!minimized;renderCard();});toggle.dataset.action="minimize";toggle.setAttribute("aria-label",minimized?"展開抓取進度":"縮小抓取進度");head.append(title,toggle);card.append(head);
    if(!minimized)for(const run of (expanded?rows:rows.slice(0,1))){
      const row=element("div",undefined,"floating-job-row");row.dataset.taskId=run.id;
      if(expanded)row.append(element("strong",run.name||taskNames[run.kind]||"資料抓取"));
      row.append(create(run));
      const status=element("small",({queued:"排隊中",running:"執行中",cancelling:"暫停中",completed:"完成",partial:"部分完成",failed:"失敗",paused:"待續跑",interrupted:"待續跑",cancelled:"已暫停"})[run.status]||run.status);
      const actions=element("div",undefined,"floating-job-actions");actions.append(status);
      const action=(label,type)=>{const button=control(label,()=>window.dispatchEvent(new CustomEvent("jobprogressaction",{detail:{action:type,run}})));button.dataset.action=type+run.id;actions.append(button);};
      if(!run._pending&&!readOnly&&!run.read_only){if(activeStatuses.has(run.status))action("暫停","stop");else if(run.status!=="completed"&&run.kind!=="list")action("續跑","resume");}
      if(!run._pending)action("爬蟲紀錄","debug");row.append(actions);card.append(row);
    }
    if(focus)[...card.querySelectorAll("button")].find(button=>button.dataset.action===focus)?.focus({preventScroll:true});
  }
  function remaining(run) {const end=Date.parse(run.finished_at||"");return Number.isFinite(end)?Math.max(0,5000-(Date.now()-end)):0;}
  const visible=run=>run.status!=="completed"||remaining(run)>0;
  function value(run) {
    if(run.progress)return run.progress;
    const c=run.counts||{},days=run.kind==="list"||run.stage==="registrations";
    return {done:days?(c.days_done||0):(c.patients_done||0),total:days?c.days_total:c.patients_total,failed:c.errors||c.days_failed||0,unit:days?"天":"位病人",stage:run.message||""};
  }
  function create(run) {
    const p=value(run),box=document.createElement("div");box.className="job-progress";box.dataset.status=run.status||"";
    if(!visible(run)){box.hidden=true;return box;}
    const bar=document.createElement("progress"),line=document.createElement("div"),stage=document.createElement("small"),text=document.createElement("span");bar.setAttribute("aria-label",(run.name||"資料抓取")+"進度");line.className="job-progress-line";
    if(p.total!=null){bar.max=Math.max(1,p.total);bar.value=p.total?Math.min(p.done,p.total):(run.status==="completed"?1:0);}
    else if(!["queued","running","cancelling"].includes(run.status)){bar.max=1;bar.value=0;}
    text.textContent=(p.total==null?`已處理 ${p.done||0} ${p.unit||"項"} · 確認總量中`:`${p.done} / ${p.total} ${p.unit||"項"}`)+(p.failed?` · ${p.failed} 項未完成`:"");
    stage.textContent=p.stage||run.name||"資料抓取";stage.title=stage.textContent;line.append(stage,text);box.append(line,bar);
    if(run.status==="completed")setTimeout(()=>{const card=box.closest('[data-transient="true"]');if(card)card.hidden=true;else box.remove();},remaining(run));
    return box;
  }
  function show(target,run){publish(run);if(typeof target==="string")target=document.querySelector(target);if(target)target.replaceChildren();}
  function pending(target,name,stage="正在送出查詢…"){
    if(typeof target==="string")target=document.querySelector(target);
    const account=owner,run=pendingRun(name,stage),box=create(run);
    if(target)target.replaceChildren();
    box.remove=()=>{const key=account+":"+run.id;if(tasks.get(key)?.status==="queued")removePending(run.id,account);renderCard();};
    return box;
  }
  // Observe only local task APIs. Pending state appears before network dispatch;
  // task snapshots reuse the application's existing polling and account gates.
  const originalFetch=window.fetch.bind(window);
  window.fetch=async(input,options)=>{
    const url=new URL(typeof input==="string"?input:input.url,location.href);
    const match=url.pathname.match(/^\/api\/accounts\/([a-f0-9]{32})(\/.*)$/);
    const account=match?.[1]||scopedAccount||"local",path=match?.[2]||url.pathname.replace(/^\/api/,"");
    const local=url.origin===location.origin&&url.pathname.startsWith("/api/");
    let placeholder=null,kind="",name="";
    if(local&&["/tasks/start","/analysis/start","/start","/lists/browse","/fetch"].includes(path)){
      let values={};try{values=JSON.parse(options?.body||"{}");}catch(_){}
      kind=values.kind||(path==="/analysis/start"?"analysis":path==="/lists/browse"||path==="/start"?"list":"review");name=taskNames[kind]||"資料抓取";
      const old=[...tasks.values()].reverse().find(run=>run._owner===account&&run._pending&&run.status==="queued"&&!run._claimed);
      placeholder=old||pendingRun(name,"正在送出查詢…",account);placeholder._claimed=true;const stored=tasks.get(account+":"+placeholder.id);if(stored)stored._claimed=true;
    }
    try{
      const response=await originalFetch(input,options);
      if(local&&(placeholder||["/history","/workbench","/analysis/cohorts"].includes(path)||path==="/tasks/detail")){
        const data=await response.clone().json();
        if(placeholder){
          if(response.ok){
            removePending(placeholder.id,account);
            const ids=data.run_ids||[data.task_id||data.id].filter(Boolean);
            for(const id of ids)publish({id,kind,name,status:"queued",source:path==="/tasks/start"?"bot":"run",progress:{done:0,total:null,unit:"項",stage:"任務已送出"}},account);
          }else publish({...placeholder,status:"failed",finished_at:new Date().toISOString(),progress:{done:0,total:null,stage:data.error||"操作未完成"}},account);
        }
        if(response.ok){for(const run of data.runs||[])if(run.kind!=="bot")publish({...run,source:"run"},account);for(const run of data.tasks||[])publish({...run,source:"bot"},account);if(path==="/tasks/detail")publish({...data,source:"bot"},account);}
      }
      return response;
    }catch(error){if(placeholder)publish({...placeholder,status:"failed",finished_at:new Date().toISOString(),progress:{done:0,total:null,stage:error.message||"連線未完成"}},account);throw error;}
  };
  function removePending(id,account=owner){tasks.delete(account+":"+id);if(embedded)parent.postMessage({type:"bot:progress-remove",account,id},location.origin);renderCard();}
  function receive(event,frames,account,readOnly=false){
    if(event.origin!==location.origin||!frames.some(frame=>frame?.contentWindow===event.source)||event.data?.account!==account)return;
    if(event.data.type==="bot:progress")for(const run of event.data.runs||[])publish({...run,read_only:readOnly},account);
    if(event.data.type==="bot:progress-remove")removePending(event.data.id,account);
    if(event.data.type==="bot:progress-dialog")renderCard();
  }
  document.addEventListener("DOMContentLoaded",()=>{
    mountCard();
    const observer=new MutationObserver(records=>{
      for(const {target} of records)if(target.tagName==="DIALOG"){
        const index=dialogStack.indexOf(target);if(index>=0)dialogStack.splice(index,1);if(target.open)dialogStack.push(target);
      }
      if(embedded)parent.postMessage({type:"bot:progress-dialog",account:owner},location.origin);else mountCard();
    });
    observer.observe(document.body,{attributes:true,subtree:true,attributeFilter:["open"]});setInterval(renderCard,1000);
  });
  return {create,show,visible,pending,context,publish,receive};
})();
