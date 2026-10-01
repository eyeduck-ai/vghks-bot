"use strict";

let activePane = "list", listRange = null, listing = null, listRequest = 0, listSelectionKey = "";
let selectedPatients = new Set(), libraryData = null, libraryOffset = 0, libraryRequest = 0;
let selectedRecords = new Set(), openedRecord = null, workspaceRevision = "", listSignature = "", searchTimer;
const selectedRecordMrns = new Map();
let listLoading = false, pendingListBrowse = false;
const rowKey = row => `${row.day}:${row.id}`;

function showPane(name) {
  activePane = name;
  for (const item of ["list", "library", "analysis", "history", "tags"]) {const pane=$("#" + item + "Pane");if(pane)pane.hidden = item !== name;}
  $$('[data-pane]').forEach(node => node.setAttribute("aria-pressed", String(node.dataset.pane === name)));
}

function syncWorkspaceAccounts() {
  const select = $("#listAccount"), previous = select.value;
  select.replaceChildren(...[...accounts.values()].map(a => new Option(a.label ? `${a.label} · ${a.username || "未設定"}` : a.username || "未設定帳號", a.id)));
  if (accounts.has(previous)) select.value = previous;
}

async function useListAccount(key) {
  pendingListBrowse = false;
  syncWorkspaceAccounts();
  $("#listAccount").value = key;
  const account = accounts.get(key);
  if (!account) return;
  $("#listDate").value = account.start;
  listRange = {start: account.start, end: account.mode === "range" ? account.end : account.start};
  await loadList();
}

async function loadList() {
  const key = $("#listAccount").value, account = accounts.get(key);
  if (!account?.username || !listRange) {
    listing = null;
    renderPatientList();
    return;
  }
  const request = ++listRequest, range = {...listRange};
  listLoading = true;
  updatePatientSelection();
  let data;
  try { data = await api("/api/lists", {account_id:key, ...range}); }
  finally { if (request === listRequest) { listLoading = false; updatePatientSelection(); } }
  if (stopped || request !== listRequest || key !== $("#listAccount").value) return;
  listing = data;
  const selectionKey = `${key}:${range.start}:${range.end}`;
  const rows = data.days.flatMap(day => day.rows);
  const signature = JSON.stringify(data);
  if (selectionKey !== listSelectionKey || (!listSignature && rows.length)) {
    selectedPatients = new Set(rows.filter(r => r.eligible).map(rowKey));
  } else if (signature !== listSignature) {
    // Preserve explicit deselections; select newly received rows by default.
    for (const row of rows) if (row.eligible && !knownPatientRows.has(rowKey(row))) selectedPatients.add(rowKey(row));
    for (const id of selectedPatients) if (!rows.some(row => rowKey(row) === id)) selectedPatients.delete(id);
  }
  knownPatientRows = new Set(rows.map(rowKey));
  listSelectionKey = selectionKey;
  listSignature = signature;
  renderPatientList();
}
let knownPatientRows = new Set();

function visiblePatients() {
  const query = $("#listSearch").value.trim().toLocaleLowerCase();
  return (listing?.days.flatMap(day => day.rows) || []).filter(row => !query || [row.name,row.mrn,row.sequence_no,row.sex,row.age,row.section_code,row.room,row.day].join(" ").toLocaleLowerCase().includes(query));
}

function renderPatientList() {
  const rows = visiblePatients();
  const nodes = rows.map(row => {
    const tr = el("tr"), selectCell = el("td"), checkbox = el("input");
    checkbox.type = "checkbox"; checkbox.checked = selectedPatients.has(rowKey(row)); checkbox.disabled = !row.eligible;
    checkbox.setAttribute("aria-label", `選取 ${row.name || row.mrn} ${row.day} 掛號序號 ${row.sequence_no || "未提供"}`);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selectedPatients.add(rowKey(row)); else selectedPatients.delete(rowKey(row));
      updatePatientSelection();
    });
    selectCell.append(checkbox);
    const patient = el("td"); patient.append(el("strong", row.name || "姓名未提供"), el("span", row.mrn, "muted small"));
    if(row.manual_tags?.length)patient.append(patientTagBadges(row.manual_tags,"手動"));
    const saved = el("td");
    if (row.record_ids.length) saved.append(button(`${row.status} · ${row.record_ids.length} 筆`, () => action(() => openSavedRecord(row.record_ids[0])), "quiet-button"));
    else saved.append(el("span", row.status, row.saved ? "muted" : ""));
    tr.append(selectCell,el("td",row.day),el("td",row.sequence_no || "—"),patient,el("td",[row.sex,row.age].filter(Boolean).join(" / ")),el("td",[row.section_code,row.room].filter(Boolean).join(" / ")),saved);
    return tr;
  });
  $("#patientRows").replaceChildren(...nodes);
  $("#listEmpty").hidden = rows.length > 0;
  const missing = listing?.days.filter(day => !day.cached).length || 0;
  $("#listEmpty").textContent = !listing ? "選擇帳號查看門診清單" : missing ? "此日期尚無快取，請按「重新整理清單」" : $("#listSearch").value ? "沒有符合的病人" : "此日期沒有門診病人";
  if (listing) {
    const total = listing.days.reduce((n,d) => n + d.rows.length, 0);
    const fetched = listing.days.map(d => d.fetched_at).filter(Boolean).sort().at(-1);
    $("#listContext").textContent = `${listing.account} · ${rangeLabel(listing)} · ${total} 筆${missing ? ` · ${missing} 天尚未取得` : " · 本機快取"}${fetched ? ` · 更新 ${fetched.slice(5,16).replace("T"," ")}` : ""}`;
  } else $("#listContext").textContent = "選擇帳號與日期";
  updatePatientSelection();
}

