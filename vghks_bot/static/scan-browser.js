"use strict";

window.ScanBrowser = (() => {
  let context = null, task = "", starting = false, serial = 0, previewSerial = 0, selectedId = "";
  let catalog = null, previewHost = null, rendered = "";
  let categoryFilter = "eye", searchText = "", archive = null;
  const make = (tag, label, cls) => {
    const el = document.createElement(tag);
    if (label !== undefined) el.textContent = label;
    if (cls) el.className = cls;
    return el;
  };
  const button = (label, fn) => {
    const el = make("button", label);
    el.type = "button";
    el.addEventListener("click", async () => {
      if (el.dataset.busy) return;
      const revision = serial, wasDisabled = el.disabled;
      el.dataset.busy = "true";
      el.setAttribute("aria-busy", "true");
      el.disabled = true;
      try {await fn();}
      catch (error) {if (isOpen(revision)) message(error.message, true);}
      finally {
        delete el.dataset.busy;
        el.removeAttribute("aria-busy");
        if (el.isConnected) el.disabled = wasDisabled;
      }
    });
    return el;
  };
  const dialog = () => document.getElementById("scanBrowserDialog");
  const content = () => document.getElementById("scanBrowserContent");
  const isOpen = revision => revision === serial && dialog()?.open;
  const emptyPreview = () => make("p", "從左側選擇掃描病歷，在這裡查看 PDF。", "scan-empty-preview");
  function mount() {
    if (dialog()) return;
    const shell = make("dialog", undefined, "scan-browser-dialog");
    shell.id = "scanBrowserDialog";
    shell.setAttribute("aria-labelledby", "scanBrowserTitle");
    const head = make("div", undefined, "scan-browser-head");
    const titleGroup = make("div", undefined, "scan-browser-title-group");
    const title = make("h2", "掃描病歷");
    title.id = "scanBrowserTitle";
    const patient = make("p", "", "dialog-patient");
    patient.id = "scanBrowserPatient";
    titleGroup.append(title, patient);
    const compare = button("查看比較", () => window.FileCompare.open());
    compare.id = "scanCompareOpen";
    head.append(titleGroup, compare, button("關閉", () => shell.close()));
    const body = make("div", undefined, "scan-browser-content");
    body.id = "scanBrowserContent";
    shell.append(head, body);
    shell.addEventListener("close", () => {if (!shell.open) clearView();});
    document.body.append(shell);
    window.addEventListener("filecomparechange", syncCompare);
    window.addEventListener("filecomparemessage", event => {if (shell.open) message(event.detail, true);});
    syncCompare();
  }
  function syncCompare() {
    const compare = document.getElementById("scanCompareOpen");
    if (!compare) return;
    const count = window.FileCompare?.count() || 0;
    compare.textContent = count ? `查看比較（${count} / 6）` : "查看比較";
    compare.disabled = !count;
  }
  function message(value, error = false) {
    const notice = make("p", value, error ? "scan-error" : "scan-caption");
    content()?.prepend(notice);
  }
  function base() {
    const {mrn, review_task_id, cohort_id, resource, reference} = context;
    return {mrn, ...(review_task_id ? {review_task_id} : {cohort_id}), resource, reference: reference || ""};
  }
  const request = (path, values) => context.request(path, values);
  function canFetch() {return typeof context?.allowFetch === "function" ? context.allowFetch() : true;}
  function image(name, history = false) {
    const icon = make("span", undefined, "scan-icon" + (history ? " scan-icon-history" : ""));
    const img = make("img");
    img.src = "/review-scan-current.svg";
    img.alt = "";
    img.setAttribute("aria-hidden", "true");
    icon.append(img);
    if (history) {const clock = make("span", "◷", "scan-clock"); clock.setAttribute("aria-hidden", "true"); icon.append(clock);}
    const label = make("span", name);
    const wrap = make("span", undefined, "scan-labelled");
    wrap.append(icon, label);
    return wrap;
  }
  async function open(options) {
    mount();
    clearView();
    context = options;
    if (options.archive && window.ReviewArchive) archive = {
      selected: new Set(), rows: new Map(), checks: [], visible: [], busy: false, notice: "", error: false,
    };
    categoryFilter = "eye";
    searchText = "";
    const displayName = (window.ClinicalUI?.patientName?.(options.patient_name) ?? options.patient_name) || "姓名未提供";
    window.FileCompare?.context(options.account, options.mrn, displayName);
    syncCompare();
    document.getElementById("scanBrowserTitle").textContent = options.resource === "case_scans" ? "該次掃描病歷" : "歷年掃描病歷";
    document.getElementById("scanBrowserPatient").textContent = displayName + " · 病歷號 " + options.mrn;
    dialog().setAttribute("aria-describedby", "scanBrowserPatient");
    content().replaceChildren(make("p", "正在讀取已存掃描資料…"));
    if (!dialog().open) dialog().showModal();
    await refresh({fetchMissing:true});
  }
  async function refresh({fetchMissing = false} = {}) {
    if (!context || !dialog()?.open) return;
    const revision = serial;
    try {
      const result = await request("/reviews/history/read", base());
      if (!isOpen(revision)) return;
      render(result);
      if (fetchMissing && !result.complete && canFetch()) await start({kind:"history", ...base()});
    } catch (error) {
      if (isOpen(revision)) throw error;
    }
  }
  async function start(values) {
    if (task || starting) return;
    if (!canFetch()) {message("目前可查看已保存資料；重新連線後可補查。", true); return;}
    const revision = serial;
    starting = true;
    const status = make("div");
    content().prepend(status);
    window.JobProgress?.pending(status, "掃描病歷", "正在送出掃描病歷查詢…");
    let started;
    try {started = await request("/tasks/start", values);}
    finally {status.remove(); if (revision === serial) starting = false;}
    if (!isOpen(revision)) return;
    const id = task = started.task_id;
    try {await poll(revision, id);}
    finally {if (revision === serial && task === id) task = "";}
  }
  async function poll(revision, id) {
    const current = () => isOpen(revision) && task === id;
    let lastStatus = "";
    while (current()) {
      await window.JobProgress.ready();
      if (!current()) return;
      const detail = await request("/tasks/detail?summary=1&id=" + encodeURIComponent(id));
      if (!current()) return;
      const status = JSON.stringify([detail.status, detail.revision ?? detail.updated_at]);
      if (status !== lastStatus) {
        const result = await request("/reviews/history/read", base());
        if (!current()) return;
        lastStatus = status;
        render(result);
      }
      window.JobProgress.publish(detail);
      if (!["queued", "running", "cancelling"].includes(detail.status)) {
        if (["failed", "partial", "paused"].includes(detail.status)) message(detail.message || "部分掃描資料未取得，可續跑。", true);
        return;
      }
      await window.JobProgress.wait();
    }
  }
  function scanRow(row, index) {
    const line = make("div", undefined, "scan-row");
    line.dataset.scanId = row.id;
    if (selectedId === row.id) line.classList.add("active");
    const meta = make("div", undefined, "scan-row-meta");
    const category = row.category_label || (row.section ? "SOAP 掃描" : "類別未提供");
    meta.append(make("strong", category + " · " + (row.date || "日期未提供")),
      make("span", [row.section_label, row.section, row.case_no,
        row.record_type ? `PDF ${row.record_type}` : ""].filter(Boolean).join(" · ") || "就診資料待核對", "scan-caption"));
    const actions = make("div", undefined, "scan-row-actions");
    if (archive) actions.append(archiveChoice(row.id, "選取", `選取下載 ${category} · ${row.date || "日期未提供"}${row.case_no ? " · " + row.case_no : ""}`));
    actions.append(button("開啟", () => showAsset(row, index)), button("加入比較", () => addCompare(row, index)));
    line.append(meta, actions);
    return line;
  }
  const categoryKey = row => JSON.stringify([row.section_label || "", row.category_label || ""]);
  const historicalRecords = data => data.records || [...(data.scans || []), ...(data.unclassified || [])];

  function archiveChoice(id, label, description) {
    const state = archive;
    const wrapper = make("label", undefined, "check-label");
    const input = make("input");
    input.type = "checkbox";
    if (description) input.setAttribute("aria-label", description);
    input.addEventListener("change", () => {
      if (archive === state && input.isConnected) updateArchiveSelection(id ? [id] : state.visible, input.checked);
    });
    wrapper.append(input, make("span", label));
    if (id) archive.checks.push([input, id]);
    else archive.all = input;
    return wrapper;
  }
  function archiveToolbar() {
    const toolbar = make("div", undefined, "review-archive-toolbar");
    const download = make("button", "下載選取原檔 ZIP", "history-action");
    const clear = make("button", "清除選取", "history-action quiet");
    const count = make("span", undefined, "caption review-archive-count");
    const status = make("span", undefined, "caption review-archive-status");
    clear.type = download.type = "button";
    clear.addEventListener("click", () => updateArchiveSelection(archive.selected, false));
    download.addEventListener("click", () => void downloadArchive());
    count.setAttribute("aria-live", "polite");
    status.setAttribute("role", "status");
    toolbar.append(archiveChoice(null, "全選目前顯示項目"), download, clear, count, status);
    Object.assign(archive, {download, clear, count, status});
    return toolbar;
  }
  function updateArchiveSelection(ids, checked) {
    if (!archive || archive.busy) return;
    const selected = new Set(archive.selected);
    for (const id of ids) {
      if (checked) selected.add(id);
      else selected.delete(id);
    }
    archive.error = selected.size > window.ReviewArchive.limit;
    archive.notice = archive.error ? `每批最多可選 ${window.ReviewArchive.limit} 份掃描病歷。` : "";
    if (!archive.error) archive.selected = selected;
    syncArchive();
  }
  function syncArchive() {
    if (!archive?.all) return;
    const {selected, checks, visible, all, download, clear, count, status, notice, busy, error} = archive;
    const shown = visible.filter(id => selected.has(id)).length;
    all.disabled = busy || !visible.length;
    all.checked = !!visible.length && shown === visible.length;
    all.indeterminate = shown > 0 && shown < visible.length;
    for (const [input, id] of checks) {
      input.checked = selected.has(id);
      input.disabled = busy;
    }
    clear.disabled = download.disabled = busy || !selected.size;
    if (busy) download.setAttribute("aria-busy", "true");
    else download.removeAttribute("aria-busy");
    count.textContent = `已選 ${selected.size} 份`;
    status.textContent = notice;
    status.classList.toggle("error", error);
  }
  async function downloadArchive() {
    if (!archive || archive.busy || !archive.selected.size) return;
    const revision = serial, scope = base(), request = context.request, state = archive;
    const controller = state.controller = new AbortController(), signal = controller.signal;
    const current = () => !signal.aborted && isOpen(revision) && archive === state;
    const read = (path, values) => request(path, values, {signal});
    const ids = [...state.selected];
    state.busy = true;
    state.error = false;
    try {
      const missing = ids.filter(id => !state.rows.get(id)?.asset);
      for (const [index, id] of missing.entries()) {
        if (!current()) return;
        if (!canFetch()) break;
        state.notice = `正在取得掃描原檔 ${index + 1} / ${missing.length}…`;
        syncArchive();
        try {
          await readAsset(read, {...scope, resource:"scan_asset", reference:id}, canFetch, current, job => window.JobProgress.publish(job));
        } catch (_) { /* The archive lists any sources that are still missing. */ }
      }
      if (!current()) return;
      state.notice = "正在打包原檔…";
      syncArchive();
      const result = await window.ReviewArchive.download({request, scope, ids, current, signal});
      if (current() && result) {
        state.notice = `已下載 ${result.files} 個原檔。` +
          (result.missing ? ` ${result.missing} 筆未取得，請查看 ZIP 內的缺漏清單。` : "");
        state.error = !!result.missing;
      }
    } catch (error) {
      if (current()) {
        state.notice = error.message || "無法下載原檔 ZIP。";
        state.error = true;
      }
    } finally {
      if (current()) {state.busy = false; state.controller = null; syncArchive();}
    }
  }

  function renderScanRows(target, data) {
    target.replaceChildren();
    const historical = context.resource === "scans";
    const records = historical ? historicalRecords(data) : data.scans || [];
    const term = searchText.trim().toLocaleLowerCase();
    const visible = historical ? records.filter(row => {
      const categoryMatches = categoryFilter === "all" ||
        (categoryFilter === "eye" && row.eye) ||
        (categoryFilter === "unknown" && !row.category_label) ||
        (categoryFilter.startsWith("category:") && categoryKey(row) === categoryFilter.slice(9));
      return categoryMatches && (!term || [row.category_label, row.section_label, row.date,
        row.section, row.case_no, row.record_type].some(value => (value || "").toLocaleLowerCase().includes(term)));
    }) : records;
    if (archive) {
      archive.checks = [];
      archive.visible = [...new Set(visible.map(row => row.id))];
    }
    if (historical) target.append(make("p", `顯示 ${visible.length} / ${records.length} 筆掃描病歷`, "scan-caption"));
    if (visible.length) visible.forEach((row, index) => target.append(scanRow(row, index)));
    else target.append(make("p", historical ? "此篩選條件沒有掃描病歷。" : "該次 SOAP 未提供掃描病歷。", "scan-caption"));
    syncArchive();
  }
  function render(result) {
    const data = result.data, online = canFetch();
    const signature = JSON.stringify([data, online]);
    if (signature === rendered) return;
    rendered = signature;
    if (!catalog) {
      const workspace = make("div", undefined, "scan-browser-workspace");
      catalog = make("div", undefined, "scan-browser-catalog");
      previewHost = make("div", undefined, "scan-browser-preview-host");
      previewHost.id = "scanPreviewHost";
      previewHost.append(emptyPreview());
      workspace.append(catalog, previewHost);
      content().replaceChildren(workspace);
    }
    catalog.replaceChildren();
    if (!data) {
      catalog.append(make("p", "沒有已存掃描病歷；連線後可取得。", "scan-caption"));
      if (online) catalog.append(button("重試取得", () => start({kind:"history", ...base()})));
      return;
    }
    if (archive) {
      const rows = context.resource === "scans" ? historicalRecords(data) : data.scans || [];
      archive.rows = new Map(rows.map(row => [row.id, row]));
      for (const id of archive.selected) if (!archive.rows.has(id)) archive.selected.delete(id);
    }
    if (context.resource === "scans") {
      catalog.append(make("p", data.history_loaded ?
        `完整歷年索引已保存 · ${data.records?.length || 0} 筆 · ${data.categories?.length || 0} 種院方類別` :
        data.cache_error ? "已存索引無法核對；連線後可重新取得完整類別清單。" :
        data.index_loaded ? "目前顯示舊版索引；連線後會更新類別與日期。" : "尚未取得歷年掃描索引。", "scan-caption"));
      if (data.link_error) catalog.append(make("p", "已存就診連結無法核對；歷年類別清單仍可查看。", "scan-error"));
      if (online) catalog.append(button("更新索引", () => start({kind:"history", ...base(), force:true})));
      const filters = make("div", undefined, "scan-filters");
      const categoryLabel = make("label", "病歷類別");
      const categorySelect = make("select");
      categorySelect.setAttribute("aria-label", "篩選掃描病歷類別");
      [["eye", "眼科紀錄"], ["all", "全部類別"], ["unknown", "類別未提供"]].forEach(([value, name]) => {
        const option = make("option", name); option.value = value; categorySelect.append(option);
      });
      (data.categories || []).forEach(category => {
        const option = make("option", `${category.section_label ? category.section_label + " · " : ""}${category.category_label} (${category.count})`);
        option.value = "category:" + categoryKey(category);
        categorySelect.append(option);
      });
      categorySelect.value = categoryFilter;
      if (categorySelect.selectedIndex < 0) {categoryFilter = "eye"; categorySelect.value = categoryFilter;}
      categoryLabel.append(categorySelect);
      const searchLabel = make("label", "搜尋");
      const search = make("input");
      search.type = "search";
      search.placeholder = "類別、日期或就診資料";
      search.value = searchText;
      searchLabel.append(search);
      filters.append(categoryLabel, searchLabel);
      catalog.append(filters);
      if (archive) catalog.append(archiveToolbar());
      const list = make("div", undefined, "scan-group");
      catalog.append(list);
      categorySelect.addEventListener("change", () => {categoryFilter = categorySelect.value; renderScanRows(list, data);});
      search.addEventListener("input", () => {searchText = search.value; renderScanRows(list, data);});
      renderScanRows(list, data);
      if (data.visits_loaded) catalog.append(make("p", `眼科就診參照已核對 ${data.checked_case_count || 0} / ${data.eye_case_count || 0} 次；此數量不影響歷年類別清單。`, "scan-caption"));
      if (!data.links_complete && online) catalog.append(button("補查眼科就診連結", () => start({kind:"history", ...base(), backfill:true})));
    } else {
      catalog.append(make("p", [data.case?.date, data.case?.section, data.case?.case_no].filter(Boolean).join(" · ") || "該次就診", "scan-caption"));
      if (online) catalog.append(button("更新該次掃描", () => start({kind:"history", ...base(), force:true})));
      if (archive && data.scans?.length) catalog.append(archiveToolbar());
      const list = make("div", undefined, "scan-group");
      catalog.append(list);
      renderScanRows(list, data);
    }
  }
  async function runResource(request, values, current, onProgress = () => {}) {
    const started = await request("/tasks/start", {kind:"history", ...values});
    while (current()) {
      await window.JobProgress.ready();
      if (!current()) return null;
      const job = await request("/tasks/detail?summary=1&id=" + encodeURIComponent(started.task_id));
      if (!current()) return null;
      onProgress(job);
      if (!["queued", "running", "cancelling"].includes(job.status)) return job;
      await window.JobProgress.wait();
    }
    return null;
  }
  async function readAsset(request, values, canFetch, current, onProgress) {
    try {
      let result = await request("/reviews/history/read", values);
      if (!current()) return null;
      if (!result.data && canFetch()) {
        const job = await runResource(request, values, current, onProgress);
        if (!job || !current()) return null;
        if (job.status !== "completed") throw new Error(job.message || "掃描 PDF 尚未取得。");
        result = await request("/reviews/history/read", values);
        if (!current()) return null;
      }
      if (!result.data) throw new Error("此掃描 PDF 尚未保存；離線時無法下載。");
      return result.data;
    } catch (error) {
      if (current()) throw error;
      return null;
    }
  }
  const asset = (row, current) => readAsset(request, {...base(), resource:"scan_asset", reference:row.id}, canFetch, current);
  async function addCompare(row, index) {
    const revision = serial;
    const current = () => isOpen(revision);
    const file = await asset(row, current);
    if (!file || !current()) return;
    window.FileCompare.add({account:context.account, mrn:context.mrn, digest:file.digest, mime:file.mime,
      name:`${row.category_label || "掃描病歷"} ${index + 1}`, source:"掃描病歷", date:row.date || ""});
  }
  async function showAsset(row, index) {
    const revision = serial, choice = ++previewSerial;
    const current = () => isOpen(revision) && choice === previewSerial;
    const file = await asset(row, current);
    if (!file || !current()) return;
    const preview = make("section", undefined, "scan-preview");
    const head = make("div", undefined, "scan-preview-head");
    head.append(make("h3", `${row.category_label || "掃描病歷"} · ${row.date || "日期未提供"}`),
      button("加入比較", () => addCompare(row, index)), button("收起", () => {
        previewSerial++; selectedId = "";
        previewHost.replaceChildren(emptyPreview());
        catalog.querySelectorAll(".scan-row").forEach(line => line.classList.remove("active"));
      }));
    const frame = make(file.mime === "application/pdf" ? "iframe" : "img");
    const url = window.LocalSession.resourceURL(`/api/accounts/${context.account}/analysis/asset?id=${file.digest}`);
    frame.src = file.mime === "application/pdf" ? ClinicalUI.pdfURL(url) : url;
    frame.title = "掃描病歷 PDF";
    if (frame.tagName === "IMG") frame.alt = frame.title;
    head.append(ClinicalUI.assetTools({url,asset:file,name:[row.category_label || "掃描病歷",row.date,index+1].filter(Boolean).join(" "),image:frame,compact:true}));
    preview.append(head, frame);
    selectedId = row.id;
    previewHost.replaceChildren(preview);
    for (const line of catalog.querySelectorAll(".scan-row")) line.classList.toggle("active", line.dataset.scanId === row.id);
    if (matchMedia("(max-width: 760px)").matches) preview.scrollIntoView({block:"start"});
  }
  function clearView() {
    serial++; context = null; task = selectedId = rendered = ""; starting = false;
    archive?.controller?.abort();
    catalog = previewHost = archive = null; content()?.replaceChildren();
  }
  function reset() {clearView(); if (dialog()?.open) dialog().close();}
  document.addEventListener("DOMContentLoaded", mount);
  return {open, image, reset, runResource, readAsset};
})();
