"use strict";
window.SurgerySystem = (() => {
  let data=null, initialized=false, revision=0, taskId="", signature="", rendered="";
  const expanded=new Map();
  const fields=()=>({start:$("#surgeryStart").value,end:$("#surgeryEnd").value,department:$("#surgeryDepartment").value.trim().toUpperCase()});
  function fill(value){for(const [key,id] of [["start","surgeryStart"],["end","surgeryEnd"],["department","surgeryDepartment"]])$("#"+id).value=value[key]||"";$("#surgeryEnd").min=value.start;}
  function unloadBoard(){const frame=$("#operatingRoomFrame");frame.removeAttribute("src");frame.hidden=true;$("#operatingRoomClose").hidden=true;$("#operatingRoomEmbed").textContent="在此頁開啟";$("#operatingRoomPlaceholder").hidden=false;}
  function reset(){revision++;data=null;initialized=false;taskId=signature=rendered="";expanded.clear();fill({});$("#surgerySearch").value="";for(const id of ["surgeryResults","surgeryTasks"])$("#"+id).replaceChildren();$("#surgeryResultMeta").textContent="";unloadBoard();}
  function visibility(name){if(name!=="operatingRoom")unloadBoard();}
  async function load(){
    const n=++revision,value=await api("/surgery/overview",taskId?{task_id:taskId}:{});
    if(n!==revision)return;
    data=value;
    if(!initialized){fill(taskId&&data.result?data.result:data.query);initialized=true;}
    render();
  }
  async function open(id=""){taskId=id;initialized=false;rendered="";expanded.clear();await load();}
  function controls(){
    if(!data)return;
    const busy=data.tasks.some(t=>inProgress.has(t.status));
    $("#surgeryQuery").disabled=!!root.read_only||!work?.online||busy;
    $("#surgeryAccount").textContent=`${info()?.label||data.doctor_card} · 帳號 ${data.doctor_card}`;
    $("#surgeryConnection").textContent=root.read_only?"唯讀檢閱已保存排程":!work?.online?"離線檢閱；登入後可更新排程":"";
  }
  function renderTasks(){
    const tasks=data.tasks.filter(t=>inProgress.has(t.status)||["paused","failed","partial"].includes(t.status)||JobProgress.visible(t));
    $("#surgeryTasks").replaceChildren(...tasks.map(t=>{
      const row=node("div",undefined,"task-row"),description=node("div"),actions=node("div",undefined,"actions");
      row.dataset.transient=String(t.status==="completed");
      description.append(node("strong",t.name+" · "+(labels[t.status]||t.status)),JobProgress.create(t));
      if(t.message&&t.status!=="completed")description.append(node("p",t.message,"caption"));
      if(!root.read_only){
        if(inProgress.has(t.status))actions.append(btn("暫停",async()=>{await api("/tasks/stop",{id:t.id});await load();}));
        else if(["paused","failed","partial"].includes(t.status)){
          const resume=btn("續跑",async()=>{await api("/tasks/start",{resume:t.id});taskId="";await load();});
          resume.disabled=!work?.online;actions.append(resume);
        }
      }
      if(t.status==="failed"||t.status==="partial")actions.append(btn("查看診斷",()=>showDiagnostics({task_id:t.id})));
      row.append(description,actions);return row;
    }));
  }
  function displayTime(value){const raw=String(value||"").trim(),m=raw.match(/^(\d{2}):?(\d{2})(?::?\d{2})?$/);return m&&+m[1]<24&&+m[2]<60?m[1]+":"+m[2]:raw;}
  function details(row){
    $("#dataTitle").textContent="手術排程 · "+(row.name||row.patient_mrn||"院方紀錄");
    const body=$("#dataContent"),extra=node("details");
    extra.append(node("summary","院方原始欄位"),node("pre",JSON.stringify({...row,group:undefined,row_id:undefined,name:undefined,date:undefined,note:undefined},null,2)));
    body.replaceChildren(table(["欄位","內容"],[
      ["查詢帳號",data.result.doctor_card],["取得時間",time(data.result.fetched_at)],
      ["姓名",row.name||"院方未提供"],["病歷號",row.patient_mrn||"—"],["刀日",row.date||row.raw_date||"待確認"],
      ["時間",[displayTime(row.start_time),displayTime(row.end_time)].filter(Boolean).join(" ～ ")||"未提供"],
      ["術式",row.procedure||"—"],["刀房",row.room||"—"],["醫師",[row.doctor_name,row.doctor_card].filter(Boolean).join(" · ")||"未提供"],
      ["院方狀態",row.status||"未提供"],["案件編號",row.case_no||"未提供"],
      ...(row.note?[["待確認",row.note]]:[])
    ]),extra);dialog("dataDialog");
  }
  function group(kind,label,all,rows){
    const box=node("details",undefined,"schedule-group schedule-"+kind),summary=node("summary"),count=rows.length;
    box.open=expanded.has(kind)?expanded.get(kind):kind!=="past"||!all.some(r=>r.group==="upcoming");
    box.addEventListener("toggle",()=>expanded.set(kind,box.open));
    summary.append(node("strong",label),node("span",`${count} 筆`,"badge"));box.append(summary);
    rows.sort((a,b)=>{
      const dateOrder=(a.date||a.raw_date||"").localeCompare(b.date||b.raw_date||"");
      return (kind==="past"?-dateOrder:dateOrder)||displayTime(a.start_time).localeCompare(displayTime(b.start_time))||a.row_id-b.row_id;
    });
    if(!count){box.append(empty("沒有符合的排程。"));return box;}
    box.append(table(["日期","時間","姓名","病歷號","術式","刀房","醫師","院方狀態",""],rows.map(r=>{
      const procedure=node("span",r.procedure||"—","schedule-procedure"),physician=node("span",r.doctor_name||r.doctor_card||"—"),detail=btn("查看排程",()=>details(r));
      physician.title=[r.doctor_name,r.doctor_card].filter(Boolean).join(" · ");setIcon(detail,"diagnostic","查看排程 "+(r.patient_mrn||r.case_no||r.row_id));
      if(r.note)procedure.append(node("small",r.note,"schedule-warning"));
      return [r.date||r.raw_date||"待確認",[displayTime(r.start_time),displayTime(r.end_time)].filter(Boolean).join("–")||"—",r.name||"—",r.patient_mrn||"—",procedure,r.room||"—",physician,r.status||"—",detail];
    })));
    return box;
  }
  function renderResults(){
    const result=data.result,container=$("#surgeryResults");
    $("#surgerySearchBar").hidden=!result;
    $("#surgeryResultMeta").textContent=result?`${result.start} ～ ${result.end} · 科別 ${result.department} · ${result.rows.length} 筆 · 取得 ${time(result.fetched_at)}`:"尚未查詢手術排程";
    if(!result){container.replaceChildren(empty("選擇日期後按「查詢／更新」。已保存的結果可離線檢閱。"));return;}
    const query=$("#surgerySearch").value.trim().toLocaleLowerCase(),all=result.rows;
    const rows=all.filter(r=>!query||[r.name,r.patient_mrn,r.procedure,r.room,r.doctor_name,r.doctor_card,r.status,r.note].join(" ").toLocaleLowerCase().includes(query));
    const groups=[["upcoming","今天及未來"],["past","過去"]];if(all.some(r=>r.group==="unconfirmed"))groups.push(["unconfirmed","待確認"]);
    container.replaceChildren(...groups.map(([key,label])=>group(key,label,all,rows.filter(r=>r.group===key))));
    $("#surgerySearchCount").textContent=query?`${rows.length} / ${all.length} 筆`:"";
  }
  function render(){
    controls();renderTasks();
    const next=JSON.stringify([data.result?.snapshot_id,data.result?.fetched_at,data.today]);
    if(next!==rendered){rendered=next;renderResults();}
  }
  async function start(){
    const result=await api("/tasks/start",{kind:"surgery_schedule",...fields()});
    taskId="";signature="";await load();say("手術排程查詢已排入任務。");return result;
  }
  async function poll(){
    if(!data)return;
    controls();
    const value=JSON.stringify(work.tasks.filter(t=>t.kind==="surgery_schedule").map(t=>[t.id,t.status,t.updated_at]));
    const dateParts=Object.fromEntries(new Intl.DateTimeFormat("en",{timeZone:"Asia/Taipei",year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(new Date()).map(p=>[p.type,p.value]));
    const day=[dateParts.year,dateParts.month,dateParts.day].join("-");
    if(value!==signature||day!==data.today){signature=value;await load();}
  }
  $("#surgeryForm").addEventListener("submit",e=>{e.preventDefault();act(start).finally(controls);});
  $("#surgeryStart").addEventListener("change",()=>{$("#surgeryEnd").min=$("#surgeryStart").value;});
  $("#surgeryDefault").addEventListener("click",()=>act(async()=>{const value=await api("/surgery/overview",{});fill(value.defaults);}));
  $("#surgerySearch").addEventListener("input",()=>{if(data)renderResults();});
  $("#operatingRoomEmbed").addEventListener("click",()=>{
    const frame=$("#operatingRoomFrame");frame.src=$("#operatingRoomLink").href;frame.hidden=false;
    $("#operatingRoomClose").hidden=false;$("#operatingRoomPlaceholder").hidden=true;$("#operatingRoomEmbed").textContent="重新載入";
  });
  $("#operatingRoomClose").addEventListener("click",unloadBoard);
  return {open,reset,poll,visibility};
})();
