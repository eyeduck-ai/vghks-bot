"use strict";
window.JobProgress = (() => {
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
    const bar=document.createElement("progress"),text=document.createElement("span");bar.setAttribute("aria-label",(run.name||"資料抓取")+"進度");
    if(p.total!=null){bar.max=Math.max(1,p.total);bar.value=p.total?Math.min(p.done,p.total):(run.status==="completed"?1:0);}
    else if(!["queued","running","cancelling"].includes(run.status)){bar.max=1;bar.value=0;}
    text.textContent=(p.total==null?`已處理 ${p.done||0} ${p.unit||"項"} · 確認總量中`:`${p.done} / ${p.total} ${p.unit||"項"}`)+(p.failed?` · ${p.failed} 項未完成`:"");box.append(bar,text);
    if(p.stage){const detail=document.createElement("small");detail.textContent=p.stage;box.append(detail);}
    if(run.status==="completed")setTimeout(()=>{box.hidden=true;const card=box.closest('[data-transient="true"]');if(card)card.hidden=true;},remaining(run));
    return box;
  }
  function show(target,run){if(typeof target==="string")target=document.querySelector(target);if(target)target.replaceChildren(create(run));}
  return {create,show,visible};
})();