function updatePatientSelection() {
  const visible = visiblePatients().filter(r => r.eligible);
  const count = visible.filter(r => selectedPatients.has(rowKey(r))).length;
  $("#selectAllPatients").checked = !!visible.length && count === visible.length;
  $("#selectAllPatients").indeterminate = count > 0 && count < visible.length;
  $("#selectAllPatients").disabled = !visible.length;
  $("#patientSelection").textContent = `已勾選 ${selectedPatients.size} 筆${selectedPatients.size > count ? "（包含未顯示項目）" : ""}`;
  const busy = history.some(r => r.account_id === $("#listAccount").value && active.has(r.status));
  const chosen = (listing?.days.flatMap(d=>d.rows)||[]).filter(r=>selectedPatients.has(rowKey(r)));
  const soapCount = chosen.filter(r=>r.soap_eligible).length;
  $("#fetchPatients").disabled = !soapCount || busy || listLoading;
  $("#fetchPatients").textContent = soapCount < chosen.length ? "抓取已到診日 SOAP（"+soapCount+" 筆）" : "抓取並保存 SOAP";
  $("#listToAnalysis").disabled = !chosen.length || listLoading;
  $("#futureListHint").hidden = !listing?.days.some(d=>d.day>config.today);
  $("#refreshList").disabled = busy;
  $("#clearList").disabled = history.some(r => active.has(r.status)) || !listing?.days.some(d => d.cached);
}

async function browseList(force = false) {
  const key = $("#listAccount").value;
  const form = $$(".account-card").find(f => f.dataset.id === key);
  if (!form || !listRange) return;
  await saveRows([form]);
  const result = await api("/api/lists/browse", {account_ids:[key], ranges:{[key]:listRange}, force});
  selectedRun = result.run_ids[0]; current = null;
  await refresh();
}

async function changeListDate(day) {
  if (!day || !$("#listDate").checkValidity()) return;
  listRange = {start:day,end:day};
  await loadList();
  pendingListBrowse = false;
  if (!listing) return;
  if (history.some(r => r.account_id === listing.account_id && active.has(r.status))) {
    pendingListBrowse = !listing.days.every(day => day.fresh);
    return;
  }
  // The server decides cache freshness; this call needs no login for cached days.
  if (!listing.days.every(day => day.fresh)) await browseList(false);
}

async function confirmDelete(message) {
  const dialog = $("#confirmDialog");
  $("#confirmMessage").textContent = message;
  dialog.returnValue = "cancel";
  return new Promise(resolve => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), {once:true});
    dialog.showModal();
  });
}

function libraryFilters() {
  return {q:$("#libraryQuery").value, account:$("#libraryAccount").value,
    start:$("#libraryStart").value, end:$("#libraryEnd").value,
    tag:$("#libraryTag").value, group:$("#libraryGroup").value, offset:libraryOffset, limit:40};
}

