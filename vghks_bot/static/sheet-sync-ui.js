"use strict";
// Static controls only; every remote value is rendered as text.
$("#toolSourcePanel").insertAdjacentHTML("beforeend",'<section id="sheetSyncPanel" class="feature-controls" aria-labelledby="sheetSyncTitle" hidden><div class="section-heading"><h2 id="sheetSyncTitle">當日門診 → 線上刀表</h2><div class="actions"><button id="sheetSyncSettings" type="button" aria-haspopup="dialog">排程設定</button><button id="sheetSyncRun" type="button" class="primary">立即同步</button><button id="sheetSyncRefresh" type="button">重新整理</button></div></div><p id="sheetSyncStatus" class="caption" role="status"></p><details id="sheetSyncPendingDetails"><summary id="sheetSyncPendingTitle">待確認項目</summary><div id="sheetSyncPending"></div></details></section>');
document.body.insertAdjacentHTML("beforeend",'<dialog id="sheetSyncDialog" aria-labelledby="sheetSyncDialogTitle"><form id="sheetSyncForm"><div class="dialog-heading"><h2 id="sheetSyncDialogTitle">刀表更新排程</h2><button type="button" data-close="sheetSyncDialog" aria-label="關閉刀表更新排程">×</button></div><label class="check"><input id="sheetSyncEnabled" type="checkbox">定期同步我的當日門診 SOAP</label><label>間隔（分鐘）<input id="sheetSyncInterval" type="number" min="5" max="1440" value="60" required></label><p class="caption">只在主程式開啟、此帳號在線時執行。來源固定為台北時間當天的我的門診；有衝突或缺少必要資料時保留待確認。</p><p class="caption">日曆同步由獨立監控程式執行。</p><div class="actions"><button id="sheetSyncSave" type="submit" class="primary">保存排程</button></div></form></dialog>');
window.SheetSyncUI=(()=>{
 let current=null,owner="",visible=false,reading=null,lastRead=0;
 const call=(path,values={})=>api("/sheet-sync/"+path,values);
 function render(value){
  current=value;const settings=value.settings||{},last=value.last_result;
  $("#sheetSyncStatus").textContent=[value.running?"正在同步":settings.enabled?"排程已啟用":"排程未啟用",
   value.online===false?"帳號離線":"",settings.enabled?"每 "+(settings.interval_minutes||60)+" 分鐘":"",
   settings.last_run_at?"上次 "+time(settings.last_run_at):"",
   settings.next_run_at?"下次 "+time(settings.next_run_at):"",value.scheduler_error?.message||last?.message||""].filter(Boolean).join(" · ");
  $("#sheetSyncRun").disabled=!!root.read_only||!!value.running||value.online===false;
  $("#sheetSyncSettings").disabled=!!root.read_only;
  const pending=value.pending||[],deferred=value.deferred||[];$("#sheetSyncPendingTitle").textContent="待確認項目 · "+(pending.length+deferred.length)+" 筆";
  const area=$("#sheetSyncPending");area.replaceChildren();
  for(const entry of deferred){
   const row=node("section",undefined,"tracking-panel"),actions=node("div",undefined,"actions");
   row.append(node("strong","已保存的 SOAP 更新批次"),node("p",entry.message||"Google 目前無法使用；來源與候選已保存。","caption"));
   const retry=btn("重新連線並核對",async()=>{retry.disabled=true;try{const result=await call("retry",{id:entry.id});await load(true);say(result.message||"已重新讀取來源與刀表");}finally{retry.disabled=!!root.read_only;}});
   retry.disabled=!!root.read_only||!!value.running;actions.append(retry);
   if(entry.cohort_id)actions.append(btn("開啟已保存候選",()=>openModule(entry.cohort_id,"surgery")));
   row.append(actions);area.append(row);
  }
  for(const entry of pending){
   const row=node("section",undefined,"tracking-panel"),actions=node("div",undefined,"actions");
   row.append(node("strong",(entry.name||"姓名未提供")+" · "+entry.mrn),node("p",entry.reason||"請核對來源與刀表內容。","caption"));
   if(entry.cohort_id)actions.append(btn("開啟候選",()=>openModule(entry.cohort_id,"surgery",{focusMrn:entry.mrn})));
   const dismiss=btn("移除待確認",async()=>{await call("review",{id:entry.id,action:"dismiss"});await load(true);},"quiet");
   dismiss.disabled=!!root.read_only;actions.append(dismiss);row.append(actions);area.append(row);
  }
  if(!pending.length&&!deferred.length)area.append(node("p","目前沒有待確認項目。","caption"));
 }
 async function load(force=false){
  if(!account||!visible)return;
  if(reading)return reading;
  if(!force&&Date.now()-lastRead<5000&&owner===account)return;
  const key=account;owner=key;
  const read=call("status").then(value=>{if(account===key&&visible){lastRead=Date.now();render(value);}return value;})
   .finally(()=>{if(reading===read)reading=null;});
  reading=read;return read;
 }
 function visibility(name,kind){
  const next=["tool","module"].includes(name)&&kind==="surgery",changed=next&&!visible||owner!==account;
  visible=next;$("#sheetSyncPanel").hidden=!next;
  if(next&&changed)void act(()=>load(true));
 }
 $("#sheetSyncSettings").addEventListener("click",()=>act(async()=>{
  const settings=await call("settings");
  $("#sheetSyncEnabled").checked=!!(settings.settings||settings).enabled;
  $("#sheetSyncInterval").value=(settings.settings||settings).interval_minutes||60;dialog("sheetSyncDialog");
 }));
 $("#sheetSyncDialog").querySelector("[data-close]").addEventListener("click",()=>$("#sheetSyncDialog").close());
 $("#sheetSyncForm").addEventListener("submit",event=>{event.preventDefault();act(async()=>{
  if(!$("#sheetSyncForm").reportValidity())return;
  await call("settings",{enabled:$("#sheetSyncEnabled").checked,interval_minutes:Number($("#sheetSyncInterval").value)});
  $("#sheetSyncDialog").close();await load(true);say("刀表更新排程已保存");
 });});
 $("#sheetSyncRun").addEventListener("click",()=>act(async()=>{await call("run");await load(true);say("刀表同步已排入執行");}));
 $("#sheetSyncRefresh").addEventListener("click",()=>act(()=>load(true)));
 return {visibility,poll:()=>visible?load():Promise.resolve(),reset(){current=null;owner="";visible=false;lastRead=0;$("#sheetSyncPanel").hidden=true;}};
})();
