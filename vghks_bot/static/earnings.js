"use strict";
window.Earnings = (()=>{
  let data=null,selected=new Set(),opened="",signature="",revision=0,detailRevision=0,scopeRevision=0,optionsTask="",catalogDialog=false,offset=0,view="";
  let exportController=null,exportUrls=[];
  const call=(path,body={},options)=>api("/earnings/"+path,body,options),names={performance:"醫療績點明細",payroll:"專勤工作獎金明細"};
  const active=()=>data?.view==="active";
  function syncActions(){
    const fetching=!active()||root.read_only||!work.online||data.tasks.some(task=>inProgress.has(task.status));
    for(const id of ["earningsCaptureAll","earningsOptions"])$("#"+id).disabled=fetching;
    $("#earningsMonitorSettings").disabled=!active()||root.read_only||!data.monitor;
    $("#earningsCredentials").disabled=root.read_only;
    $("#earningsDelete").disabled=root.read_only||!selected.size;
    $("#earningsExportCsv").disabled=$("#earningsExportJson").disabled=!!exportController||!selected.size;
  }
  const runAction=callback=>act(callback).finally(syncActions);
  function clearExports(){
    $("#earningsExportDownloads").replaceChildren();
    for(const url of exportUrls)URL.revokeObjectURL(url);
    exportUrls=[];$("#earningsExportStatus").textContent="";$("#earningsExportPanel").hidden=true;
  }
  function resetReports(next=""){
    view=next;offset=0;data=null;selected.clear();opened=signature=optionsTask="";revision++;detailRevision++;scopeRevision++;catalogDialog=false;
    exportController?.abort();exportController=null;clearExports();
    for(const id of ["earningsTasks","earningsReports","earningsDetail","earningsPager"])$("#"+id)?.replaceChildren();
    for(const id of ["earningsSummary","earningsMonitorStatus","earningsMonitorSchedule"])$("#"+id).textContent="";
    syncActions();$("#earningsSelectAll").disabled=true;
    $("#earningsSelectAll").checked=$("#earningsSelectAll").indeterminate=false;
    $("#earningsKind").value="";$("#earningsMonth").replaceChildren(new Option("全部期別",""));$("#earningsMonth").value="";
  }
  function reset(){resetReports();$("#earningsViewField").hidden=true;$("#earningsCredentialState").textContent="";}
  async function open(){
    const n=++revision,value=await call("overview",{...(view?{view}:{}),kind:$("#earningsKind").value,month:$("#earningsMonth").value,summary:1,limit:40,offset});
    if(n!==revision)return;
    if(view==="archive"&&!value.has_archive){resetReports("active");return open();}
    data=value;view=value.view;
    $("#earningsViewField").hidden=!value.has_archive;$("#earningsView").value=view;
    let pager=$("#earningsPager");if(!pager){pager=node("div",undefined,"pagination");pager.id="earningsPager";$("#earningsReports").after(pager);}
    const previous=btn("上一頁",()=>{offset=Math.max(0,offset-40);return open();}),next=btn("下一頁",()=>{offset+=40;return open();});
    previous.disabled=offset===0;next.disabled=offset+40>=data.total;
    pager.replaceChildren(previous,node("span",`${data.total?offset+1:0}–${Math.min(offset+40,data.total)} / ${data.total} 份`),next);
    const month=$("#earningsMonth").value;$("#earningsMonth").replaceChildren(new Option("全部期別",""),...data.months.map(m=>new Option(m,m)));$("#earningsMonth").value=month;
    render();renderMonitor();if(!opened)$("#earningsDetail").replaceChildren(empty("選擇已保存報表檢閱內容。"));
  }
  function render(){Choices.sync();syncActions();const rows=data.reports,archive=$("#earningsReports");$("#earningsSummary").textContent=`${rows.length} 份報表 · 勾選 ${selected.size} 份`;$("#earningsCredentialState").textContent=data.credentials.configured?(data.credentials.remembered?"已記住薪資登入資訊":"本次登入資訊已設定"):"尚未設定薪資登入資訊";$("#earningsSelectAll").disabled=!rows.length;const checked=rows.filter(r=>selected.has(r.id)).length;$("#earningsSelectAll").checked=!!rows.length&&checked===rows.length;$("#earningsSelectAll").indeterminate=checked>0&&checked<rows.length;
    archive.replaceChildren(table(["選取","期別／報表","版本"],rows.map(r=>{const label=node("span");label.append(btn(r.month||r.label||r.period,()=>detail(r.id),"quiet"),node("div",names[r.kind],"caption"));return [check("選取 "+names[r.kind]+" "+r.period,selected.has(r.id),v=>{if(v)selected.add(r.id);else selected.delete(r.id);render();}),label,r.version_count];})));if(!rows.length)archive.append(empty("尚無符合的已保存報表。"));
    $("#earningsTasks").replaceChildren(...data.tasks.filter(JobProgress.visible).map(t=>{const box=node("div",undefined,"task-row"),desc=node("div"),controls=node("div",undefined,"actions");box.dataset.transient=String(t.status==="completed");desc.append(node("strong",(t.automatic?"自動監控 · ":"")+t.name+" · "+(labels[t.status]||t.status)),JobProgress.create(t));if(!root.read_only){if(inProgress.has(t.status))controls.append(btn("暫停",async()=>{await api("/tasks/stop",{id:t.id});await open();}));else if(["paused","partial","failed"].includes(t.status))controls.append(btn("續跑",async()=>{await api("/tasks/start",{resume:t.id});await open();}));}if(t.status==="partial"||t.status==="failed")controls.append(btn("未完成項目",async()=>{const task=await api("/tasks/detail?id="+t.id);$("#dataTitle").textContent="未完成項目";$("#dataContent").replaceChildren(node("p",task.message),table(["報表","期別","原因"],task.items.filter(i=>i.status==="error").map(i=>[names[i.kind]||i.kind,i.period||"期別選單",i.message])));dialog("dataDialog");}));box.append(desc,controls);return box;}));
  }
  function renderMonitor(){
    const m=data.monitor;
    if(!active()||!m){$("#earningsMonitorStatus").textContent="";$("#earningsMonitorSchedule").textContent="";return;}
    $("#earningsMonitorStatus").textContent=(m.enabled?`每 ${m.interval_value||m.hours} ${m.interval_unit==="days"?"天":"小時"}更新`:"持續監控已關閉")+(m.last_attempt?" · 上次 "+time(m.last_attempt)+" · "+(labels[m.last_status]||m.last_status):" · 尚未檢查");
    $("#earningsMonitorSchedule").textContent=!m.configured?"先設定薪資登入資訊即可開始監控。":m.enabled?(m.due?"已到期，登入且沒有待續跑任務時自動排入佇列。":"下次更新 "+time(m.next_check))+" EXE 關閉期間不執行；同一期別內容有變更時保留版本。":"可隨時按「立即更新」手動更新。";
  }
  function monitorSettings(){
    if(!active()||!data.monitor||root.read_only)return;
    const scope=scopeRevision,m=data.monitor,form=node("form"),enabled=node("input"),a=node("label",undefined,"check"),frequency=MonitorUI.fields(m);enabled.type="checkbox";enabled.checked=m.enabled;a.append(enabled,node("span","開啟持續監控"));const save=node("button","保存","primary");save.type="submit";form.append(a,frequency.element,save);
    form.addEventListener("submit",e=>{e.preventDefault();act(async()=>{
      if(scope!==scopeRevision)return;
      await call("monitor",{enabled:enabled.checked,...frequency.value()});
      if(scope!==scopeRevision)return;
      if($("#dataContent").contains(form))$("#dataDialog").close();
      await open();
    });});
    $("#dataTitle").textContent="薪資監控設定";$("#dataContent").replaceChildren(form);dialog("dataDialog");
  }

  function credentials(){
    if(root.read_only)return;
    const scope=epoch,form=node("form"),id=node("input"),password=node("input"),remember=node("input"),a=node("label","身分證字號"),b=node("label","薪資系統密碼"),c=node("label",undefined,"check");id.required=true;id.maxLength=32;id.autocomplete="off";password.required=true;password.type="password";password.maxLength=256;password.autocomplete="off";remember.type="checkbox";remember.checked=true;a.append(id);b.append(password);c.append(remember,node("span","記住登入資訊（隨此帳號資料庫攜帶）"));
    const change=async(path,values)=>{
      if(scope!==epoch)return;
      await call(path,values);
      id.value=password.value="";
      if(scope!==epoch)return;
      if($("#dataContent").contains(form))$("#dataDialog").close();
      resetReports();
      await open();
    };
    const submit=node("button","保存","primary");submit.type="submit";form.append(a,b,c,submit,btn("忘記薪資登入資訊",()=>change("forget"),"quiet"));
    form.addEventListener("submit",e=>{e.preventDefault();act(()=>change("credentials",{national_id:id.value,password:password.value,remember:remember.checked}));});
    $("#dataTitle").textContent="薪資登入設定";$("#dataContent").replaceChildren(form,node("p","此為 MIS 薪資系統的第二次登入資訊。","caption"));dialog("dataDialog");$("#dataDialog").addEventListener("close",()=>{id.value=password.value="";},{once:true});
  }
  async function start(values){
    if(!active()||root.read_only||!work.online)return;
    if(!data.credentials.configured){credentials();return;}
    const scope=scopeRevision,pending=JobProgress.pending("#earningsTasks","薪資業績","正在送出查詢任務…");
    try{
      const t=await api("/tasks/start",values);if(scope!==scopeRevision)return;
      if(values.kind==="earnings_options")optionsTask=t.task_id;
      signature="";await open();
    }finally{pending?.remove();}
  }
  function monthPicker(){const body=node("form"),list=node("div",undefined,"earnings-months"),choices=[];for(const catalog of data.catalogs){list.append(node("h3",names[catalog.id]),node("p","期別取得 "+time(catalog.fetched_at),"caption"));for(const p of catalog.periods){const row=node("label",undefined,"check"),saved=p.saved,input=check(names[catalog.id]+" "+p.label,!saved,()=>{});row.append(input,node("span",p.label+(saved?" · 已保存":"")));list.append(row);choices.push({input,kind:catalog.id,period:p.value});}}const force=node("input"),label=node("label",undefined,"check");force.type="checkbox";label.append(force,node("span","更新已保存報表，內容變更時另存版本"));const save=node("button","保存勾選期別","primary");save.type="submit";body.append(list,label,save);body.addEventListener("submit",e=>{e.preventDefault();act(async()=>{await start({kind:"earnings_capture",reports:choices.filter(c=>c.input.checked).map(c=>({kind:c.kind,period:c.period})),force:force.checked});$("#dataDialog").close();});});$("#dataTitle").textContent="院方目前可取得期別";$("#dataContent").replaceChildren(body);catalogDialog=true;dialog("dataDialog");$("#dataDialog").addEventListener("close",()=>catalogDialog=false,{once:true});}
  async function detail(id,version){if(!data)return;const n=++detailRevision;opened=id;const value=await call("detail",{id,version,view});if(n!==detailRevision||opened!==id)return;const {report,version:v,versions}=value,container=$("#earningsDetail"),select=node("select"),label=node("label","內容版本");select.append(...versions.map(i=>new Option(time(i.fetched_at)+(i.id===report.latest?" · 最新":""),i.id)));select.value=v.id;select.addEventListener("change",()=>act(()=>detail(id,select.value)));label.append(select);container.replaceChildren(node("h2","院內期別 "+(report.label||report.period)+" · "+names[report.kind]),node("p","院內期別 "+report.period+" · 取得 "+time(v.fetched_at),"caption"),label);container.append(node("p","最後核對 "+time(report.fetched_at)+" · "+value.fetches.length+" 次抓取","caption"));const fetchHistory=node("details");fetchHistory.append(node("summary","抓取紀錄"),table(["時間","內容"],value.fetches.map(f=>[time(f.fetched_at),f.changed?"內容變更":"相同或首次保存"])));container.append(fetchHistory);for(const note of v.payload.notes)container.append(node("p",note,"caption"));for(const t of v.payload.tables){const wrap=node("div",undefined,"table-wrap"),tab=node("table",undefined,"earnings-table"),body=node("tbody");tab.setAttribute("aria-label","報表表格 "+(t.index+1));const rows=new Map();for(const c of t.cells){if(!rows.has(c.row))rows.set(c.row,node("tr"));const cell=node(c.header?"th":"td",c.raw);if(c.header)cell.scope=c.rowspan>1?"row":"col";cell.rowSpan=c.rowspan;cell.colSpan=c.colspan;if(c.number!==null){cell.dataset.number=c.number;cell.title="可解析數值："+c.number+(c.unit?" "+c.unit:"");}rows.get(c.row).append(cell);}for(const r of rows.values())body.append(r);tab.append(body);wrap.append(tab);container.append(node("h3",t.id||"表格 "+(t.index+1)),wrap);}const raw=node("details");raw.append(node("summary","原始報表文字"),node("pre",v.payload.text));container.append(raw);}
  async function download(format){
    if(!data||!selected.size||exportController)return;
    const scope=scopeRevision,ids=[...selected],controller=new AbortController();
    exportController=controller;clearExports();syncActions();
    $("#earningsExportPanel").hidden=false;
    $("#earningsExportStatus").textContent=`正在準備 ${ids.length} 份報表的 ${format.toUpperCase()} 檔案…`;
    let timedOut=false;
    const timeout=setTimeout(()=>{timedOut=true;controller.abort();},60000);
    try{
      const out=await call("export",{ids,format,view},{signal:controller.signal});
      if(scope!==scopeRevision||controller.signal.aborted)return;
      for(const file of out.files){
        const url=URL.createObjectURL(new Blob([file.content],{type:file.mime})),a=node("a",`下載 ${names[file.kind]} ${format.toUpperCase()}`);
        exportUrls.push(url);a.href=url;a.download=file.filename;$("#earningsExportDownloads").append(a);
      }
      $("#earningsExportStatus").textContent=`已產生 ${out.files.length} 個 ${format.toUpperCase()} 檔案（${ids.length} 份報表），請分別點選下方連結下載。`;
    }catch(error){
      if(scope!==scopeRevision)return;
      clearExports();$("#earningsExportPanel").hidden=false;
      const message=timedOut?"匯出逾時，請重試。":error.message||"匯出未完成，請重試。";
      $("#earningsExportStatus").textContent=message;
      throw new Error(message);
    }finally{
      clearTimeout(timeout);
      if(exportController===controller){exportController=null;syncActions();}
    }
  }
  async function removeSelected(){
    if(!data||root.read_only||!selected.size)return;
    const scope=scopeRevision,ids=[...selected];
    if(!await confirmDelete(`刪除勾選的 ${ids.length} 份${view==="archive"?"封存":""}薪資報表及所有版本？`)||scope!==scopeRevision)return;
    await call("delete",{ids,view});
    if(scope!==scopeRevision)return;
    if(ids.includes(opened)){opened="";detailRevision++;}
    for(const id of ids)selected.delete(id);
    await open();
  }
  async function poll(){
    if(!active())return;
    const scope=scopeRevision,s=JSON.stringify(work.tasks.filter(t=>t.kind.startsWith("earnings_")).map(t=>[t.id,t.status,t.revision??t.updated_at]));
    if(s!==signature){await open();if(scope!==scopeRevision)return;signature=s;}
    if(!optionsTask)return;
    const key=optionsTask,t=await api("/tasks/detail?summary=1&id="+key);
    if(scope!==scopeRevision||key!==optionsTask||inProgress.has(t.status))return;
    optionsTask="";
    if(t.status==="completed"||t.status==="partial"){await open();if(scope===scopeRevision&&!catalogDialog)monthPicker();}
    else say(t.message,true);
  }
  $("#earningsMonitorSettings").addEventListener("click",()=>monitorSettings());$("#earningsCredentials").addEventListener("click",()=>credentials());
  $("#earningsCaptureAll").addEventListener("click",()=>runAction(()=>start({kind:"earnings_capture",all_available:true})));$("#earningsOptions").addEventListener("click",()=>runAction(()=>start({kind:"earnings_options"})));
  $("#earningsView").addEventListener("change",()=>act(async()=>{const next=$("#earningsView").value;if(next!==view){resetReports(next);await open();}}));
  for(const id of ["earningsKind","earningsMonth"])$("#"+id).addEventListener("change",()=>{offset=0;act(open);});
  $("#earningsSelectAll").addEventListener("change",e=>{if(!data)return;for(const r of data.reports){if(e.target.checked)selected.add(r.id);else selected.delete(r.id);}render();});
  $("#earningsExportCsv").addEventListener("click",()=>runAction(()=>download("csv")));$("#earningsExportJson").addEventListener("click",()=>runAction(()=>download("json")));$("#earningsDelete").addEventListener("click",()=>runAction(removeSelected));
  return {reset,open,poll};
})();