async function loadLibrary(reset = false) {
  if (reset) { libraryOffset = 0; selectedRecords.clear(); selectedRecordMrns.clear(); }
  const tag = $("#libraryTag").value;
  if (!["", "__tagged", "__untagged", ...config.settings.categories.map(c => c.id)].includes(tag)) $("#libraryTag").value = "";
  const request = ++libraryRequest;
  const data = await api("/api/library/search", libraryFilters());
  if (stopped || request !== libraryRequest) return;
  libraryData = data;
  for(const record of data.records)selectedRecordMrns.set(record.id,record.mrn);
  if (libraryOffset > 0 && libraryOffset >= data.page_total) {
    libraryOffset = Math.max(0, Math.floor((data.page_total-1)/data.limit)*data.limit);
    await loadLibrary();
    return;
  }
  $("#corpusSummary").textContent = `${data.stats.patients} 位病人 · ${data.stats.records} 筆 SOAP`;
  const account = $("#libraryAccount").value;
  $("#libraryAccount").replaceChildren(new Option("全部帳號", ""), ...data.stats.accounts.map(name => new Option(name,name)));
  if (data.stats.accounts.includes(account)) $("#libraryAccount").value = account;
  const selectedTag = $("#libraryTag").value;
  $("#libraryTag").replaceChildren(new Option("全部 tag 與未分類", ""), new Option("有 tag", "__tagged"),new Option(`無 tag（${data.untagged}）`, "__untagged"), ...data.categories.map(c => new Option(`${c.name}（${data.tag_counts[c.id] || 0}）`, c.id)));
  if (["", "__tagged", "__untagged", ...data.categories.map(c => c.id)].includes(selectedTag)) $("#libraryTag").value = selectedTag;
  renderLibrary();
}

function corpusCard(record, tag = "") {
  const card = patientCard(record, tag);
  $(".card-footer button", card).replaceWith(button("段落與 SOAP ↗", () => action(() => openSavedRecord(record.id))));
  const label = el("label", undefined, "check-label record-select"), input = el("input");
  input.type = "checkbox"; input.checked = selectedRecords.has(record.id);
  input.setAttribute("aria-label", `勾選病歷 ${record.name || record.mrn} ${record.date} ${record.case_no || ""}`);
  input.addEventListener("change", () => {
    if (input.checked) selectedRecords.add(record.id); else selectedRecords.delete(record.id);
    $$("input[data-record-id]").filter(node => node.dataset.recordId === record.id).forEach(node => { node.checked = input.checked; });
    updateRecordSelection();
  });
  input.dataset.recordId = record.id;
  label.append(input, el("span", `${record.case_no || "就診紀錄"}${record.version_count > 1 ? ` · ${record.version_count} 個版本` : ""}`));
  card.prepend(label);
  return card;
}

function renderLibrary() {
  if (!libraryData) return;
  const data = libraryData, mode = $("#libraryGroup").value, nodes = [];
  $("#librarySummary").textContent = `${data.patients} 位病人 · ${data.total} 筆就診 · 搜尋最新內容`;
  if (mode === "patient") {
    const groups = new Map();
    for (const record of data.records) {
      if (!groups.has(record.mrn)) groups.set(record.mrn, []);
      groups.get(record.mrn).push(record);
    }
    for (const [mrn, records] of groups) {
      const group = el("details", undefined, "patient-group"); group.open = groups.size < 8;
      const tags = [...new Set(records.flatMap(r => [...r.matches.map(m => m.category_name),...(r.manual_tags||[]).map(t=>t.name+"（手動）")]))];
      group.append(el("summary", `${records[0].name || mrn} · ${mrn} · ${records.length} 次就診${tags.length ? ` · ${tags.join("、")}` : " · 無 tag"}`));
      const grid = el("div", undefined, "patient-grid"); grid.append(...records.map(r => corpusCard(r))); group.append(grid); nodes.push(group);
    }
  } else if (mode === "tag") {
    const categories = [...data.categories, {id:"__untagged",name:"無 tag"}];
    for (const category of categories) {
      const records = data.records.filter(r => category.id === "__untagged" ? !r.matches.length&&!r.manual_tags?.length : r.matches.some(m => m.category === category.id)||(r.manual_tags||[]).some(t=>t.id===category.id));
      if (!records.length) continue;
      const group = el("section", undefined, "category-section" + (category.parser === "surgery" ? " surgery" : ""));
      group.append(el("h3", `${category.name} · 本頁 ${records.length} 筆`, "category-heading"));
      const grid = el("div", undefined, "patient-grid"); grid.append(...records.map(r => corpusCard(r, category.id === "__untagged" ? "" : category.id))); group.append(grid); nodes.push(group);
    }
  } else {
    const grid = el("div", undefined, "patient-grid"); grid.append(...data.records.map(r => corpusCard(r))); nodes.push(grid);
  }
  $("#libraryResults").replaceChildren(...nodes);
  $("#libraryEmpty").hidden = !!data.records.length;
  $("#libraryEmpty").textContent = data.stats.records ? "沒有符合條件的病歷" : "尚無已保存病歷；請先從門診清單勾選抓取 SOAP";
  $("#libraryPrevious").disabled = libraryOffset === 0;
  $("#libraryNext").disabled = libraryOffset + data.limit >= data.page_total;
  $("#libraryPage").textContent = `第 ${Math.floor(libraryOffset / data.limit) + 1} / ${Math.max(1, Math.ceil(data.page_total / data.limit))} 頁`;
  $("#libraryJSON").disabled = $("#libraryCSV").disabled = !data.records.length;
  updateRecordSelection();
}

