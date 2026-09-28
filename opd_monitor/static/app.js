"use strict";

const embeddedAccount = new URLSearchParams(location.search).get("account");
const scopedPath = path => /^[a-f0-9]{32}$/.test(embeddedAccount || "") && path.startsWith("/api/") && path !== "/api/session"
  ? `/api/accounts/${embeddedAccount}${path.slice(4)}` : path;

const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];
const active = new Set(["queued", "running", "cancelling"]);
const statusLabels = {queued:"排隊中", running:"查詢中", cancelling:"停止中", completed:"完成", partial:"部分完成", failed:"失敗", cancelled:"已停止", interrupted:"上次中斷"};
let config, accounts = new Map(), history = [], selectedRun = "", current = null;
let categoryFilter = "", stopped = false, historySignature = "", toastTimer;
const pageSizes = new Map();

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function button(text, action, className) {
  const node = el("button", text, className);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}
function notify(message, error = false) {
  if (error) {
    $("#notice").textContent = message;
    $("#notice").hidden = false;
  } else {
    $("#toast").textContent = message;
    $("#toast").hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 3200);
  }
}
async function api(path, body) {
  const response = await fetch(scopedPath(path), {
    method: body === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store",
    headers: body === undefined ? {} : {"Content-Type":"application/json", "X-CSRF-Token":config?.csrf || "", "X-Database-Context":config?.context || ""},
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "操作未完成。");
  return data;
}
async function action(callback) {
  $("#notice").hidden = true;
  const control = document.activeElement?.tagName === "BUTTON" ? document.activeElement : null;
  if (control?.dataset.busy) return;
  const disabled = control?.disabled;
  if (control) {control.dataset.busy = "true"; control.setAttribute("aria-busy", "true"); control.disabled = true;}
  try { await callback(); } catch (error) { notify(error.message, true); }
  finally {if (control) {delete control.dataset.busy; control.removeAttribute("aria-busy"); if (control.isConnected) control.disabled = !!disabled;}}
}
const rangeLabel = r => r.start === r.end ? r.start : `${r.start} ～ ${r.end}`;
const accountLabel = r => r.account_label ? `${r.account_label} · ${r.account}` : r.account;

function addAccount(account) {
  accounts.set(account.id, account);
  const form = $("#accountTemplate").content.firstElementChild.cloneNode(true);
  form.dataset.id = account.id;
  for (const key of ["label", "username", "start", "end", "response_encoding"]) form.elements[key].value = account[key] || "";
  for (const input of $$("input[type=radio]", form)) {
    input.name = `mode-${account.id}`;
    input.checked = input.value === account.mode;
    input.addEventListener("change", () => updateDates(form));
  }
  form.elements.start.max = form.elements.end.max = "9999-12-31";
  $(".account-title", form).textContent = account.label || account.username || "新帳號";
  form.elements.password.placeholder = account.password_set ? "已輸入；留白保留" : "請輸入密碼";
  form.elements.username.addEventListener("input", () => {
    form.elements.password.placeholder = form.elements.username.value.trim() === accounts.get(account.id)?.username && accounts.get(account.id)?.password_set ? "已輸入；留白保留" : "請輸入密碼";
  });
  form.elements.start.addEventListener("change", () => { form.elements.end.min = form.elements.start.value || "1912-01-01"; });
  $(".today-button", form).addEventListener("click", () => {
    $("input[type=radio][value=single]", form).checked = true;
    form.elements.start.value = form.elements.end.value = config.today;
    updateDates(form);
  });
  $(".save-account", form).addEventListener("click", () => action(async () => { await saveRows([form]); notify("帳號設定已儲存"); }));
  $(".delete-account", form).addEventListener("click", () => action(async () => {
    await api("/api/accounts/delete", {id:account.id});
    accounts.delete(account.id);
    form.remove();
    syncWorkspaceAccounts();
    notify("已移除帳號，查詢紀錄保留");
  }));
  form.addEventListener("submit", event => { event.preventDefault(); action(() => startRows([form])); });
  $(".stop-account", form).addEventListener("click", () => action(async () => { await api("/api/stop", {id:form.dataset.run}); await refresh(); }));
  $(".view-run", form).addEventListener("click", () => action(() => selectRun(form.dataset.run)));
  $("#accounts").append(form);
  updateDates(form);
  return form;
}
function updateDates(form) {
  const isRange = $("input[type=radio]:checked", form).value === "range";
  $(".end-field", form).hidden = !isRange;
  form.elements.end.disabled = !isRange;
  form.elements.end.required = isRange;
  form.elements.end.min = form.elements.start.value || "1912-01-01";
  $(".start-field span", form).textContent = isRange ? "開始日期" : "門診日期";
}
async function saveRows(forms, {credentialsOnly=false}={}) {
  for (const form of forms) if (credentialsOnly ? !form.elements.username.checkValidity() : !form.checkValidity()) {
    $("#accountsDetails").open = true;
    (credentialsOnly ? form.elements.username : form).reportValidity();
    throw new Error(credentialsOnly ? "請填妥登入帳號。" : "請填妥帳號與有效日期。");
  }
  const values = forms.map(form => ({
    id:form.dataset.id, label:form.elements.label.value, username:form.elements.username.value,
    password:form.elements.password.value,
    ...(!credentialsOnly ? {mode:$("input[type=radio]:checked", form).value,
      start:form.elements.start.value, end:form.elements.end.value} : {}),
    response_encoding:form.elements.response_encoding.value
  }));
  const saved = await api("/api/accounts/save", {accounts:values});
  for (const account of saved.accounts) {
    accounts.set(account.id, account);
    const form = forms.find(f => f.dataset.id === account.id);
    form.elements.password.value = "";
    form.elements.password.placeholder = account.password_set ? "已輸入；留白保留" : "請輸入密碼";
    $(".account-title", form).textContent = account.label || account.username || "新帳號";
  }
  syncWorkspaceAccounts();
}
async function startRows(forms) {
  if (!forms.length) throw new Error("請勾選要查詢的帳號。");
  const controls = forms.flatMap(f => $$(".run-account,.save-account", f));
  controls.forEach(b => { b.disabled = true; });
  try {
    await saveRows(forms);
    const result = await api("/api/lists/browse", {account_ids:forms.map(f => f.dataset.id)});
    selectedRun = result.run_ids[0];
    current = null;
    categoryFilter = "";
    pageSizes.clear();
    $("#accountsDetails").open = false;
    await useListAccount(forms[0].dataset.id);
    showPane("list");
    await refresh();
  } finally { controls.forEach(b => { b.disabled = false; }); updateAccountProgress(); }
}
function updateAccountProgress() {
  for (const form of $$(".account-card")) {
    const run = history.find(r => r.account_id === form.dataset.id);
    const busy = history.find(r => r.account_id === form.dataset.id && active.has(r.status));
    const shown = busy || run;
    $(".account-state", form).textContent = shown ? statusLabels[shown.status] || shown.status : "尚未查詢";
    $(".run-account", form).disabled = Boolean(busy);
    $(".delete-account", form).disabled = Boolean(busy);
    $(".account-progress", form).hidden = !shown;
    if (shown) {
      form.dataset.run = shown.id;
      $(".account-progress>span", form).textContent = `${rangeLabel(shown)} · ${shown.message} SOAP ${shown.counts.soap_read}`;
      $(".account-progress>span", form).title = $(".account-progress>span", form).textContent;
      $(".account-progress .job-progress", form)?.remove();$(".account-progress", form).append(JobProgress.create(shown));
      $(".stop-account", form).hidden = !active.has(shown.status);
    }
  }
}
function renderHistory() {
  const filter = $("#historyAccount").value;
  const options = new Map(history.map(r => [r.account, r.account]));
  const optionSignature = JSON.stringify([...options]);
  if ($("#historyAccount").dataset.signature !== optionSignature) {
    $("#historyAccount").replaceChildren(new Option("全部帳號", ""), ...[...options].map(([value, label]) => new Option(label, value)));
    $("#historyAccount").value = options.has(filter) ? filter : "";
    $("#historyAccount").dataset.signature = optionSignature;
  }
  const rows = history.filter(r => !$("#historyAccount").value || r.account === $("#historyAccount").value);
  const signature = JSON.stringify([selectedRun, rows.map(r => [r.id,r.revision,r.status])]);
  if (signature === historySignature) return;
  historySignature = signature;
  const focusedId = $("#historyList").contains(document.activeElement) ? document.activeElement.dataset.id : "";
  const nodes = rows.map(run => {
    const node = button("", () => action(() => selectRun(run.id)), "history-item" + (selectedRun === run.id ? " selected" : ""));
    node.dataset.id = run.id;
    node.setAttribute("aria-pressed", String(selectedRun === run.id));
    node.append(el("strong", accountLabel(run)), el("span", rangeLabel(run)));
    const meta = el("span", undefined, "history-meta");
    meta.append(el("span", `${statusLabels[run.status] || run.status} · ${run.kind === "list" ? "清單" : "SOAP"} ${run.kind === "list" ? run.counts.registrations : run.counts.soap_read} 筆`, `status-${run.status}`), el("span", run.created_at.slice(5,16).replace("T"," ")));
    node.append(meta);
    if (run.source_id) node.append(el("span", "重新分類"));
    return node;
  });
  $("#historyList").replaceChildren(...nodes);
  if (focusedId) nodes.find(n => n.dataset.id === focusedId)?.focus({preventScroll:true});
  $("#historyCount").textContent = rows.length;
  $("#historyEmpty").hidden = rows.length > 0;
}
async function selectRun(id) {
  showPane("history");
  selectedRun = id;
  current = null;
  categoryFilter = "";
  pageSizes.clear();
  renderHistory();
  await loadRun();
}
async function loadRun() {
  if (!selectedRun) return;
  const id = selectedRun;
  const result = await api(`/api/run?id=${encodeURIComponent(id)}&revision=${current?.id === id ? current.revision : ""}`);
  if (stopped || selectedRun !== id || result.unchanged) return;
  current = result;
  renderRun();
}
async function refresh() {
  const result = await api("/api/history");
  if (stopped) return;
  history = result.runs;
  if (!selectedRun && history.length) selectedRun = history[0].id;
  updateAccountProgress();
  renderHistory();
  if (result.warnings?.length) notify(result.warnings.join(" "), true);
  if (activePane === "history") await loadRun();
  await refreshWorkspace();
}
function renderRun() {
  if (!current) return;
  $("#resultContext").textContent = `${accountLabel(current)} · ${rangeLabel(current)}${current.source_id ? " · 重新分類" : ""}`;
  $("#runStatus").hidden = false;
  $("#runStatusLabel").textContent = statusLabels[current.status] || current.status;
  $("#runMessage").textContent = current.message;
  $("#runMessage").title = current.message || "";
  $("#stopRun").hidden = !active.has(current.status);
  $("#runProgress").hidden = !active.has(current.status);
  const c = current.counts;
  if (current.stage === "registrations") $("#runProgress").value = c.days_total ? c.days_done / c.days_total * 100 : 0;
  else if (current.stage === "soap") $("#runProgress").value = c.patients_total ? c.patients_done / c.patients_total * 100 : 0;
  else $("#runProgress").removeAttribute("value");
  $("#metrics").hidden = false;
  $("#metrics").replaceChildren(...[["門診病人",c.patients_total],["已保存 SOAP",c.soap_read],["有 tag 病人",c.matched_patients],["tag 段落",c.markers]].map(([label, value]) => {
    const item = el("div", undefined, "metric"); item.append(el("span",label),el("strong",value)); return item;
  }));
  $("#resultFilters").hidden = false;
  $("#reclassify").disabled = active.has(current.status) || !current.records.length;
  $("#jsonExport").disabled = false;
  $("#csvExport").disabled = !current.records.length;
  $("#audit").hidden = false;
  $("#auditTitle").textContent = `查詢明細${c.errors ? ` · ${c.errors} 項異常` : ""}`;
  const counts = el("div", undefined, "audit-counts");
  counts.append(...[["日期",`${c.days_done}/${c.days_total}`],["清單快取",c.days_cached || 0],["SOAP 重用",c.soap_cached || 0],["日期失敗",c.days_failed],["門診清單",c.registrations],["已核對病人",c.patients_done],["有就診",c.with_visit],["無同日就診",c.without_visit],["無法判定",c.unknown],["共用診",c.shared],["排除",c.excluded],["SOAP 無內容",c.soap_missing]].map(([label,value]) => el("span",`${label} ${value}`)));
  $("#auditCounts").replaceChildren(counts);
  $("#issues").replaceChildren(...current.issues.map(issue => {
    const node = el("div",undefined,"issue"); node.append(el("small",[issue.stage,issue.mrn,issue.date,issue.code].filter(Boolean).join(" · ")),el("span",issue.message)); return node;
  }));
  renderResults();
}
function filteredRecords() {
  if (!current) return [];
  const search = $("#search").value.trim().toLocaleLowerCase();
  return current.records.filter(r => !search || [r.name,r.mrn,r.sex,r.age,r.date,r.soap].join(" ").toLocaleLowerCase().includes(search));
}
function renderResults() {
  if (!current) return;
  const records = filteredRecords();
  const all = $("#recordScope").value === "all";
  const categories = current.categories || [];
  if (!categories.some(c => c.id === categoryFilter)) categoryFilter = "";
  const filterButtons = [{id:"",name:"全部 tag"}, ...categories].map(category => {
    const count = records.filter(r => r.matches.some(m => !category.id || m.category === category.id)).length;
    const node = button(`${category.name} ${count}`, () => { categoryFilter = category.id; pageSizes.clear(); renderResults(); });
    node.setAttribute("aria-pressed", String(categoryFilter === category.id));
    return node;
  });
  $("#categoryFilters").replaceChildren(...(all ? [] : filterButtons));
  const groups = all ? [{id:"all",name:"全部 SOAP"}] : categories.filter(c => !categoryFilter || c.id === categoryFilter);
  const sections = [];
  for (const category of groups) {
    const rows = records.filter(r => all || r.matches.some(m => m.category === category.id));
    if (!rows.length) continue;
    const section = el("section",undefined,"category-section" + (category.parser === "surgery" ? " surgery" : ""));
    const heading = el("div",undefined,"category-heading");
    heading.append(el("h3",category.name),el("span",`${rows.length} 筆就診`,"badge"));
    const grid = el("div",undefined,"patient-grid");
    const limit = pageSizes.get(category.id) || 40;
    grid.append(...rows.slice(0,limit).map(row => patientCard(row, all ? "" : category.id)));
    section.append(heading,grid);
    if (rows.length > limit) section.append(button(`載入更多（尚有 ${rows.length-limit} 筆）`, () => { pageSizes.set(category.id,limit+40); renderResults(); },"load-more"));
    sections.push(section);
  }
  $("#results").replaceChildren(...sections);
  $("#emptyResult").hidden = sections.length > 0;
  $("#emptyResult").textContent = active.has(current.status) ? "查詢進行中，取得結果後會顯示在這裡" : current.status !== "completed" ? "目前沒有符合條件的結果；請查看查詢明細" : "沒有符合條件的結果";
}
function surgeryFields(values) {
  const node = el("dl",undefined,"surgery-fields");
  for (const [key,label] of [["procedure","手術"],["laterality","側別"],["iol","IOL"],["target","Target"],["scheduled_date","排程日期"],["tel","TEL"]]) {
    const value = key === "scheduled_date" ? values.date_iso || values.scheduled_date : values[key];
    const item = el("div"); item.append(el("dt",label),el("dd",value || "未填",value ? "" : "unfilled")); node.append(item);
  }
  return node;
}
function patientCard(record, category) {
  const card = el("article",undefined,"patient-card");
  const head = el("div",undefined,"patient-head"); head.append(el("strong",record.name || "姓名未提供"),el("span",record.date));
  card.append(head,el("p",[record.mrn,record.sex,record.age,record.section].filter(Boolean).join(" · "),"patient-meta"));
  if(record.manual_tags?.length)card.append(patientTagBadges(record.manual_tags,"手動"));
  const matches = record.matches.filter(m => !category || m.category === category);
  for (const match of matches.slice(0,2)) {
    const excerpt = el("div",undefined,"excerpt"); excerpt.append(el("pre",match.header));
    if (match.surgery) excerpt.append(surgeryFields(match.surgery));
    card.append(excerpt);
  }
  const foot = el("div",undefined,"card-footer");
  foot.append(el("span",matches.length ? `${matches.length} 個自動 tag 段落` : record.manual_tags?.length ? "病人手動 tag" : "無 tag"),button("段落與 SOAP ↗", () => openRecord(record)));
  card.append(foot);
  return card;
}
function openRecord(record) {
  $("#versionLabel").hidden = true;
  $("#deleteRecord").hidden = true;
  renderRecord(record);
  if (!$("#recordDialog").open) $("#recordDialog").showModal();
}
function renderRecord(record) {
  const manual=$("#recordManualTags");if(manual)manual.replaceChildren(patientTagBadges(record.manual_tags||[],"手動"));
  $("#recordTitle").textContent = record.name || "病歷內容";
  $("#recordMeta").textContent = [record.mrn,record.sex,record.age,record.date,record.section,record.doctor].filter(Boolean).join(" · ");
  $("#recordExcerpts").replaceChildren(...record.matches.map(match => {
    const block = el("section"); block.append(el("h3",`${match.category_name} · ${match.keyword}`));
    const excerpt = el("div",undefined,"excerpt");
    if (match.surgery) excerpt.append(surgeryFields(match.surgery));
    excerpt.append(el("pre",match.excerpt)); block.append(excerpt); return block;
  }));
  // Python offsets count Unicode code points; Array.from preserves that mapping.
  const chars = Array.from(record.soap), intervals = [];
  for (const match of record.matches.filter(m=>Number.isInteger(m.start)&&m.start>=0).sort((a,b) => a.start-b.start)) {
    const previous = intervals.at(-1);
    if (previous && match.start <= previous[1]) previous[1] = Math.max(previous[1],match.end);
    else intervals.push([match.start,match.end]);
  }
  let cursor = 0;
  const nodes = [];
  for (const [start,end] of intervals) {
    nodes.push(document.createTextNode(chars.slice(cursor,start).join("")),el("mark",chars.slice(start,end).join("")));
    cursor = end;
  }
  nodes.push(document.createTextNode(chars.slice(cursor).join("")));
  $("#fullSoap").replaceChildren(...nodes);
  $("#fullSoapDetails").open = !record.matches.length;
}
function addCategory(value = {id:crypto.randomUUID().replaceAll("-",""),name:"",keywords:[],parser:"none"}) {
  const fieldset = $("#categoryTemplate").content.firstElementChild.cloneNode(true);
  fieldset.dataset.id = value.id;
  $("[name=category_name]",fieldset).value = value.name;
  $("[name=keywords]",fieldset).value = value.keywords.join("\n");
  $("[name=parser]",fieldset).value = value.parser;
  const scopeLabel=el("label","辨識範圍"),scope=el("select");scope.name="scope";
  for(const [key,label] of [["all","全文"],["s","S"],["o","O"],["ap","A+P"],["medications","藥囑"],["orders","醫囑"]])scope.append(new Option(label,key));
  scope.value=value.scope||"all";scopeLabel.append(scope);fieldset.append(scopeLabel);
  $(".remove-category",fieldset).addEventListener("click",() => fieldset.remove());
  $("#categoryEditor").append(fieldset);
}
function openSettings() {
  $("#settingsError").hidden = true;
  $("#categoryEditor").replaceChildren();
  config.settings.categories.forEach(addCategory);
  for (const [key,value] of Object.entries(config.settings)) {
    const control = $("#settingsForm").elements[key];
    if (!control) continue;
    if (typeof value === "boolean") control.checked = value; else control.value = value;
  }
  $("#settingsDialog").showModal();
}
function download(name, content, mime) {
  const url = URL.createObjectURL(new Blob([content],{type:mime}));
  const a = el("a"); a.href=url; a.download=name; document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url),30000);
}
function csvCell(value) {
  let text = String(value ?? "");
  if (/^[\s]*[=+\-@\t\r]/.test(text)) text = "'" + text;
  return `"${text.replaceAll('"','""')}"`;
}
function exportCSV() {
  const rows = [["帳號","門診日期","病歷號","姓名","性別","年齡","分類","關鍵字","手術","側別","IOL","Target","排程日期","TEL","段落","SOAP"]];
  for (const record of filteredRecords()) {
    const matches = record.matches.filter(m => $("#recordScope").value === "all" || !categoryFilter || m.category === categoryFilter);
    if (!matches.length && $("#recordScope").value !== "all") continue;
    for (const match of matches.length ? matches : [{}]) {
      const s = match.surgery || {};
      rows.push([current.account,record.date,record.mrn,record.name,record.sex,record.age,match.category_name,match.keyword,s.procedure,s.laterality,s.iol,s.target,s.date_iso || s.scheduled_date,s.tel,match.excerpt,record.soap]);
    }
  }
  download(`VGHKS-${current.account}-${current.start}-${current.id.slice(0,8)}.csv`,"\ufeff"+rows.map(row=>row.map(csvCell).join(",")).join("\r\n"),"text/csv;charset=utf-8");
}

