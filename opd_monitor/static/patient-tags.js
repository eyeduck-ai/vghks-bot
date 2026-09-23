"use strict";

// Manual membership is attached to an MRN; SOAP-derived matches retain their source.
const tagPane=el("section",undefined,"workspace-pane");tagPane.id="tagsPane";tagPane.hidden=true;
tagPane.setAttribute("aria-labelledby","tagGroupsTitle");
tagPane.innerHTML=[
  '<div class="section-heading"><div><h2 id="tagGroupsTitle">tag 群組</h2><p class="small muted">以病人分組；手動 tag 不依賴 SOAP，重抓病歷仍保留。</p></div><div class="actions"><button id="assignTagNumbers" type="button" class="primary">輸入病歷號加 tag</button><button id="tagDefinitions" type="button">管理 tag 定義</button></div></div>',
  '<form id="tagGroupSearch" class="library-filters"><label>tag<select id="tagGroupFilter"><option value="">全部 tag</option></select></label><label>來源<select id="tagSourceFilter"><option value="">自動與手動</option><option value="manual">有手動標記</option><option value="auto">SOAP 自動命中</option></select></label><label class="fulltext-field">搜尋病人<input id="tagPatientQuery" type="search" maxlength="500" placeholder="姓名、病歷號"></label><button type="submit">搜尋</button></form>',
  '<div class="selection-bar"><label class="check-label"><input id="tagSelectPage" type="checkbox">全選本頁</label><span id="tagSelectedCount" class="small muted"></span><button id="tagSelectedEdit" type="button">勾選病人加／移除 tag</button><button id="tagSelectedAnalysis" type="button">勾選病人 → 分析</button><button id="tagAllAnalysis" type="button">全部篩選結果 → 分析（跨頁）</button></div>',
  '<p id="tagGroupSummary" class="small muted"></p><div class="table-wrap"><table class="patient-table"><caption class="sr-only">tag 群組病人</caption><thead><tr><th scope="col">選取</th><th scope="col">病人／病歷號</th><th scope="col">SOAP 自動 tag</th><th scope="col">手動 tag</th><th scope="col">操作</th></tr></thead><tbody id="tagPatientRows"></tbody></table></div>',
  '<p id="tagGroupEmpty" class="empty-result" hidden>尚無符合的病人。可直接輸入病歷號加入手動 tag。</p><div class="pagination"><button id="tagGroupPrevious" type="button">上一頁</button><span id="tagGroupPage" class="small muted"></span><button id="tagGroupNext" type="button">下一頁</button></div>'
].join("");
$("#analysisPane").before(tagPane);
const tagNav=button("tag 群組",()=>action(async()=>{showPane("tags");await loadTagGroups();}));
tagNav.dataset.pane="tags";tagNav.setAttribute("aria-pressed","false");
$('.workspace-nav [data-pane="analysis"]').before(tagNav);
document.body.insertAdjacentHTML("beforeend",[
  '<dialog id="patientTagDialog" aria-labelledby="patientTagTitle"><form id="patientTagForm" method="post"><div class="dialog-heading"><h2 id="patientTagTitle">病人手動 tag</h2><button id="closePatientTags" type="button" aria-label="關閉手動 tag">×</button></div><div class="record-body">',
  '<label>病歷號（每行一筆）<textarea id="tagMRNs" name="mrns" rows="4" maxlength="680000" required spellcheck="false" autocomplete="off"></textarea></label><p id="currentPatientTags" class="small muted"></p>',
  '<fieldset class="patient-tag-options"><legend>選擇 tag</legend><div id="patientTagOptions"></div></fieldset><label>新增 tag 名稱（選填）<input id="newPatientTagName" name="new_tag" maxlength="40" placeholder="例如：術後追蹤、研究候選"></label>',
  '<p class="small muted">新增名稱會建立不需關鍵字的 tag。移除只解除手動標記；SOAP 自動命中仍會保留。</p><p id="patientTagError" class="form-error" role="alert" hidden></p><div class="dialog-actions"><button type="submit" name="operation" value="remove">移除手動 tag</button><button type="submit" name="operation" value="add" class="primary">加入 tag</button></div></div></form></dialog>'
].join(""));
const recordTagArea=el("div");recordTagArea.id="recordManualTags";$("#recordExcerpts").before(recordTagArea);
const listTagButton=button("勾選病人加 tag",()=>action(()=>{
  const mrns=(listing?.days.flatMap(d=>d.rows)||[]).filter(r=>selectedPatients.has(rowKey(r))).map(r=>r.mrn);
  if(!mrns.length)throw new Error("請先勾選病人。");return openPatientTags(mrns);
}));$("#listToAnalysis").after(listTagButton);
$("#libraryPane .analysis-entry").append(button("勾選病人加 tag",()=>action(()=>{
  if(!selectedRecords.size)throw new Error("請先勾選病歷。");
  return openPatientTags([...selectedRecords].map(id=>selectedRecordMrns.get(id)).filter(Boolean));
})));
$("#analysisPane .analysis-view-toolbar").append(button("此病人加／移除 tag",()=>action(()=>{
  const mrn=$("#analysisPatient").value;if(!mrn)throw new Error("請先選擇病人。");return openPatientTags([mrn]);
})));
$("#analysisPane .analysis-controls").append(button("整份分析清單加 tag",()=>action(()=>{
  if(!currentCohort)throw new Error("請先選擇分析清單。");return openPatientTags(currentCohort.members.map(m=>m.mrn));
})));
$("#recordDialog .version-bar").append(button("此病人加／移除 tag",()=>action(()=>{
  if(!openedRecord)throw new Error("請從病歷資料庫開啟病歷。");return openPatientTags([openedRecord.mrn]);
})));