function updateRecordSelection() {
  $("#analysisSelection").textContent = selectedRecords.size ? "已勾選 " + selectedRecords.size + " 筆（跨頁保留）" : "";
  $("#deleteLibrarySelection").disabled = !selectedRecords.size || history.some(r => active.has(r.status));
  $("#deleteLibrarySelection").textContent = selectedRecords.size ? `刪除勾選病歷（${selectedRecords.size}）` : "刪除勾選病歷";
}

async function openSavedRecord(id) {
  openedRecord = await api("/api/library/record", {id});
  renderRecord(openedRecord);
  const versions = openedRecord.versions;
  $("#recordVersion").replaceChildren(new Option("目前內容", "current"), ...versions.map((v,i) => new Option(`${v.saved_at.slice(0,19).replace("T"," ")} · 版本 ${versions.length-i}`, String(i))));
  $("#versionLabel").hidden = versions.length <= 1;
  $("#deleteRecord").hidden = true;
  if (!$("#recordDialog").open) $("#recordDialog").showModal();
}

async function deleteSavedRecords(ids) {
  if (!ids.length || !await confirmDelete(`刪除 ${ids.length} 筆就診的所有 SOAP 版本及作業紀錄中的 SOAP 副本？此操作無法復原。門診清單會保留。`)) return;
  await api("/api/library/delete", {ids});
  $("#recordDialog").close(); openedRecord = null; current = null;
  selectedRecords.clear(); libraryOffset = 0;
  await loadLibrary(); await loadList(); await refresh();
  notify("病歷及歷史 SOAP 副本已刪除");
}

async function refreshWorkspace() {
  if (activePane === "analysis" && typeof refreshAnalysisRuns === "function") await refreshAnalysisRuns();
  const busy = history.filter(r => active.has(r.status));
  $("#activeJobs").replaceChildren(...busy.map(run => {
    const node = el("div", undefined, "running-job");
    const summary=el("span", `${accountLabel(run)} · ${run.kind === "list" ? "門診清單" : "SOAP"} · ${run.message}`);summary.title=summary.textContent;
    node.append(summary,button("明細", () => action(() => selectRun(run.id))),button("停止", () => action(async () => { await api("/api/stop",{id:run.id}); await refresh(); })));
    node.append(JobProgress.create(run));return node;
  }));
  updatePatientSelection(); updateRecordSelection();
  const signature = JSON.stringify(history.filter(r => r.kind !== "analysis").map(r => [r.id,r.revision,r.status]));
  if (signature !== workspaceRevision) {
    workspaceRevision = signature;
    await loadList();
    await loadLibrary();
    if(activePane==="tags")await loadTagGroups();
  }
  if (pendingListBrowse && !busy.some(r => r.account_id === $("#listAccount").value)) {
    pendingListBrowse = false;
    if (listing && !listing.days.every(day => day.fresh)) await browseList();
  }
}

async function initializeWorkspace() {
  if (typeof loadAnalysis === "function") await loadAnalysis();
  syncWorkspaceAccounts();
  $("#listDate").max = "9999-12-31";
  $("#libraryStart").max = $("#libraryEnd").max = config.today;
  const key = $("#listAccount").value;
  if (key) await useListAccount(key);
  await loadLibrary();
}

