"use strict";
window.ToolWorkspace=(()=>{
  const names={review:"病歷檢閱",retina:"視網膜比較",cataract:"白內障術前比較",surgery:"刀表更新"};
  const retrieval={cache:"重用已存病歷",refresh:"檢查新紀錄",force:"重新下載病歷"};
  let kind="review",states={},loaded=false,group=null,generation=0;
  let resolveId="",resolvedPatient=null,resolveSignature="",soapSetup=false,soapTask="",soapSetId="";
  let setupOpen=true,activeTask=null,resultKind="",starting=false;
  let scopeKey="",scopeRevision=0,scopeData=null,scopePending=false,scopeError="";
  let settingsDraft=null;
  const settingIds=["departmentFilter","department","soapMode","cutoffMode","cutoffDate","reviewRefresh"];
  const settingValues=()=>Object.fromEntries(settingIds.map(id=>[id,$("#"+id).value]));
  function queryOptions(){return {department_filter:$("#departmentFilter").value,department:$("#department").value,mode:$("#soapMode").value,cutoff_mode:$("#cutoffMode").value,cutoff:$("#cutoffDate").value,retrieval:$("#reviewRefresh").value};}
  function loadOptions(){
    const values=work?.preferences.review_options||{};
    for(const [id,key,fallback] of [["departmentFilter","department_filter","same"],["soapMode","mode","latest"],["cutoffMode","cutoff_mode","today"],["reviewRefresh","retrieval","cache"]])$("#"+id).value=values[key]||fallback;
    if([...$("#department").options].some(o=>o.value===values.department))$("#department").value=values.department;
    if(values.cutoff_mode==="date"&&values.cutoff)$("#cutoffDate").value=values.cutoff;
    scopeChanged();
  }
  function beginSettings(){settingsDraft=settingValues();$("#reviewAdvancedSettings").open=false;$("#tagSettings").open=false;$("#departmentSettings").open=false;render();}
  function commitSettings(){const changed=settingsDraft&&JSON.stringify(settingsDraft)!==JSON.stringify(settingValues());settingsDraft=null;if(changed&&activeTask)setupOpen=true;render();}
  function cancelSettings(){if(settingsDraft){for(const [id,value] of Object.entries(settingsDraft))$("#"+id).value=value;settingsDraft=null;scopeChanged();}}
  function reset(){
    generation++;scopeRevision++;soapSetup=false;soapTask=soapSetId="";$("#toolSoapProgress").replaceChildren();
    kind="review";states={};loaded=false;group=null;resolveId="";resolvedPatient=null;resolveSignature="";
    setupOpen=true;activeTask=null;resultKind="";starting=false;scopeKey="";scopeData=null;scopePending=false;scopeError="";
    $("#department").replaceChildren();$("#departmentConfirmed").checked=false;
    settingsDraft=null;$("#departmentFilter").value="same";$("#soapMode").value="latest";$("#cutoffMode").value="today";$("#reviewRefresh").value="cache";
    $("#toolIdentifier").value="";$("#toolResolveResult").replaceChildren();visibility("patients");
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
  async function save(){if(root.read_only)return;states[kind]={set_id:group?.id||""};await api("/tools/state/save",{module:kind,set_id:group?.id||""});}
  async function choose(value){group=value;selectedSet=value;await save();render();}
  function syncScope(){
    const needed=group&&(kind==="review"||kind==="surgery"&&soapSetup)&&$("#soapMode").value!=="registration"&&$("#departmentFilter").value!=="all";
    const key=needed?JSON.stringify([group.id,$("#department").value,work?.preferences.departments]):"";
    if(key!==scopeKey){
      scopeKey=key;scopeData=null;scopeError="";scopePending=!!key;$("#departmentConfirmed").checked=false;
      const revision=++scopeRevision;
      if(key)api("/reviews/scope",{set_id:group.id,department:$("#department").value}).then(value=>{
        if(revision!==scopeRevision)return;scopeData=value;scopePending=false;render();
      }).catch(error=>{if(revision!==scopeRevision||error.name==="AbortError")return;scopePending=false;scopeError=error.message;render();});
    }
    const confirm=!!scopeData?.needs_confirmation&&!!needed;
    $("#departmentWarning").hidden=!confirm&&!scopeError&&!scopePending;
    $("#departmentConfirmed").disabled=!confirm;$("#departmentConfirmed").required=confirm;
    $("#departmentConfirmed").parentElement.hidden=!confirm;
    $("#departmentReason").textContent=scopePending?"正在核對本機科別設定…":scopeError||
      (confirm?"來源科別尚未對應："+scopeData.unmatched.map(s=>[s.name,s.code].filter(Boolean).join("／")||"未提供科別").join("、")+"。可在下方「科別群組對應」補上對應，或選擇不限科別。":"");
    $("#departmentConfirmLabel").textContent=confirm?"本次仍以「"+scopeData.department.name+"」範圍尋找歷史 SOAP":"";
  }
  function scopeChanged(){scopeKey="";scopeRevision++;scopeData=null;scopePending=false;scopeError="";$("#departmentConfirmed").checked=false;render();}
  function render(){
    const isReview=kind==="review",hasResult=!!activeTask||resultKind===kind;
    $("#toolKind").value=kind;toolOptions();
    $("#toolReviewOptions").hidden=!(isReview||kind==="surgery"&&soapSetup);$("#reviewOptions").hidden=$("#toolReviewOptions").hidden;
    $("#cutoffDate").disabled=$("#reviewOptions").hidden||$("#cutoffField").hidden;$("#cutoffDate").required=!$("#cutoffDate").disabled;
    $("#toolPage").firstElementChild.hidden=!!group;
    $("#toolSoapProgress").hidden=kind!=="surgery"||group?.id!==soapSetId;
    $("#toolSelectedCount").textContent=!setupOpen&&activeTask?activeTask.members.length+" 位病人":group?group.members.length+" 位病人":"尚未選擇病人";
    $("#toolSelectionSummary").textContent=group?"已選："+group.members.slice(0,3).map(p=>(p.name||"姓名未提供")+"（"+p.mrn+"）").join("、")+(group.members.length>3?"…":""):"";
    $("#toolSetupBody").hidden=!setupOpen;$("#toolSourcePanel").classList.toggle("setup-collapsed",!setupOpen);
    $("#toolEditSetup").hidden=!hasResult;setIcon($("#toolEditSetup"),"patients",setupOpen?"收起病人來源":"更換病人");$("#toolEditSetup").setAttribute("aria-expanded",String(setupOpen));
    $("#toolRunSummary").hidden=setupOpen;
    $("#toolRunSummary").textContent=activeTask?(activeTask.mode==="registration"?"掛號當次":activeTask.department.name+" · 最新")+" · 截至 "+activeTask.cutoff+" · "+retrieval[activeTask.force?"force":activeTask.refresh?"refresh":"cache"]:group?.name||"";
    const all=$("#departmentFilter").value==="all",department=$("#department").selectedOptions[0]?.text||"同科別";
    $("#toolSettingsSummary").hidden=!(isReview||kind==="surgery"&&soapSetup);
    $("#toolSettingsSummary").textContent=[$("#soapMode").value==="registration"?"掛號當次":(all?"不限科別":department)+" · 最新",$("#cutoffMode").value==="today"?"截至今天":"截至 "+$("#cutoffDate").value,retrieval[$("#reviewRefresh").value]].join(" · ");
    $("#toolNewReviewHint").hidden=!isReview||!activeTask;
    syncScope();
    $("#toolStart").textContent=isReview?(starting?"正在建立檢閱…":activeTask?"開始新檢閱":"開始檢閱"):"開啟"+names[kind];
    $("#toolStart").disabled=starting||!group||root.read_only||(isReview&&(!work?.online||scopePending||!!scopeError));
    $("#toolResolve").disabled=root.read_only||!work?.online||!!resolveId;
    $("#toolFetchSoap").hidden=kind!=="surgery";$("#toolFetchSoap").textContent=soapSetup?"開始取得／更新 SOAP":"取得／更新此集合 SOAP";
    $("#toolFetchSoap").disabled=starting||!group||!work?.online||root.read_only||(soapSetup&&(scopePending||!!scopeError));
    $("#toolRules").hidden=!(isReview||kind==="surgery"&&soapSetup);$("#toolRules").disabled=root.read_only;
    if(scopeData?.needs_confirmation&&!$("#departmentConfirmed").checked)$("#toolSettingsSummary").textContent+=" · 請開啟檢閱設定核對科別";
    Choices.sync();
  }
  async function open(value,selectedGroup=null){
    const revision=++generation;soapSetup=false;activeTask=null;resultKind="";setupOpen=true;activate(value);
    if(!loaded){const data=await api("/tools/state",{});if(revision!==generation)return;states=data.modules;loaded=true;}
    await refresh();if(revision!==generation)return;
    // A refresh may deliver the previous task while the new workspace is opening.
    activeTask=null;resultKind="";setupOpen=true;
    group=selectedGroup||(work.sets||[]).find(s=>s.id===states[value]?.set_id)||null;selectedSet=group;
    resolveId="";resolvedPatient=null;resolveSignature="";$("#toolResolveResult").replaceChildren();
    $("#toolIdentifier").value="";
    toolDepartments();$("#cutoffDate").max=root.today;$("#cutoffDate").value=[group?.source_range?.end||root.today,root.today].sort()[0];
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
      if(!$("#department").options.length)toolDepartments();
      if([...$("#department").options].some(o=>o.value===task.department.id))$("#department").value=task.department.id;
      $("#departmentFilter").value=task.department_filter||"same";
      $("#soapMode").value=task.mode;$("#cutoffMode").value=task.cutoff===root.today?"today":"date";
      $("#cutoffDate").max=root.today;$("#cutoffDate").value=task.cutoff;$("#reviewRefresh").value=task.force?"force":task.refresh?"refresh":"cache";
      setupOpen=false;
    }
    // Polling updates the displayed snapshot, never the editable next-task draft.
    activeTask=task;render();
  }
  async function startReview(inline=false){
    if(!group)throw new Error("請先選擇病人。");
    if(scopePending||scopeError)throw new Error(scopeError||"科別設定仍在核對中。");
    if(scopeData?.needs_confirmation&&!$("#departmentConfirmed").checked){openRules();return;}
    if($("#cutoffMode").value==="date"&&!$("#cutoffDate").checkValidity()){openRules();$("#reviewAdvancedSettings").open=true;$("#cutoffDate").reportValidity();return;}
    if($("#soapMode").value==="registration"&&group.members.some(p=>!p.registrations?.length))throw new Error("此集合包含沒有掛號來源的病人，請使用最新同科模式。");
    const revision=generation,selected=group,refreshMode=$("#reviewRefresh").value;
    const result=await api("/tasks/start",{kind:"review",set_id:selected.id,department:$("#department").value,department_filter:$("#departmentFilter").value,
      department_confirmed:$("#departmentConfirmed").checked,department_confirmation:scopeData?.confirmation_key||"",
      mode:$("#soapMode").value,cutoff:$("#cutoffMode").value==="date"?$("#cutoffDate").value:root.today,
      refresh:refreshMode!=="cache",force:refreshMode==="force"});
    if(revision!==generation)return;
    if(inline){soapTask=result.task_id;soapSetId=selected.id;soapSetup=false;$("#toolSoapProgress").replaceChildren(node("p","正在取得此集合 SOAP…","caption"));render();}
    else{await openReview(result.task_id);if(revision!==generation)return;$("#reviewTitle").focus({preventScroll:true});$("#toolSourcePanel").scrollIntoView({block:"start"});}
    await refresh();
  }
  async function start(){
    if(starting)return;if(!group)throw new Error("請先選擇病人。");
    const revision=generation,selected=group,module=kind;starting=true;render();
    try{
      await save();if(revision!==generation)return;if(module==="review")return await startReview();
      const result=await api("/sets/cohort",{set_id:selected.id});if(revision!==generation)return;await openModule(result.id,module);render();
    }finally{if(revision===generation){starting=false;render();}}
  }
  async function poll(){
    if(soapTask){const id=soapTask,revision=generation,task=await api("/tasks/detail?id="+id);if(id!==soapTask||revision!==generation)return;JobProgress.show("#toolSoapProgress",task);if(!inProgress.has(task.status)){soapTask="";if(task.status!=="completed")$("#toolSoapProgress").append(node("p",task.message+"；可由任務紀錄續跑。","error"));else if(kind==="surgery"&&group?.id===soapSetId){say("SOAP 已保存，可重新整理手術候選。");$("#moduleFrame").contentWindow?.postMessage({type:"bot:soap-updated"},location.origin);}}}
    if(!resolveId)return;const id=resolveId,revision=generation,task=await api("/tasks/detail?id="+id);if(id!==resolveId||revision!==generation)return;
    const signature=JSON.stringify([task.status,task.items]);if(signature===resolveSignature)return;resolveSignature=signature;const area=$("#toolResolveResult");area.replaceChildren(JobProgress.create(task));
    if(!inProgress.has(task.status)){
      resolveId="";resolvedPatient=task.items.find(p=>p.status==="resolved");
      if(resolvedPatient){const p=resolvedPatient;area.append(node("strong",(p.name||p.mrn)+" · "+p.mrn+" · "+(p.sex||"—")+" · "+(p.birthday||p.age||"—")),btn("使用此病人",async()=>{const revision=generation,set=await api("/sets/save",{mrns:p.mrn});await refresh();if(revision!==generation)return;await choose(set);area.replaceChildren();$("#toolIdentifier").value="";}));}
      else{area.append(node("p",task.items[0]?.message||task.message||"病人解析失敗","error"),btn("重試",async()=>{await api("/tasks/start",{resume:task.id});resolveId=task.id;resolveSignature="";await poll();}));}
      render();
    }
  }
  function renderSaved(){
    const query=$("#toolSavedQuery").value.trim().toLocaleLowerCase(),area=$("#toolSavedSets");
    const groups=(work?.sets||[]).filter(s=>[s.name,...s.members.map(p=>p.mrn)].join(" ").toLocaleLowerCase().includes(query));
    area.replaceChildren(...groups.map(s=>{const button=btn(s.name,async()=>{generation++;resolveId="";resolvedPatient=null;$("#toolResolveResult").replaceChildren();await choose(s);$("#toolSavedDialog").close();},"saved-set-option");button.replaceChildren(node("strong",s.name),node("span",s.members.length+" 位 · "+s.members.slice(0,3).map(p=>p.name||p.mrn).join("、"),"caption"));return button;}));
    if(!groups.length)area.append(node("p","沒有符合的已保存清單","empty"));
  }
  for(const button of $$("[data-tool]"))button.addEventListener("click",()=>act(()=>open(button.dataset.tool)));
  for(const button of $$("[data-use-tool]"))button.addEventListener("click",()=>act(async()=>{await open(button.dataset.useTool,await saveCurrentSet());}));
  $("#toolOpenSaved").addEventListener("click",()=>act(async()=>{$("#toolSavedQuery").value="";await refresh();renderSaved();dialog("toolSavedDialog");}));
  $("#toolSavedQuery").addEventListener("input",renderSaved);
  $("#toolGoList").addEventListener("click",()=>navigate("patients"));
  for(const id of ["department","soapMode","departmentFilter"])$("#"+id).addEventListener("change",scopeChanged);
  for(const id of ["cutoffMode","cutoffDate","reviewRefresh"])$("#"+id).addEventListener("change",render);
  $("#toolEditSetup").addEventListener("click",()=>{setupOpen=!setupOpen;render();if(setupOpen)$("#toolSourcePanel").scrollIntoView({block:"start"});});
  $("#toolIdentityForm").addEventListener("submit",e=>{e.preventDefault();act(async()=>{
    const text=$("#toolIdentifier").value.trim();if(!text||/[\s,;，；]/.test(text))throw new Error("此處請輸入一筆病歷號或身分證字號；批次請使用選擇病人清單。");
    const revision=++generation;resolveId="";resolvedPatient=null;group=null;selectedSet=null;render();$("#toolResolveResult").replaceChildren(node("p","正在核對病人…","caption"));
    const task=await api("/tasks/start",{kind:"resolve",identifier_kind:"auto",identifiers:text});if(revision!==generation)return;resolveId=task.task_id;resolveSignature="";render();await poll();
  });});
  $("#toolStartForm").addEventListener("submit",e=>{e.preventDefault();act(start);});$("#toolRules").addEventListener("click",openRules);
  $("#toolFetchSoap").addEventListener("click",()=>act(async()=>{if(!soapSetup){soapSetup=true;toolDepartments();$("#cutoffDate").max=$("#cutoffDate").value=root.today;render();}else await startReview(true);}));
  return {reset,open,activate,visibility,adoptReview,adoptModule,poll,scopeChanged,refreshSettings:render,beginSettings,commitSettings,cancelSettings,queryOptions};
})();