let tagGroupData=null,tagOffset=0,tagRequest=0,tagSearchTimer;
const tagSelected=new Set();
function patientTagBadges(tags,source) {
  const group=el("div",undefined,"patient-tag-badges");
  for(const tag of tags)group.append(el("span",source+" · "+tag.name,"patient-tag-badge"+(source==="手動"?" manual":"")));
  return group;
}
function tagGroupFilters(){return {tag:$("#tagGroupFilter").value,source:$("#tagSourceFilter").value,
  q:$("#tagPatientQuery").value,offset:tagOffset,limit:40};}
async function loadTagGroups(reset=false) {
  if(reset){tagOffset=0;tagSelected.clear();}
  if(!config.settings.categories.some(t=>t.id===$("#tagGroupFilter").value))$("#tagGroupFilter").value="";
  const request=++tagRequest,filters=tagGroupFilters();
  const data=await api("/api/patient-tags/search",filters);
  if(request!==tagRequest||stopped)return;
  tagGroupData=data;
  config.settings.categories=data.categories;
  if(tagOffset&&tagOffset>=data.total){tagOffset=Math.max(0,Math.floor((data.total-1)/40)*40);await loadTagGroups();return;}
  $("#tagGroupFilter").replaceChildren(new Option("全部 tag",""),...data.categories.map(t=>new Option(t.name+"（"+(data.tag_counts[t.id]||0)+" 位）",t.id)));
  if(data.categories.some(t=>t.id===filters.tag))$("#tagGroupFilter").value=filters.tag;
  $("#tagPatientRows").replaceChildren(...data.patients.map(patient=>{
    const tr=el("tr"),check=el("input"),selection=el("td"),name=el("td"),automatic=el("td"),manual=el("td"),actions=el("td");
    check.type="checkbox";check.checked=tagSelected.has(patient.mrn);check.setAttribute("aria-label","選取群組病人 "+patient.mrn);
    check.addEventListener("change",()=>{if(check.checked)tagSelected.add(patient.mrn);else tagSelected.delete(patient.mrn);renderTagSelection();});
    selection.append(check);name.append(el("strong",patient.name||"姓名未提供"),el("span",patient.mrn,"small muted"));
    automatic.append(patient.auto_tags.length?patientTagBadges(patient.auto_tags,"自動"):el("span","—","muted"));
    manual.append(patient.manual_tags.length?patientTagBadges(patient.manual_tags,"手動"):el("span","—","muted"));
    actions.append(button("編輯 tag",()=>action(()=>openPatientTags([patient.mrn],$("#tagGroupFilter").value))));
    tr.append(selection,name,automatic,manual,actions);return tr;
  }));
  $("#tagGroupSummary").textContent=data.total+" 位病人 · 相同病歷號合併顯示";
  $("#tagGroupEmpty").hidden=!!data.total;
  $("#tagGroupPrevious").disabled=tagOffset===0;$("#tagGroupNext").disabled=tagOffset+40>=data.total;
  $("#tagGroupPage").textContent="第 "+(Math.floor(tagOffset/40)+1)+" / "+Math.max(1,Math.ceil(data.total/40))+" 頁";
  $("#tagAllAnalysis").disabled=!data.total;renderTagSelection();
}
function renderTagSelection() {
  const rows=tagGroupData?.patients||[],n=rows.filter(r=>tagSelected.has(r.mrn)).length;
  $("#tagSelectedCount").textContent="已勾選 "+tagSelected.size+" 位（跨頁保留）";
  $("#tagSelectPage").checked=!!rows.length&&n===rows.length;$("#tagSelectPage").indeterminate=n>0&&n<rows.length;
  $("#tagSelectPage").disabled=!rows.length;
  for(const id of ["tagSelectedEdit","tagSelectedAnalysis"])$("#"+id).disabled=!tagSelected.size;
}
async function openPatientTags(mrns=[],selectedTag="") {
  const unique=[...new Set(mrns)];
  const data=await api("/api/patient-tags/search",{mrn:unique.length===1?unique[0]:"",limit:1});
  config.settings.categories=data.categories;
  $("#tagMRNs").value=unique.join("\n");$("#newPatientTagName").value="";$("#patientTagError").hidden=true;
  const current=unique.length===1?data.patients.find(p=>p.mrn===unique[0]):null;
  $("#currentPatientTags").textContent=current?
    "目前手動："+(current.manual_tags.map(t=>t.name).join("、")||"無")+"；SOAP 自動："+(current.auto_tags.map(t=>t.name).join("、")||"無"):
    "以病歷號保存手動標記，不需要先抓 SOAP。";
  $("#patientTagOptions").replaceChildren(...data.categories.map(tag=>{
    const label=el("label",undefined,"check-label"),input=el("input");input.type="checkbox";input.value=tag.id;input.name="tag_id";input.checked=tag.id===selectedTag;
    label.append(input,el("span",tag.name+(tag.keywords.length?"":"（手動）")));return label;
  }));
  $("#patientTagDialog").showModal();
  if(!unique.length)$("#tagMRNs").focus();
}
$("#patientTagForm").addEventListener("submit",async event=>{
  event.preventDefault();const operation=event.submitter?.value||"add",controls=$$('button[type="submit"]',event.currentTarget);
  controls.forEach(n=>n.disabled=true);$("#patientTagError").hidden=true;
  try {
    const result=await api("/api/patient-tags/update",{operation,mrns:$("#tagMRNs").value,
      tag_ids:$$('input[name="tag_id"]:checked',$("#patientTagForm")).map(n=>n.value),new_tag:$("#newPatientTagName").value});
    config.settings=result.settings;$("#patientTagDialog").close();
    await loadLibrary();await loadList();await loadTagGroups(true);
    if($("#recordDialog").open&&openedRecord)await openSavedRecord(openedRecord.id);
    notify((operation==="add"?"已加入":"已移除")+" "+result.changed+" 個手動標記；"+result.patients+" 位病人。");
  } catch(error){$("#patientTagError").textContent=error.message;$("#patientTagError").hidden=false;}
  finally {controls.forEach(n=>n.disabled=false);}
});
$("#closePatientTags").addEventListener("click",()=>$("#patientTagDialog").close());
$("#assignTagNumbers").addEventListener("click",()=>action(()=>openPatientTags([],$("#tagGroupFilter").value)));
$("#tagDefinitions").addEventListener("click",openSettings);
$("#tagSelectedEdit").addEventListener("click",()=>action(()=>openPatientTags([...tagSelected],$("#tagGroupFilter").value)));
$("#tagSelectedAnalysis").addEventListener("click",()=>action(()=>beginCohort("tags")));
$("#tagAllAnalysis").addEventListener("click",()=>action(()=>beginCohort("tags-all")));
$("#tagGroupSearch").addEventListener("submit",event=>{event.preventDefault();clearTimeout(tagSearchTimer);action(()=>loadTagGroups(true));});
for(const id of ["tagGroupFilter","tagSourceFilter"])$("#"+id).addEventListener("change",()=>action(()=>loadTagGroups(true)));
$("#tagPatientQuery").addEventListener("input",event=>{if(event.isComposing)return;clearTimeout(tagSearchTimer);tagSearchTimer=setTimeout(()=>action(()=>loadTagGroups(true)),350);});
$("#tagSelectPage").addEventListener("change",event=>{
  for(const patient of tagGroupData?.patients||[]){if(event.target.checked)tagSelected.add(patient.mrn);else tagSelected.delete(patient.mrn);}
  $$('#tagPatientRows input[type="checkbox"]').forEach(n=>n.checked=event.target.checked);renderTagSelection();
});
for(const [id,delta] of [["tagGroupPrevious",-40],["tagGroupNext",40]])$("#"+id).addEventListener("click",()=>action(async()=>{tagOffset=Math.max(0,tagOffset+delta);await loadTagGroups();}));