$$('[data-pane]').forEach(node => node.addEventListener("click", () => action(async () => {
  showPane(node.dataset.pane);
  if (activePane === "library") await loadLibrary();
  if (activePane === "list") await loadList();
  if (activePane === "history") await loadRun();
  if (activePane === "analysis") await loadAnalysis();
})));
$("#listAccount").addEventListener("change", () => action(async () => {
  await useListAccount($("#listAccount").value);
  if (listing && !listing.days.every(day => day.fresh)) await browseList();
}));
$("#listDate").addEventListener("change", () => action(() => changeListDate($("#listDate").value)));
for (const [id,delta] of [["previousDay",-1],["nextDay",1]]) $("#"+id).addEventListener("click", () => action(async () => {
  const input = $("#listDate");
  if (!input.value) return;
  const day = new Date(input.value + "T12:00:00Z"); day.setUTCDate(day.getUTCDate()+delta);
  const value = day.toISOString().slice(0,10);
  if (value.length !== 10 || value < "1912-01-01") return;
  input.value = value; await changeListDate(value);
}));
$("#accountRange").addEventListener("click", () => action(async () => { await useListAccount($("#listAccount").value); await browseList(); }));
$("#refreshList").addEventListener("click", () => action(() => browseList(true)));
$("#clearList").addEventListener("click", () => action(async () => {
  if (!listing || !await confirmDelete(`刪除 ${listing.account} ${rangeLabel(listing)} 的門診清單快取？已保存的 SOAP 不受影響。`)) return;
  await api("/api/lists/delete",{account_id:listing.account_id,...listRange});
  await loadList(); await loadLibrary();
}));
$("#listSearch").addEventListener("input",renderPatientList);
$("#selectAllPatients").addEventListener("change", event => {
  for (const row of visiblePatients().filter(r => r.eligible)) {
    if (event.target.checked) selectedPatients.add(rowKey(row)); else selectedPatients.delete(rowKey(row));
  }
  renderPatientList();
});
$("#fetchPatients").addEventListener("click", () => action(async () => {
  if (!listing) return;
  const rows = listing.days.flatMap(d => d.rows).filter(r => r.soap_eligible && selectedPatients.has(rowKey(r))).map(r => ({id:r.id,day:r.day}));
  if (!rows.length) return;
  const form = $$(".account-card").find(f => f.dataset.id === listing.account_id);
  if (form) await saveRows([form]);
  $("#fetchPatients").disabled = true;
  try {
    const result = await api("/api/fetch", {selections:[{account_id:listing.account_id,rows}],force:$("#forceSoap").checked});
    selectedRun = result.run_ids[0]; current = null;
    $("#accountsDetails").open = false;
    await refresh();
    notify("已加入 SOAP 抓取佇列");
  } finally { updatePatientSelection(); }
}));
$("#librarySearchForm").addEventListener("submit", event => { event.preventDefault(); clearTimeout(searchTimer); action(() => loadLibrary(true)); });
$("#libraryQuery").addEventListener("input", event => {
  if (event.isComposing) return;
  clearTimeout(searchTimer); searchTimer = setTimeout(() => action(() => loadLibrary(true)), 350);
});
for (const id of ["libraryAccount","libraryStart","libraryEnd","libraryTag","libraryGroup"]) $("#"+id).addEventListener("change", () => action(() => loadLibrary(true)));
$("#resetLibrary").addEventListener("click", () => action(async () => { $("#librarySearchForm").reset(); $("#libraryTag").value = ""; await loadLibrary(true); }));
for (const [id,delta] of [["libraryPrevious",-40],["libraryNext",40]]) $("#"+id).addEventListener("click", () => action(async () => { libraryOffset = Math.max(0,libraryOffset+delta); await loadLibrary(); }));
$("#deleteLibrarySelection").addEventListener("click", () => action(() => deleteSavedRecords([...selectedRecords])));
$("#deleteRecord").addEventListener("click", () => action(() => deleteSavedRecords(openedRecord ? [openedRecord.id] : [])));
$("#recordVersion").addEventListener("change", event => { if (openedRecord) renderRecord(event.target.value === "current" ? openedRecord : openedRecord.versions[Number(event.target.value)]); });
$("#libraryJSON").addEventListener("click", () => { if (libraryData) download(`VGHKS-library-${config.today}-page${Math.floor(libraryOffset/40)+1}.json`, JSON.stringify(libraryData,null,2), "application/json;charset=utf-8"); });
$("#libraryCSV").addEventListener("click", () => {
  if (!libraryData) return;
  const rows = [["來源帳號","門診日期","就診號","病歷號","姓名","性別","年齡","自動 tag","手動 tag","關鍵字","SOAP"]];
  for (const r of libraryData.records) rows.push([r.accounts.join(" / "),r.date,r.case_no,r.mrn,r.name,r.sex,r.age,[...new Set(r.matches.map(m=>m.category_name))].join(" / "),(r.manual_tags||[]).map(t=>t.name).join(" / "),[...new Set(r.matches.map(m=>m.keyword))].join(" / "),r.soap]);
  download(`VGHKS-library-${config.today}.csv`, "\ufeff"+rows.map(row=>row.map(csvCell).join(",")).join("\r\n"), "text/csv;charset=utf-8");
});