$("#addAccount").addEventListener("click",() => {
  $("#accountsDetails").open = true;
  const form = addAccount({id:crypto.randomUUID().replaceAll("-",""),label:"",username:"",mode:"single",start:config.today,end:config.today,response_encoding:"auto",password_set:false});
  form.elements.username.focus();
});
$("#runSelected").addEventListener("click",() => action(async () => {
  $("#runSelected").disabled = true;
  try { await startRows($$(".account-card").filter(f => f.elements.selected.checked && !$(".run-account",f).disabled)); }
  finally { $("#runSelected").disabled = false; }
}));
$("#settingsButton").addEventListener("click",openSettings);
$("#addCategory").addEventListener("click",() => addCategory());
$("#settingsForm").addEventListener("submit",async event => {
  event.preventDefault();
  const submit = $("button[type=submit]",event.target); submit.disabled = true;
  try {
    const categories = $$(".category-edit").map(f => ({id:f.dataset.id,name:$("[name=category_name]",f).value,keywords:$("[name=keywords]",f).value.split(/\r?\n/).filter(k=>k.trim()),parser:$("[name=parser]",f).value,scope:$("[name=scope]",f).value}));
    const values = {categories};
    for (const input of $$(".connection-settings input")) values[input.name] = input.type === "checkbox" ? input.checked : Number(input.value);
    config.settings = await api("/api/settings",values);
    $("#settingsDialog").close(); notify("tag 已更新，病歷資料庫已套用");
    await loadLibrary(true);
    if(typeof loadTagGroups==="function")await loadTagGroups(true);
  } catch (error) { $("#settingsError").textContent=error.message; $("#settingsError").hidden=false; }
  finally { submit.disabled=false; }
});
$$('[data-close]').forEach(node => node.addEventListener("click",() => document.getElementById(node.dataset.close).close()));
$("#historyAccount").addEventListener("change",renderHistory);
$("#search").addEventListener("input",() => { pageSizes.clear(); renderResults(); });
$("#recordScope").addEventListener("change",() => { pageSizes.clear(); renderResults(); });
$("#stopRun").addEventListener("click",() => action(async () => { await api("/api/stop",{id:selectedRun}); await refresh(); }));
$("#reclassify").addEventListener("click",() => action(async () => {
  $("#reclassify").disabled = true;
  try { const result = await api("/api/reclassify",{id:selectedRun}); await selectRun(result.run_ids[0]); await refresh(); }
  finally { if (current) $("#reclassify").disabled=active.has(current.status) || !current.records.length; }
}));
$("#jsonExport").addEventListener("click",() => { if (current) download(`VGHKS-${current.account}-${current.start}-${current.id.slice(0,8)}.json`,JSON.stringify(current,null,2),"application/json;charset=utf-8"); });
$("#csvExport").addEventListener("click",exportCSV);
$("#exitButton").addEventListener("click",() => action(async () => {
  await api("/api/shutdown",{}); stopped=true;
  document.body.replaceChildren(el("p","程式已結束，查詢紀錄已保留。可以關閉此分頁。","closed"));
}));
async function poll() {
  if (stopped) return;
  try { await refresh(); } catch (error) { if (!stopped) notify(error.message === "Failed to fetch" ? "無法連線至本機程式，請重新執行 EXE。" : error.message,true); }
  if (!stopped) setTimeout(poll,1800);
}
async function initialize() {
  const token = location.hash.slice(1);
  if (/^[A-Za-z0-9_-]{43}$/.test(token)) { await api("/api/session",{token}); window.history.replaceState(null,"",location.pathname); }
  config = await api("/api/bootstrap");
  $("#version").textContent = `v${config.version}`;
  $("#dataPath").textContent = config.data_dir;
  config.accounts.forEach(addAccount);
  await initializeWorkspace();
  window.dispatchEvent(new Event("workspace-ready"));
  if (config.warnings?.length) notify(config.warnings.join(" "),true);
  await poll();
}
document.addEventListener("DOMContentLoaded", () => initialize().catch(error => { notify(error.message,true); $("#runSelected").disabled=true; $("#addAccount").disabled=true; $("#settingsButton").disabled=true; }));
