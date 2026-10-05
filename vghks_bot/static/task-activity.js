"use strict";

window.TaskActivityUI=(()=>{
 const childKinds=new Set(["history","numeric","registrations"]);
 const expanded=new Set(),closedMatches=new Set();
 let lastHistory=null,signature="",filterSignature="";
 let remote=null,offset=0,changeVersion=0,remoteVersion=-1,requestSequence=0,filterTimer=0,loading=null;
 const taskKey=task=>(task.session?"sdk:":task.bot?"bot:":"run:")+task.id;
 const patientKey=(task,mrn)=>taskKey(task)+":"+mrn;
 const stamp=task=>Math.max(Date.parse(task.created_at)||0,Date.parse(task.updated_at)||0);
 const title=task=>task.analysis_name||task.name||(task.kind==="analysis"?"歷年檢查分析（模組未記錄）":"門診清單");
 function source(task){
  const kind=task.kind||"";
  return kind.startsWith("approval_")?"審查查詢":kind.startsWith("earnings_")?"薪資業績":kind==="surgery_schedule"?"手術系統":task.session?"SDK":kind==="analysis"||task.cohort_id?"進階工具":"門診系統";
 }
 function searchText(task,system){
  return [title(task),task.id,task.mrn,task.patient_name,task.resource,task.reference,system,
   task.created_at,time(task.created_at),labels[task.status]||task.status].join(" ").toLocaleLowerCase();
 }
 function groupTasks(tasks,{system="",query=""}={}){
  const parents=new Map(tasks.filter(task=>task.bot&&task.kind==="review").map(task=>[task.id,task]));
  const children=new Map(),attached=new Set();
  for(const task of tasks){
   if(!task.bot||!childKinds.has(task.kind))continue;
   const parent=parents.get(task.review_task_id);
   if(!parent||!parent.members?.some(member=>member.mrn===task.mrn)
    ||task.account_id&&parent.account_id&&task.account_id!==parent.account_id)continue;
   if(!children.has(parent.id))children.set(parent.id,[]);
   children.get(parent.id).push(task);attached.add(taskKey(task));
  }
  const needle=query.trim().toLocaleLowerCase(),groups=[];
  let totalGroups=0;
  for(const task of tasks){
   if(attached.has(taskKey(task)))continue;
   totalGroups++;
   const origin=source(task),allChildren=(task.bot&&task.kind==="review"?children.get(task.id)||[]:[]).sort((a,b)=>(Date.parse(b.created_at)||0)-(Date.parse(a.created_at)||0)||b.id.localeCompare(a.id));
   if(system&&system!==origin)continue;
   const ownMatch=!needle||searchText(task,origin).includes(needle);
   const matchedMembers=needle?(task.members||[]).filter(member=>[member.name,member.mrn].join(" ").toLocaleLowerCase().includes(needle)):[];
   const matches=needle?allChildren.filter(child=>searchText(child,origin).includes(needle)
    ||matchedMembers.some(member=>member.mrn===child.mrn)):allChildren;
   if(!ownMatch&&!matchedMembers.length&&!matches.length)continue;
   groups.push({task,system:origin,allChildren,children:ownMatch?allChildren:matches,matchedMembers,
    autoOpen:!!needle&&!ownMatch&&!!matches.length,
    latest:Math.max(stamp(task),...allChildren.map(stamp))});
  }
  groups.sort((a,b)=>b.latest-a.latest||taskKey(b.task).localeCompare(taskKey(a.task)));
  return {groups,totalGroups,totalTasks:tasks.length};
 }
 function collected(){if(remote)return remote.tasks;return [
  ...(work?.tasks||[]).map(task=>({...task,bot:true})),
  ...(work?.sdk_sessions||[]).map(task=>({...task,session:true})),
  ...(lastHistory?.runs||[]).filter(task=>task.kind!=="bot")
 ];}
 function actions(task){
  const box=node("span",undefined,"actions");
  const add=(label,run,key,cls="")=>{const button=btn(label,run,cls);button.dataset.activityFocus=taskKey(task)+":"+key;box.append(button);return button;};
  if(task.bot&&task.kind==="review")add("檢閱",()=>openReview(task.id),"view");
  else if(task.bot&&task.kind==="surgery_schedule")add("查看排程",async()=>{showPage("surgery");await window.SurgerySystem.open(task.id);},"view");
  else if(task.bot&&task.kind==="history")add("查看",()=>window.ReviewHistoryUI.openTask(task),"view");
  else if(task.bot&&childKinds.has(task.kind))add("查看",()=>showExtensionTask(task.id,task.kind),"view");
  else if(task.bot)add("查看摘要",()=>showTaskSummary(task.id),"view");
  if(!task.session&&inProgress.has(task.status)){
   const stop=add("暫停",async()=>{await api(task.bot?"/tasks/stop":"/stop",{id:task.id});await refresh();},"stop","quiet");stop.disabled=!!root.read_only;
  }else if(!task.session&&["paused","partial","failed","cancelled","interrupted"].includes(task.status)&&task.kind!=="list"){
   const resume=add("續跑",async()=>{if(task.bot)await api("/tasks/start",{resume:task.id});else await api("/analysis/start",{resume:task.id});await refresh();},"resume","primary");resume.disabled=!!root.read_only;
  }
  const debug=add("DEBUG 資訊",()=>showDiagnostics(task.session?{session_id:task.id}:{task_id:task.id}),"debug");setIcon(debug,"help","DEBUG 資訊",false);
  box.addEventListener("click",event=>event.stopPropagation());
  return box;
 }
 function taskRow(task,system,{summary=false,child=false,group=null}={}){
  const row=node(summary?"summary":"div",undefined,"task-row"+(child?" activity-child":" activity-root"));
  row.dataset.taskId=task.id;row.dataset.activityFocus=taskKey(task);if(!summary)row.tabIndex=-1;
  const desc=node("span",undefined,"task-description");
  if(!child){
   const origin=node("span",system,"task-source");
   if(task.bot&&childKinds.has(task.kind))origin.append(node("span","獨立查詢","activity-independent"));
   desc.append(origin);
  }
  desc.append(node("strong",title(task),"activity-title"),node("span",`${labels[task.status]||task.status} · ${time(task.created_at)}`,"caption"));
  if(task.mrn&&!child)desc.append(node("span",[task.patient_name,task.mrn].filter(Boolean).join(" · "),"caption"));
  if(group&&task.kind==="review"){
   const bits=[`${task.member_count??task.members?.length??0} 位病人`,`${group.allChildren.length} 項延伸查詢`,`備註 ${task.note_count||0} 位`];
   const running=group.allChildren.filter(item=>inProgress.has(item.status)).length;
   if(running)bits.push(`子任務執行中 ${running} 項`);
   desc.append(node("span",bits.join(" · "),"caption activity-counts"));
  }
  if(group?.matchedMembers.length){
   const matches=group.matchedMembers,caption=node("span","符合病人："+matches.slice(0,4).map(member=>[member.name,member.mrn].filter(Boolean).join(" / ")).join("、")+(matches.length>4?` 等 ${matches.length} 位`:""),"caption");desc.append(caption);
  }
  if(!task.session)desc.append(JobProgress.create(task));
  row.append(desc,actions(task));return row;
 }
 function disclosure(key,summary,autoOpen,fill){
  const details=node("details",undefined,"activity-disclosure"),body=node("div",undefined,"activity-children");
  details.dataset.activityKey=key;
  let state=expanded.has(key)||autoOpen&&!closedMatches.has(key),filled=false;
  const populate=()=>{if(!filled){fill(body);filled=true;}};
  details.open=state;details.append(summary,body);if(state)populate();
  details.addEventListener("toggle",()=>{
   if(!details.isConnected||details.open===state)return;
   state=details.open;
   if(state){expanded.add(key);closedMatches.delete(key);populate();}
   else{expanded.delete(key);if(autoOpen)closedMatches.add(key);}
  });
  return details;
 }
 function render(history=lastHistory){
  lastHistory=history;
  const tasks=collected(),system=$("#taskSystem").value,query=$("#taskQuery").value;
  const filters=JSON.stringify([system,query]);if(filterSignature!==filters){filterSignature=filters;closedMatches.clear();}
  const next=JSON.stringify([account,root?.read_only,tasks,filters]);if(next===signature)return;signature=next;
  const container=$("#taskList"),focused=container.contains(document.activeElement)?document.activeElement.dataset.activityFocus:null;
  const scroll=container.scrollTop,model=groupTasks(tasks,{system,query});
  if(!remote)$("#taskCount").textContent=tasks.filter(task=>inProgress.has(task.status)).length||"";
  $("#taskSummary").textContent=remote?`${remote.total} 組符合 · 共 ${remote.total_groups} 組任務`:`${model.groups.length} / ${model.totalGroups} 組任務`;
  if(remote)paginate($("#taskPager"),Math.floor(offset/40),remote.total,index=>{offset=index*40;return load({force:true});});
  const elements=model.groups.map(group=>{
   const {task}=group,row=taskRow(task,group.system,{summary:!!group.children.length,group});
   if(!group.children.length)return row;
   return disclosure(taskKey(task),row,group.autoOpen,body=>{
    const patients=new Map(),order=new Map((task.members||[]).map((member,index)=>[member.mrn,index]));
    for(const child of group.children){if(!patients.has(child.mrn))patients.set(child.mrn,[]);patients.get(child.mrn).push(child);}
    for(const [mrn,children] of [...patients].sort((a,b)=>(order.get(a[0])??Infinity)-(order.get(b[0])??Infinity)||a[0].localeCompare(b[0]))){
     const member=task.members?.find(item=>item.mrn===mrn),summary=node("summary",undefined,"activity-patient-heading");
     summary.dataset.activityFocus=patientKey(task,mrn);
     summary.append(node("strong",member?.name||"姓名未提供"),node("span",mrn,"mrn"),node("span",`${children.length} 項查詢`,"caption"));
     const patient=disclosure(patientKey(task,mrn),summary,group.autoOpen,content=>{for(const child of children)content.append(taskRow(child,group.system,{child:true}));});
     patient.classList.add("activity-patient");body.append(patient);
    }
   });
  });
  container.replaceChildren(...elements);container.scrollTop=scroll;
  if(!elements.length)container.append(empty(tasks.length?"沒有符合的爬蟲紀錄。":"尚無 SDK 抓取紀錄。"));
  if(focused)$$('[data-activity-focus]',container).find(element=>element.dataset.activityFocus===focused)?.focus({preventScroll:true});
 }
 async function reveal(taskId){
  $("#taskStatus").value=$("#taskStart").value=$("#taskEnd").value="";$("#taskWithNotes").checked=false;
  $("#taskSystem").value="";$("#taskQuery").value=taskId;offset=0;
  await load({force:true});
  const model=groupTasks(collected());
  for(const group of model.groups){
   const child=group.allChildren.find(task=>task.id===taskId);
   if(child){expanded.add(taskKey(group.task));expanded.add(patientKey(group.task,child.mrn));break;}
  }
  signature="";Choices.sync();render();
 }
 function invalidate(){changeVersion++;}
 async function load({force=false}={}){
  if(!force&&remoteVersion===changeVersion&&remote)return;
  const params={system:$("#taskSystem").value,q:$("#taskQuery").value,status:$("#taskStatus").value,start:$("#taskStart").value,end:$("#taskEnd").value,notes:$("#taskWithNotes").checked?"1":"",offset,limit:40};
  const key=JSON.stringify(params);
  if(loading?.key===key)return loading.promise;
  const sequence=++requestSequence,selectedAccount=account,version=changeVersion;
  const promise=(async()=>{
   const result=await api("/activity?"+new URLSearchParams(params));
   if(sequence!==requestSequence||selectedAccount!==account)return;
   if(result.total&&offset>=result.total){offset=Math.floor((result.total-1)/40)*40;loading=null;return load({force:true});}
   remote=result;remoteVersion=version;signature="";render();
  })().finally(()=>{if(loading?.promise===promise)loading=null;});
  loading={key,promise};return promise;
 }
 function reset(){requestSequence++;clearTimeout(filterTimer);loading=null;remote=null;offset=0;changeVersion=0;remoteVersion=-1;lastHistory=null;signature=filterSignature="";expanded.clear();closedMatches.clear();for(const id of ["taskSystem","taskQuery","taskStatus","taskStart","taskEnd"])$("#"+id).value="";$("#taskWithNotes").checked=false;$("#taskPager").replaceChildren();}
 const search=()=>{offset=0;clearTimeout(filterTimer);return act(()=>load({force:true}));};
 $("#taskSearchForm").addEventListener("submit",event=>{event.preventDefault();void search();});
 $("#taskQuery").addEventListener("input",()=>{clearTimeout(filterTimer);filterTimer=setTimeout(search,250);});
 for(const id of ["taskSystem","taskStatus","taskStart","taskEnd","taskWithNotes"])$("#"+id).addEventListener("change",search);
 return {render,reveal,reset,groupTasks,load,invalidate,update:()=>{invalidate();if(page==="tasks")return load({force:true});}};
})();
