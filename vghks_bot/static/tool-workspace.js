"use strict";
window.ToolWorkspace=(()=>{
  const names={review:"病歷檢閱",retina:"視網膜比較",cataract:"白內障術前分析",surgery:"刀表更新"};
  const retrieval={cache:"重用已存病歷",refresh:"檢查新紀錄",force:"重新抓取全部病人"};
  let kind="review",states={},drafts={},loaded=false,group=null,generation=0,sourceRevision=0,moduleAdoption=0;
  let soapSetup=false,soapTask="",soapSetId="";
  const lookupErrors=window.PatientTokens.createErrors({
    container:"toolLookupErrors",onOpen:taskId=>act(()=>openLookupDiagnostics(taskId)),
    onRetry:(token,mode)=>act(()=>{if(!work?.online)throw new Error("請先重新連線，再重試此筆。");lookup.add([token],mode);}),
    onChange:()=>render(),
  });
  let setupOpen=true,activeTask=null,resultKind="",starting=false,savedSequence=0,savedTimer=null;
  const count=value=>value?.member_count??value?.members_count??value?.members?.length??0;
  function requireChecked(){
    if(lookup.hasBlockers())throw new Error("尚有識別號碼未完成查核，請先完成輸入並等候查核結果。");
    if(lookupErrors.count())throw new Error("有查核失敗的識別號碼，請重試或明確移除後再開始。");
  }
  async function ensureMembers(){
    if(!group||Array.isArray(group.members))return group;
    const current=group,revision=generation,selectedAccount=account;
    const full=await api("/sets/read",{id:current.id});
    if(revision!==generation||selectedAccount!==account||group!==current)return null;
    group=full;selectedSet=full;return group;
  }
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
  function commitSettings(){const changed=settingsDraft&&JSON.stringify(settingsDraft)!==JSON.stringify(settingValues());settingsDraft=null;if(changed&&activeTask){setupOpen=true;if(page==="review"){reviewSetupExpanded=true;updateReviewLayout();}void act(async()=>{await ensureMembers();render();});}render();}
  function cancelSettings(){if(settingsDraft){$("#departmentKeyword").value=settingsDraft.departmentKeyword;$("#soapMode").value=settingsDraft.soapMode;$("#forceReview").checked=settingsDraft.forceReview;settingsDraft=null;render();}}
  function reset(){
    window.SheetSyncUI?.reset();
    generation++;sourceRevision++;moduleAdoption++;savedSequence++;clearTimeout(savedTimer);soapSetup=false;soapTask=soapSetId="";$("#toolSoapProgress").replaceChildren();$("#toolLaunchProgress").replaceChildren();
    kind="review";states={};drafts={};loaded=false;group=null;lookup.reset();lookupErrors.clear();
    setupOpen=true;activeTask=null;resultKind="";starting=false;
    settingsDraft=null;$("#departmentKeyword").value="眼科";$("#soapMode").value="latest";$("#forceReview").checked=false;
    visibility("patients");
  }
  function visibility(name){
    window.SheetSyncUI?.visibility(name,kind);
    $("#toolSourcePanel").hidden=!["tool","review","module"].includes(name);
    $("#reviewProgress").hidden=name!=="review";
    for(const button of $$("[data-tool]")){
      const active=!$("#toolSourcePanel").hidden&&button.dataset.tool===kind;
      button.classList.toggle("active",active);
      if(active)button.setAttribute("aria-current","page");else button.removeAttribute("aria-current");
    }
  }
  function activate(value){
    if(kind!==value){generation++;starting=false;lookup.reset();lookupErrors.clear();activeTask=null;resultKind="";setupOpen=true;}
    kind=value;$("#toolKind").value=value;$("#toolWorkspaceLabel").textContent=names[value];visibility(page);
  }
  async function save(){if(root.read_only)return;const id=group?.id||group?._base_set_id||"";states[kind]={set_id:id};await api("/tools/state/save",{module:kind,set_id:id});}
  async function choose(value){group=value;selectedSet=value;delete drafts[kind];if(!await ensureMembers())return;await save();render();}
  function editMembers(members){
    const base=group?._base_set_id||group?.id||"";
    group={id:"",_base_set_id:base,name:group?.name||"手動病人集合",members};
    drafts[kind]=group;selectedSet=group;render();
  }
  async function clearPatients(){
    if(starting||!count(group)&&!lookup.hasBlockers()&&!lookupErrors.count())return;
    const revision=generation;
    if(count(group)&&!await ensureMembers()||revision!==generation)return;
    lookup.reset();lookupErrors.clear();
    if(group?.worklist_module)return removeWorklist(group.members.map(member=>member.mrn));
    generation++;lookup.reset();lookupErrors.clear();soapSetup=false;
    group={id:"",name:"手動病人集合",members:[]};drafts[kind]=group;selectedSet=group;
    render();await save();
  }
  async function removeWorklist(mrns){
    const current=group,revision=generation,selectedAccount=account,module=current.worklist_module,result=await api("/tools/handoff/remove",{module,mrns});
    if(revision!==generation||account!==selectedAccount||group!==current)return;
    generation++;lookup.reset();lookupErrors.clear();group=result.worklist;selectedSet=group;
    receivedHandoff(module,result);await refresh();render();
    const frame=module==="cataract"?$("#cataractFrame"):$("#moduleFrame");
    if(frame.dataset.cohort===result.cohort.id)frame.contentWindow?.postMessage({type:"bot:worklist-updated",account,cohort:result.cohort.id},location.origin);
  }
  function addResolved(patient){
    if(!patient.mrn)return;
    lookupErrors.remove("auto:"+patient.input);
    if(group?.members?.some(member=>member.mrn===patient.mrn))return;
    editMembers([...(group?.members||[]),{mrn:patient.mrn,name:patient.name||"",sex:patient.sex||"",
      age:patient.age||"",birthday:patient.birthday||"",registrations:[],source_records:[],origins:["manual"]}]);
  }
  async function ensureSavedGroup(){
    requireChecked();const source=sourceRevision,revision=generation;
    if(!await ensureMembers())return null;
    if(source!==sourceRevision||revision!==generation)return null;
    requireChecked();
    if(!count(group))throw new Error("請先選擇病人。");
    if(group.id)return group;
    const current=group;
    requireChecked();
    const set=await api("/sets/save",{source_set_id:current._base_set_id||undefined,
      mrns:current.members.map(member=>member.mrn).join("\n"),name:current.name});
    if(revision!==generation||source!==sourceRevision||group!==current)return null;
    requireChecked();group=set;selectedSet=set;delete drafts[kind];await save();if(revision!==generation||source!==sourceRevision||group!==set)return null;requireChecked();await refresh();if(revision!==generation||source!==sourceRevision||group!==set)return null;requireChecked();render();return set;
  }
  function render(){
    window.SheetSyncUI?.visibility(page,kind);
    const isReview=kind==="review",hasResult=!!activeTask||resultKind===kind,inputState=lookup.state(),blocked=inputState.blocked||!!lookupErrors.count();
    $("#toolKind").value=kind;toolOptions();
    $("#toolReviewOptions").hidden=!(isReview||kind==="surgery"&&soapSetup);$("#reviewOptions").hidden=$("#toolReviewOptions").hidden;
    $("#toolPage").firstElementChild.hidden=!!count(group);
    $("#toolSoapProgress").hidden=kind!=="surgery"||group?.id!==soapSetId;
    $("#toolSelectedCount").textContent=(!setupOpen&&activeTask?count(activeTask)+" 位病人"+(isReview?" · "+(labels[activeTask.status]||activeTask.status):""):count(group)?count(group)+" 位病人":"尚未選擇病人")+(inputState.pendingCount?` · ${inputState.pendingCount} 筆查核中`:"")+(lookupErrors.count()?` · ${lookupErrors.count()} 筆查核失敗`:"")+(inputState.draft.trim()?" · 輸入尚未查核":"");
    const hint=inputState.composing?"請完成文字輸入後再開始。":inputState.draft.trim()?"請按 Enter 或稍候完成查核後再開始。":inputState.pendingCount?`尚有 ${inputState.pendingCount} 筆查核中，完成後即可開始。`:lookupErrors.count()?"請重試或移除查核失敗的號碼後再開始。":"";
    $("#toolLookupHint").hidden=!hint;if($("#toolLookupHint").textContent!==hint)$("#toolLookupHint").textContent=hint;
    if(setupOpen)$("#toolSelectionSummary").replaceChildren(...(group?.members||[]).map(patient=>patientChip(patient,()=>{lookup.forget(patient.mrn);return group.worklist_module?removeWorklist([patient.mrn]):editMembers(group.members.filter(member=>member.mrn!==patient.mrn));})));else $("#toolSelectionSummary").replaceChildren();
    $("#toolIdentifier").disabled=starting||!!group&&!Array.isArray(group.members);
    $("#toolClearPatients").disabled=starting||!count(group)&&!blocked||!!root.read_only&&!!group?.worklist_module;
    $("#toolSetupBody").hidden=!setupOpen;$("#toolSourcePanel").classList.toggle("setup-collapsed",!setupOpen);
    $("#toolEditSetup").hidden=!hasResult;setIcon($("#toolEditSetup"),"patients",setupOpen?"收起病人來源":"更換病人");$("#toolEditSetup").setAttribute("aria-expanded",String(setupOpen));
    $("#toolHeadingToggle").setAttribute("aria-expanded",String(setupOpen));$("#toolHeadingToggle").setAttribute("aria-label",names[kind]+"，"+(setupOpen?"收起":"展開")+"病人來源與設定");
    $("#toolRunSummary").hidden=setupOpen;
    const taskDepartment=activeTask?.department_keywords?.length?"科別含任一：「"+activeTask.department_keywords.join("」、「")+"」":activeTask?.department_keyword?"科別含「"+activeTask.department_keyword+"」":activeTask?.department_filter==="all"?"不限科別":"同科別：「"+(activeTask?.department?.name||"眼科")+"」";
    $("#toolRunSummary").textContent=activeTask?(activeTask.mode==="registration"?"該次門診的 SOAP":"最新 SOAP")+" · "+taskDepartment+" · 截至 "+activeTask.cutoff+" · "+(activeTask.refresh_mrn?"重新抓取 "+activeTask.refresh_mrn:retrieval[activeTask.force?"force":activeTask.refresh?"refresh":"cache"]):group?.name||"";
    $("#toolSettingsSummary").hidden=!(isReview||kind==="surgery"&&soapSetup);
    $("#toolSettingsSummary").textContent=[$("#soapMode").value==="registration"?"該次門診的 SOAP":"最新 SOAP","科別含任一：「"+departmentKeywords().join("」、「")+"」",retrieval[$("#forceReview").checked?"force":"cache"]].join(" · ");
    $("#toolStart").textContent=isReview?(starting?"正在建立檢閱…":activeTask?"開始新檢閱":"開始檢閱"):"開啟"+names[kind];
    $("#toolStart").disabled=starting||blocked||!count(group)||root.read_only||(isReview&&!work?.online);
    $("#toolFetchSoap").hidden=kind!=="surgery";$("#toolFetchSoap").textContent=soapSetup?"開始取得／更新 SOAP":"取得／更新此集合 SOAP";
    $("#toolFetchSoap").disabled=starting||blocked||!count(group)||!work?.online||root.read_only;
    $("#toolRules").hidden=!(isReview||kind==="surgery"&&soapSetup);$("#toolRules").disabled=root.read_only;
    Choices.sync();
  }
  async function open(value,selectedGroup=null){
    if(group&&!group.id)drafts[kind]=group;
    lookup.reset();lookupErrors.clear();
    generation++;soapSetup=false;activeTask=null;resultKind="";setupOpen=true;starting=false;activate(value);const revision=generation;
    if(!loaded){const data=await api("/tools/state",{});if(revision!==generation)return;states=data.modules;loaded=true;}
    await refresh();if(revision!==generation)return;
    // A refresh may deliver the previous task while the new workspace is opening.
    activeTask=null;resultKind="";setupOpen=true;
    group=selectedGroup||drafts[value]||(work.sets||[]).find(s=>s.id===states[value]?.set_id)||null;selectedSet=group;
    if(selectedGroup)delete drafts[value];
    if(group&&!await ensureMembers())return;if(revision!==generation)return;
    $("#toolLaunchProgress").replaceChildren();
    loadOptions();
    if(selectedGroup)await save();if(revision!==generation)return;$("#moduleFrame").removeAttribute("src");showPage("tool");render();
  }
  async function adoptModule(id){
    const revision=generation,source=sourceRevision,module=kind,sequence=++moduleAdoption,value=await api("/analysis/cohorts",{});if(revision!==generation||source!==sourceRevision||module!==kind||sequence!==moduleAdoption)return false;
    const cohort=value.cohorts.find(c=>c.id===id);if(!cohort)return false;
    sourceRevision++;starting=false;
    lookup.reset();lookupErrors.clear();group=(work.sets||[]).find(s=>s.id===cohort.source_set)||null;selectedSet=group;
    resultKind=kind;setupOpen=false;render();return true;
  }
  function adoptReview(task){
    if(activeTask?.id!==task.id){
      sourceRevision++;starting=false;
      lookup.reset();lookupErrors.clear();
      group=(work?.sets||[]).find(s=>s.id===task.set_id)||null;selectedSet=group;if(group)states.review={set_id:group.id};
      $("#departmentKeyword").value=(task.department_keywords||[task.department_keyword||"眼科"]).join("、");
      $("#soapMode").value=task.mode;$("#forceReview").checked=false;
      setupOpen=false;
    }
    // Polling updates the displayed snapshot, never the editable next-task draft.
    activeTask=task;render();
  }
  async function startReview(inline=false){
    requireChecked();const revision=generation,source=sourceRevision,module=kind;
    if(!await ensureMembers())return;
    if(revision!==generation||source!==sourceRevision||kind!==module)return;
    requireChecked();
    if(!count(group))throw new Error("請先選擇病人。");
    let keywords;
    try{keywords=checkedKeywords();}catch(error){openRules();$("#departmentKeyword").focus();throw error;}
    if($("#soapMode").value==="registration"&&group.members.some(p=>!p.registrations?.length))throw new Error("此集合包含沒有掛號來源的病人，請使用最新 SOAP 模式。");
    const intent=group,force=$("#forceReview").checked,baseId=inline?"":activeTask?.id;
    let pending;
    if(inline){soapSetId=group.id;render();pending=JobProgress.pending("#toolSoapProgress","SOAP 病歷","正在送出檢閱任務…");}
    let result;
    try{
      if(baseId&&window.ReviewNotesUI&&!await window.ReviewNotesUI.flushAll())throw new Error("有備註尚未儲存，請重試後再開始檢閱。");
      requireChecked();
      if(revision!==generation||source!==sourceRevision||kind!==module||group!==intent||baseId&&activeTask?.id!==baseId){pending?.remove();return;}
      const selected=await ensureSavedGroup();if(!selected||revision!==generation||source!==sourceRevision||kind!==module||group?.id!==selected.id||baseId&&activeTask?.id!==baseId){pending?.remove();return;}
      requireChecked();
      if(inline){soapSetId=selected.id;render();}
      result=await api("/tasks/start",{kind:"review",set_id:selected.id,department_keywords:keywords,
        mode:$("#soapMode").value,refresh:force,force,...(baseId?{base_task_id:baseId}:{})});
    }
    catch(error){pending?.remove();if(inline&&revision===generation&&source===sourceRevision){soapSetId="";render();}throw error;}
    if(revision!==generation||source!==sourceRevision||kind!==module){pending?.remove();return;}
    $("#forceReview").checked=false;
    if(inline){soapTask=result.task_id;soapSetId=group.id;soapSetup=false;$("#toolSoapProgress").replaceChildren(node("p","正在取得此集合 SOAP…","caption"));render();}
    else{await openReview(result.task_id);if(revision!==generation)return;$("#toolWorkspaceTitle").focus({preventScroll:true});$("#toolSourcePanel").scrollIntoView({block:"start"});}
    await refresh();
  }
  async function refreshPatient(mrn){
    if(starting||!activeTask||root.read_only||!work?.online)return;
    requireChecked();
    const source=activeTask,revision=generation,scope=sourceRevision;starting=true;render();
    try{
      if(!await window.ReviewNotesUI.flushAll())throw new Error("有備註尚未儲存，請重試後再重新抓取。");
      requireChecked();
      if(revision!==generation||scope!==sourceRevision||activeTask?.id!==source.id)return;
      const result=await api("/tasks/start",{kind:"review",base_task_id:source.id,refresh_mrn:mrn});
      if(revision!==generation||scope!==sourceRevision||activeTask?.id!==source.id)return;
      await openReview(result.task_id);if(revision!==generation)return;
      await selectReviewPatient(mrn);await refresh();
    }finally{if(revision===generation&&scope===sourceRevision){starting=false;render();}}
  }
  async function start(){
    if(starting)return;requireChecked();if(!count(group))throw new Error("請先選擇病人。");
    const revision=generation,source=sourceRevision,module=kind;starting=true;render();
    const pending=JobProgress.pending("#toolLaunchProgress",names[module],"正在建立抓取任務…");
    try{
      const selected=await ensureSavedGroup();if(!selected||revision!==generation||source!==sourceRevision||kind!==module)return;
      requireChecked();await save();if(revision!==generation||source!==sourceRevision||kind!==module||group?.id!==selected.id)return;requireChecked();if(module==="review")return await startReview();
      const result=await api("/sets/cohort",{set_id:selected.id});if(revision!==generation||source!==sourceRevision||kind!==module||group?.id!==selected.id)return;requireChecked();await openModule(result.id,module);render();
    }finally{pending?.remove();if(revision===generation&&source===sourceRevision){starting=false;render();}}
  }
  async function poll(){
    if(soapTask){const id=soapTask,task=work.tasks.find(row=>row.id===id);if(!task)return;JobProgress.show("#toolSoapProgress",task);if(!inProgress.has(task.status)){soapTask="";if(task.status!=="completed")$("#toolSoapProgress").append(node("p",task.message+"；可由爬蟲紀錄續跑。","error"));else if(kind==="surgery"&&group?.id===soapSetId){say("SOAP 已保存，可重新整理手術候選。");$("#moduleFrame").contentWindow?.postMessage({type:"bot:soap-updated"},location.origin);}}}
  }
  function receivedHandoff(module,result){
    states[module]={set_id:result.worklist.id,cohort_id:result.cohort.id};delete drafts[module];
  }
  async function renderSaved(){
    const sequence=++savedSequence,selectedAccount=account,revision=generation,query=$("#toolSavedQuery").value.trim(),area=$("#toolSavedSets");
    const groups=query?(await api("/sets/search",{q:query})).sets:(work?.sets||[]);
    if(sequence!==savedSequence||selectedAccount!==account||revision!==generation)return;
    area.replaceChildren(...groups.map(value=>{const button=btn(value.name,async()=>{const selected=++generation;starting=false;lookup.reset();lookupErrors.clear();await choose(value);if(selected===generation)$("#toolSavedDialog").close();},"saved-set-option"),preview=value.members_preview||value.members||[];button.replaceChildren(node("strong",value.name),node("span",count(value)+" 位 · "+preview.slice(0,3).map(patient=>patient.name||patient.mrn).join("、"),"caption"));return button;}));
    if(!groups.length)area.append(node("p","沒有符合的已保存清單","empty"));
  }
  const lookup=window.PatientTokens.create({
    input:"toolIdentifier",kind:()=> "auto",request:api,
    idleLookupMs:700,
    canLookup:()=>!!account&&!!work?.online&&!root.read_only&&!starting&&(!group||Array.isArray(group.members)),status:"toolResolveResult",
    pendingContainer:"toolIdentifierPending",scope:()=>JSON.stringify([account,epoch,generation,kind]),onStateChange:()=>render(),
    onResolved:addResolved,
    onFailed:(token,message,mode,taskId)=>lookupErrors.add(token,message,mode,taskId),
  });
  async function toggleSetup(){
    setupOpen=!setupOpen;if(page==="review"){reviewSetupExpanded=setupOpen;updateReviewLayout();}render();
    if(setupOpen){const revision=generation;await ensureMembers();if(revision!==generation)return;render();$("#toolSourcePanel").scrollIntoView({block:"start"});$("#toolIdentifier").focus({preventScroll:true});}
  }
  for(const button of $$("[data-tool]"))button.addEventListener("click",()=>act(async()=>{
    if(button.dataset.tool==="review")return resumeReview();
    if(button.dataset.tool==="cataract"&&await restoreCataract())return;
    return open(button.dataset.tool);
  },button));
  for(const button of $$("[data-use-tool]"))button.addEventListener("click",()=>act(async()=>{await open(button.dataset.useTool,await saveCurrentSet());}));
  $("#toolOpenSaved").addEventListener("click",()=>act(async()=>{$("#toolSavedQuery").value="";await refresh();await renderSaved();dialog("toolSavedDialog");}));
  $("#toolSavedQuery").addEventListener("input",()=>{clearTimeout(savedTimer);savedTimer=setTimeout(()=>void act(renderSaved),180);});
  $("#toolGoList").addEventListener("click",()=>navigate("patients"));
  $("#toolClearPatients").addEventListener("click",()=>act(clearPatients).finally(render));
  for(const id of ["departmentKeyword","soapMode"])$("#"+id).addEventListener("input",render);
  $("#forceReview").addEventListener("change",render);
  for(const id of ["toolEditSetup","toolHeadingToggle"])$("#"+id).addEventListener("click",event=>{event.stopPropagation();return act(toggleSetup);});
  $("#toolStartForm").addEventListener("submit",e=>{e.preventDefault();act(start);});$("#toolRules").addEventListener("click",openRules);
  $("#toolFetchSoap").addEventListener("click",()=>act(async()=>{if(starting)return;requireChecked();if(!soapSetup){soapSetup=true;render();}else{const revision=generation,source=sourceRevision;starting=true;render();try{await startReview(true);}finally{if(revision===generation&&source===sourceRevision){starting=false;render();}}}}));
  return {reset,open,activate,visibility,adoptReview,adoptModule,receivedHandoff,poll,watched:()=>soapTask?[soapTask]:[],refreshSettings:render,beginSettings,commitSettings,cancelSettings,queryOptions,refreshPatient};
})();
