"use strict";
window.ToolWorkspace=(()=>{
  const names={review:"病歷檢閱",retina:"視網膜比較",cataract:"白內障術前分析",surgery:"刀表更新"};
  const retrieval={cache:"重用已存病歷",refresh:"檢查新紀錄",force:"重新下載病歷"};
  let kind="review",states={},drafts={},loaded=false,group=null,generation=0;
  let soapSetup=false,soapTask="",soapSetId="";
  const lookupErrors=window.PatientTokens.createErrors({
    container:"toolLookupErrors",onOpen:taskId=>act(()=>openLookupDiagnostics(taskId)),
  });
  let setupOpen=true,activeTask=null,resultKind="",starting=false;
  let settingsDraft=null;
  const settingValues=()=>({departmentKeyword:$("#departmentKeyword").value,soapMode:$("#soapMode").value,forceReview:$("#forceReview").checked});
  function departmentKeywords(){return [...new Map($("#departmentKeyword").value.split(/[,，、]/).map(value=>value.trim()).filter(Boolean).map(value=>[value.toLocaleLowerCase(),value])).values()];}
  function checkedKeywords(){const values=departmentKeywords();if(!values.length||values.length>10||values.some(value=>value.length>100))throw new Error("請輸入 1–10 個科別關鍵字，每個至多 100 字。");return values;}
  function queryOptions(){return {department_keywords:checkedKeywords(),mode:$("#soapMode").value};}
  function loadOptions(){
    const values=work?.preferences.review_options||{};
    $("#departmentKeyword").value=(values.department_keywords||[values.department_keyword||"眼科"]).join("、");
    $("#soapMode").value=values.mode||"latest";
    $("#forceReview").checked=false;
    render();
  }
  function beginSettings(){settingsDraft=settingValues();$("#tagSettings").open=false;render();}
  function commitSettings(){const changed=settingsDraft&&JSON.stringify(settingsDraft)!==JSON.stringify(settingValues());settingsDraft=null;if(changed&&activeTask){setupOpen=true;if(page==="review"){reviewSetupExpanded=true;document.body.classList.remove("review-result-compact");}}render();}
  function cancelSettings(){if(settingsDraft){$("#departmentKeyword").value=settingsDraft.departmentKeyword;$("#soapMode").value=settingsDraft.soapMode;$("#forceReview").checked=settingsDraft.forceReview;settingsDraft=null;render();}}
  function reset(){
    generation++;soapSetup=false;soapTask=soapSetId="";$("#toolSoapProgress").replaceChildren();$("#toolLaunchProgress").replaceChildren();
    kind="review";states={};drafts={};loaded=false;group=null;lookup.reset();lookupErrors.clear();
    setupOpen=true;activeTask=null;resultKind="";starting=false;
    settingsDraft=null;$("#departmentKeyword").value="眼科";$("#soapMode").value="latest";$("#forceReview").checked=false;
    visibility("patients");
  }
  function visibility(name){
    $("#toolSourcePanel").hidden=!["tool","review","module"].includes(name);
    for(const button of $$("[data-tool]")){
      const active=!$("#toolSourcePanel").hidden&&button.dataset.tool===kind;
      button.classList.toggle("active",active);
      if(active)button.setAttribute("aria-current","page");else button.removeAttribute("aria-current");
    }
  }
  function activate(value){
    if(kind!==value){activeTask=null;resultKind="";setupOpen=true;}
    kind=value;$("#toolKind").value=value;$("#toolWorkspaceTitle").textContent=names[value];visibility(page);
  }
  async function save(){if(root.read_only)return;const id=group?.id||group?._base_set_id||"";states[kind]={set_id:id};await api("/tools/state/save",{module:kind,set_id:id});}
  async function choose(value){group=value;selectedSet=value;delete drafts[kind];await save();render();}
  function editMembers(members){
    const base=group?._base_set_id||group?.id||"";
    group={id:"",_base_set_id:base,name:group?.name||"手動病人集合",members};
    drafts[kind]=group;selectedSet=group;render();
  }
  function addResolved(patient){
    if(!patient.mrn)return;
    lookupErrors.remove("auto:"+patient.input);
    if(group?.members.some(member=>member.mrn===patient.mrn))return;
    editMembers([...(group?.members||[]),{mrn:patient.mrn,name:patient.name||"",sex:patient.sex||"",
      age:patient.age||"",birthday:patient.birthday||"",registrations:[],source_records:[],origins:["manual"]}]);
  }
  async function ensureSavedGroup(){
    if(!group?.members.length)throw new Error("請先選擇病人。");
    if(group.id)return group;
    const current=group,revision=generation;
    const set=await api("/sets/save",{source_set_id:current._base_set_id||undefined,
      mrns:current.members.map(member=>member.mrn).join("\n"),name:current.name});
    if(revision!==generation||group!==current)return null;
    group=set;selectedSet=set;delete drafts[kind];await save();await refresh();render();return set;
  }
  function render(){
    const isReview=kind==="review",hasResult=!!activeTask||resultKind===kind;
    $("#toolKind").value=kind;toolOptions();
    $("#toolReviewOptions").hidden=!(isReview||kind==="surgery"&&soapSetup);$("#reviewOptions").hidden=$("#toolReviewOptions").hidden;
    $("#toolPage").firstElementChild.hidden=!!group?.members.length;
    $("#toolSoapProgress").hidden=kind!=="surgery"||group?.id!==soapSetId;
    $("#toolSelectedCount").textContent=!setupOpen&&activeTask?activeTask.members.length+" 位病人":group?.members.length?group.members.length+" 位病人":"尚未選擇病人";
    $("#toolSelectionSummary").replaceChildren(...(group?.members||[]).map(patient=>patientChip(patient,()=>editMembers(group.members.filter(member=>member.mrn!==patient.mrn)))));
    $("#toolSetupBody").hidden=!setupOpen;$("#toolSourcePanel").classList.toggle("setup-collapsed",!setupOpen);
    $("#toolEditSetup").hidden=!hasResult;setIcon($("#toolEditSetup"),"patients",setupOpen?"收起病人來源":"更換病人");$("#toolEditSetup").setAttribute("aria-expanded",String(setupOpen));
    $("#toolRunSummary").hidden=setupOpen;
    const taskDepartment=activeTask?.department_keywords?.length?"科別含任一：「"+activeTask.department_keywords.join("」、「")+"」":activeTask?.department_keyword?"科別含「"+activeTask.department_keyword+"」":activeTask?.department_filter==="all"?"不限科別":"同科別：「"+(activeTask?.department?.name||"眼科")+"」";
    $("#toolRunSummary").textContent=activeTask?(activeTask.mode==="registration"?"該次門診的 SOAP":"最新 SOAP")+" · "+taskDepartment+" · 截至 "+activeTask.cutoff+" · "+retrieval[activeTask.force?"force":activeTask.refresh?"refresh":"cache"]:group?.name||"";
    $("#toolSettingsSummary").hidden=!(isReview||kind==="surgery"&&soapSetup);
    $("#toolSettingsSummary").textContent=[$("#soapMode").value==="registration"?"該次門診的 SOAP":"最新 SOAP","科別含任一：「"+departmentKeywords().join("」、「")+"」",retrieval[$("#forceReview").checked?"force":"cache"]].join(" · ");
    $("#toolNewReviewHint").hidden=!isReview||!activeTask;
    $("#toolStart").textContent=isReview?(starting?"正在建立檢閱…":activeTask?"開始新檢閱":"開始檢閱"):"開啟"+names[kind];
    $("#toolStart").disabled=starting||!group?.members.length||root.read_only||(isReview&&!work?.online);
    $("#toolFetchSoap").hidden=kind!=="surgery";$("#toolFetchSoap").textContent=soapSetup?"開始取得／更新 SOAP":"取得／更新此集合 SOAP";
    $("#toolFetchSoap").disabled=starting||!group?.members.length||!work?.online||root.read_only;
    $("#toolRules").hidden=!(isReview||kind==="surgery"&&soapSetup);$("#toolRules").disabled=root.read_only;
    Choices.sync();
  }
  async function open(value,selectedGroup=null){
    if(group&&!group.id)drafts[kind]=group;
    lookup.reset();
    const revision=++generation;soapSetup=false;activeTask=null;resultKind="";setupOpen=true;activate(value);
    if(!loaded){const data=await api("/tools/state",{});if(revision!==generation)return;states=data.modules;loaded=true;}
    await refresh();if(revision!==generation)return;
    // A refresh may deliver the previous task while the new workspace is opening.
    activeTask=null;resultKind="";setupOpen=true;
    group=selectedGroup||drafts[value]||(work.sets||[]).find(s=>s.id===states[value]?.set_id)||null;selectedSet=group;
    if(selectedGroup)delete drafts[value];
    lookupErrors.clear();$("#toolLaunchProgress").replaceChildren();
    loadOptions();
    if(selectedGroup)await save();if(revision!==generation)return;$("#moduleFrame").removeAttribute("src");showPage("tool");render();
  }
  async function adoptModule(id){
    const revision=generation,value=await api("/analysis/cohorts",{});if(revision!==generation)return false;
    const cohort=value.cohorts.find(c=>c.id===id);group=(work.sets||[]).find(s=>s.id===cohort?.source_set)||null;selectedSet=group;
    resultKind=kind;setupOpen=false;render();return true;
  }
  function adoptReview(task){
    if(activeTask?.id!==task.id){
      group=(work?.sets||[]).find(s=>s.id===task.set_id)||null;selectedSet=group;if(group)states.review={set_id:group.id};
      $("#departmentKeyword").value=(task.department_keywords||[task.department_keyword||"眼科"]).join("、");
      $("#soapMode").value=task.mode;$("#forceReview").checked=false;
      setupOpen=false;
    }
    // Polling updates the displayed snapshot, never the editable next-task draft.
    activeTask=task;render();
  }
  async function startReview(inline=false){
    if(!group?.members.length)throw new Error("請先選擇病人。");
    let keywords;
    try{keywords=checkedKeywords();}catch(error){openRules();$("#departmentKeyword").focus();throw error;}
    if($("#soapMode").value==="registration"&&group.members.some(p=>!p.registrations?.length))throw new Error("此集合包含沒有掛號來源的病人，請使用最新 SOAP 模式。");
    const revision=generation,force=$("#forceReview").checked;
    let pending;
    if(inline){soapSetId=group.id;render();pending=JobProgress.pending("#toolSoapProgress","SOAP 病歷","正在送出檢閱任務…");}
    let result;
    try{
      const selected=await ensureSavedGroup();if(!selected||revision!==generation){pending?.remove();return;}
      if(inline){soapSetId=selected.id;render();}
      result=await api("/tasks/start",{kind:"review",set_id:selected.id,department_keywords:keywords,
        mode:$("#soapMode").value,refresh:force,force});
    }
    catch(error){pending?.remove();if(inline){soapSetId="";render();}throw error;}
    if(revision!==generation){pending?.remove();return;}
    $("#forceReview").checked=false;
    if(inline){soapTask=result.task_id;soapSetId=group.id;soapSetup=false;$("#toolSoapProgress").replaceChildren(node("p","正在取得此集合 SOAP…","caption"));render();}
    else{await openReview(result.task_id);if(revision!==generation)return;$("#reviewTitle").focus({preventScroll:true});$("#toolSourcePanel").scrollIntoView({block:"start"});}
    await refresh();
  }
  async function start(){
    if(starting)return;if(!group?.members.length)throw new Error("請先選擇病人。");
    const revision=generation,module=kind;starting=true;render();
    const pending=JobProgress.pending("#toolLaunchProgress",names[module],"正在建立抓取任務…");
    try{
      const selected=await ensureSavedGroup();if(!selected||revision!==generation)return;
      await save();if(revision!==generation)return;if(module==="review")return await startReview();
      const result=await api("/sets/cohort",{set_id:selected.id});if(revision!==generation)return;await openModule(result.id,module);render();
    }finally{pending?.remove();if(revision===generation){starting=false;render();}}
  }
  async function poll(){
    if(soapTask){const id=soapTask,revision=generation,task=await api("/tasks/detail?id="+id);if(id!==soapTask||revision!==generation)return;JobProgress.show("#toolSoapProgress",task);if(!inProgress.has(task.status)){soapTask="";if(task.status!=="completed")$("#toolSoapProgress").append(node("p",task.message+"；可由爬蟲紀錄續跑。","error"));else if(kind==="surgery"&&group?.id===soapSetId){say("SOAP 已保存，可重新整理手術候選。");$("#moduleFrame").contentWindow?.postMessage({type:"bot:soap-updated"},location.origin);}}}
  }
  function renderSaved(){
    const query=$("#toolSavedQuery").value.trim().toLocaleLowerCase(),area=$("#toolSavedSets");
    const groups=(work?.sets||[]).filter(s=>[s.name,...s.members.map(p=>p.mrn)].join(" ").toLocaleLowerCase().includes(query));
    area.replaceChildren(...groups.map(s=>{const button=btn(s.name,async()=>{generation++;lookup.reset();lookupErrors.clear();await choose(s);$("#toolSavedDialog").close();},"saved-set-option");button.replaceChildren(node("strong",s.name),node("span",s.members.length+" 位 · "+s.members.slice(0,3).map(p=>p.name||p.mrn).join("、"),"caption"));return button;}));
    if(!groups.length)area.append(node("p","沒有符合的已保存清單","empty"));
  }
  const lookup=window.PatientTokens.create({
    input:"toolIdentifier",kind:()=> "auto",request:api,
    idleLookupMs:700,
    canLookup:()=>!!account&&!!work?.online&&!root.read_only,status:"toolResolveResult",
    onResolved:addResolved,
    onFailed:(token,message,mode,taskId)=>lookupErrors.add(token,message,mode,taskId),
  });
  for(const button of $$("[data-tool]"))button.addEventListener("click",()=>act(()=>button.dataset.tool==="review"?resumeReview():open(button.dataset.tool),button));
  for(const button of $$("[data-use-tool]"))button.addEventListener("click",()=>act(async()=>{await open(button.dataset.useTool,await saveCurrentSet());}));
  $("#toolOpenSaved").addEventListener("click",()=>act(async()=>{$("#toolSavedQuery").value="";await refresh();renderSaved();dialog("toolSavedDialog");}));
  $("#toolSavedQuery").addEventListener("input",renderSaved);
  $("#toolGoList").addEventListener("click",()=>navigate("patients"));
  for(const id of ["departmentKeyword","soapMode"])$("#"+id).addEventListener("input",render);
  $("#forceReview").addEventListener("change",render);
  $("#toolEditSetup").addEventListener("click",()=>{setupOpen=!setupOpen;if(page==="review"&&setupOpen){reviewSetupExpanded=true;document.body.classList.remove("review-result-compact");}render();if(setupOpen)$("#toolSourcePanel").scrollIntoView({block:"start"});});
  $("#toolStartForm").addEventListener("submit",e=>{e.preventDefault();act(start);});$("#toolRules").addEventListener("click",openRules);
  $("#toolFetchSoap").addEventListener("click",()=>act(async()=>{if(!soapSetup){soapSetup=true;render();}else await startReview(true);}));
  return {reset,open,activate,visibility,adoptReview,adoptModule,poll,refreshSettings:render,beginSettings,commitSettings,cancelSettings,queryOptions};
})();
