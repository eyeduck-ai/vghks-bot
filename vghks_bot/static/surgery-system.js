"use strict";
window.SurgerySystem = (() => {
  let data=null, initialized=false, revision=0, taskId="", signature="", rendered="";
  const expanded=new Map();
  const fields=()=>({doctor_card:$("#surgeryDoctor").value.trim(),start:$("#surgeryStart").value,end:$("#surgeryEnd").value,department:$("#surgeryDepartment").value.trim().toUpperCase()});
  function fill(value){for(const [key,id] of [["doctor_card","surgeryDoctor"],["start","surgeryStart"],["end","surgeryEnd"],["department","surgeryDepartment"]])$("#"+id).value=value[key]||((key==="doctor_card"&&data?.doctor_card)||"");$("#surgeryEnd").min=value.start;}
  function unloadBoard(){const frame=$("#operatingRoomFrame");frame.removeAttribute("src");frame.hidden=true;}
  function showBoard(){const frame=$("#operatingRoomFrame");if(!frame.hasAttribute("src"))frame.src=frame.dataset.src;frame.hidden=false;}
  function reset(){revision++;data=null;initialized=false;taskId=signature=rendered="";expanded.clear();fill({});$("#surgerySearch").value="";for(const id of ["surgeryResults","surgeryTasks"])$("#"+id).replaceChildren();$("#surgeryResultMeta").textContent="";unloadBoard();}
  function visibility(name){if(name==="operatingRoom")showBoard();else unloadBoard();}
  async function load(){
    const n=++revision,value=await api("/surgery/overview",{summary:1,...(taskId?{task_id:taskId}:{})});
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
    $("#surgeryAccount").textContent=`登入帳號 ${info()?.label||data.doctor_card} · 可查詢其他醫師卡號，依院方權限決定結果`;
    $("#surgeryConnection").textContent=root.read_only?"唯讀檢閱已保存排程":!work?.online?"離線檢閱；登入後可更新排程":"";
  }
  function renderTasks(){
    const tasks=data.tasks.filter(t=>inProgress.has(t.status)||["paused","failed","partial"].includes(t.status)||JobProgress.visible(t));
    $("#surgeryTasks").replaceChildren(...tasks.map(t=>{
      const row=node("div",undefined,"task-row"),description=node("div"),actions=node("div",undefined,"actions");
      row.dataset.transient=String(t.status==="completed");
      description.append(node("strong",t.name+" · "+(labels[t.status]||t.status)),JobProgress.create(t));
      if(t.message&&["failed","partial"].includes(t.status))description.append(node("p",t.message,"caption"));
      if(!root.read_only){
        if(inProgress.has(t.status))actions.append(btn("暫停",async()=>{await api("/tasks/stop",{id:t.id});await load();}));
        else if(["paused","failed","partial"].includes(t.status)){
          const resume=btn("續跑",async()=>{await api("/tasks/start",{resume:t.id});taskId="";await load();});
          resume.disabled=!work?.online;actions.append(resume);
        }
      }
      row.append(description,actions);return row;
    }));
  }
  function displayTime(value){const raw=String(value||"").trim(),m=raw.match(/^(\d{2}):?(\d{2})(?::?\d{2})?$/);return m&&+m[1]<24&&+m[2]<60?m[1]+":"+m[2]:raw;}
  function scheduleTime(row){
    if(row.time_status==="UNCONFIRMED")return row.schedule_time?row.schedule_time+"（時間未定）":"時間未定";
    const start=displayTime(row.start_time||row.schedule_time),end=displayTime(row.end_time);
    return [start,end].filter(Boolean).join("–")||"—";
  }
  function procedures(row){return (row.procedures||[]).map(p=>[p.position,p.code,p.name].filter(v=>v!==null&&v!==undefined&&String(v).trim()).join(" · ")).join("；")||row.procedure||"—";}
  function details(row){
    $("#dataTitle").textContent="手術排程 · "+(row.name||row.patient_mrn||"院方紀錄");
    $("#dataContent").replaceChildren(table(["欄位","內容"],[
      ["查詢醫師卡號",data.result.doctor_card],["取得時間",time(data.result.fetched_at)],
      ["姓名",row.name||"院方未提供"],["性別",row.patient_sex||"—"],["病歷號",row.patient_mrn||"—"],
      ["刀日",row.date||row.raw_date||"待確認"],["排程時間",scheduleTime(row)],
      ["病房",row.ward||"—"],["科別",row.department||"—"],["類別",row.category||"—"],
      ["術式",procedures(row)],["刀房",row.room||"—"],["麻醉",row.anesthesia||"—"],
      ["主刀醫師",[row.doctor_name,row.doctor_card].filter(Boolean).join(" · ")||"—"],
      ["就診案例號",row.case_no||"—"],
      ["手術申請單號",row.request_no||"—"],["列序號",row.sequence_no||"—"],
      ["案例類別代碼",row.case_type||"—"],["內部房間代碼",row.internal_room_code||"—"],
      ["診斷碼",(row.diagnosis_codes||[]).join("、")||"—"],["診斷文字",row.diagnosis_text||"—"],
      ...(row.note?[["待確認",row.note]]:[])
    ]));dialog("dataDialog");
  }
  function group(kind,label,all,rows){
    const box=node("details",undefined,"schedule-group schedule-"+kind),summary=node("summary"),count=rows.length;
    box.open=expanded.has(kind)?expanded.get(kind):kind!=="past"||!all.some(r=>r.group==="upcoming");
    box.addEventListener("toggle",()=>expanded.set(kind,box.open));
    summary.append(node("strong",label),node("span",`${count} 筆`,"badge"));box.append(summary);
    rows.sort((a,b)=>{
      const dateOrder=(a.date||a.raw_date||"").localeCompare(b.date||b.raw_date||"");
      const timeOrder=(r)=>r.time_status==="UNCONFIRMED"?"99:99":displayTime(r.start_time||r.schedule_time);
      return (kind==="past"?-dateOrder:dateOrder)||timeOrder(a).localeCompare(timeOrder(b))||a.row_id-b.row_id;
    });
    if(!count){box.append(empty("沒有符合的排程。"));return box;}
    box.append(table(["日期","時間","姓名","病歷號","病房","科別","術式","刀房","麻醉","類別","主刀醫師",""],rows.map(r=>{
      const procedure=node("span",r.procedure||"—","schedule-procedure"),physician=node("span",r.doctor_name||r.doctor_card||"—"),detail=btn("查看排程",()=>details(r));
      physician.title=[r.doctor_name,r.doctor_card].filter(Boolean).join(" · ");setIcon(detail,"calendar","查看排程 "+(r.patient_mrn||r.case_no||r.row_id));
      if(r.note)procedure.append(node("small",r.note,"schedule-warning"));
      return [r.date||r.raw_date||"待確認",scheduleTime(r),r.name||"—",r.patient_mrn||"—",r.ward||"—",r.department||"—",procedure,r.room||"—",r.anesthesia||"—",r.category||"—",physician,detail];
    })));
    return box;
  }
  function renderResults(){
    const result=data.result,container=$("#surgeryResults");
    $("#surgerySearchBar").hidden=!result;
    $("#surgeryResultMeta").textContent=result?`醫師 ${result.doctor_card} · ${result.start} ～ ${result.end} · 科別 ${result.department} · ${result.rows.length} 筆 · 取得 ${time(result.fetched_at)}`:"尚未查詢手術排程";
    if(!result){container.replaceChildren(empty("選擇日期後按「查詢／更新」。已保存的結果可離線檢閱。"));return;}
    const query=$("#surgerySearch").value.trim().toLocaleLowerCase(),all=result.rows;
    const rows=all.filter(r=>!query||[r.name,r.patient_mrn,r.ward,r.department,r.category,r.procedure,procedures(r),r.room,r.anesthesia,r.doctor_name,r.doctor_card,r.note,r.request_no].join(" ").toLocaleLowerCase().includes(query));
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
    const values=fields(),pending=JobProgress.pending("#surgeryTasks","手術排程","正在送出排程查詢…");
    try{const result=await api("/tasks/start",{kind:"surgery_schedule",...values});
      taskId="";signature="";await load();say("手術排程查詢已排入任務。");return result;}
    finally{pending?.remove();}
  }
  async function poll(){
    if(!data)return;
    controls();
    const value=JSON.stringify(work.tasks.filter(t=>t.kind==="surgery_schedule").map(t=>[t.id,t.status,t.updated_at]));
    const dateParts=Object.fromEntries(new Intl.DateTimeFormat("en",{timeZone:"Asia/Taipei",year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(new Date()).map(p=>[p.type,p.value]));
    const day=[dateParts.year,dateParts.month,dateParts.day].join("-");
    if(value!==signature||day!==data.today){signature=value;await load();}
  }
  $("#surgeryForm").addEventListener("submit",e=>{e.preventDefault();act(start,e.submitter).finally(controls);});
  $("#surgeryStart").addEventListener("change",()=>{$("#surgeryEnd").min=$("#surgeryStart").value;});
  $("#surgeryDefault").addEventListener("click",()=>act(async()=>{const value=await api("/surgery/overview",{});$("#surgeryStart").value=value.defaults.start;$("#surgeryEnd").value=value.defaults.end;$("#surgeryEnd").min=value.defaults.start;}));
  $("#surgerySearch").addEventListener("input",()=>{if(data)renderResults();});
  return {open,reset,poll,visibility};
})();
