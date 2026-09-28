"use strict";

window.ScanBrowser = (() => {
  let context = null, task = "", starting = false, serial = 0, rows = null, selected = null, selectedId = "";
  let categoryFilter = "eye", searchText = "";
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
      const wasDisabled = el.disabled;
      el.dataset.busy = "true";
      el.setAttribute("aria-busy", "true");
      el.disabled = true;
      try {await fn();}
      catch (error) {message(error.message, true);}
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
    shell.addEventListener("click", event => {if (event.target === shell) shell.close();});
    shell.addEventListener("close", () => {serial++; task = ""; selected = null; selectedId = "";});
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
    serial++;
    context = options;
    selected = null;
    selectedId = "";
    categoryFilter = "eye";
    searchText = "";
    task = "";
    starting = false;
    window.FileCompare?.context(options.account, options.mrn, options.patient_name);
    syncCompare();
    document.getElementById("scanBrowserTitle").textContent = options.resource === "case_scans" ? "該次掃描病歷" : "歷年掃描病歷";
    document.getElementById("scanBrowserPatient").textContent = (options.patient_name || "姓名未提供") + " · 病歷號 " + options.mrn;
    dialog().setAttribute("aria-describedby", "scanBrowserPatient");
    content().replaceChildren(make("p", "正在讀取已存掃描資料…"));
    if (!dialog().open) dialog().showModal();
    await refresh({fetchMissing:true});
  }
  async function refresh({fetchMissing = false} = {}) {
    if (!context || !dialog()?.open) return;
    const revision = serial;
    const result = await request("/reviews/history/read", base());
    if (revision !== serial) return;
    rows = result.data;
    render(result);
    if (fetchMissing && !result.complete && canFetch()) await start({kind:"history", ...base()});
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
    finally {status.remove(); starting = false;}
    if (revision !== serial) return;
    task = started.task_id;
    await poll(revision);
  }
  async function poll(revision) {
    while (revision === serial && dialog()?.open && task) {
      const detail = await request("/tasks/detail?id=" + encodeURIComponent(task));
      if (revision !== serial) return;
      const result = await request("/reviews/history/read", base());
      if (revision !== serial) return;
      rows = result.data;
      render(result, detail);
      if (!["queued", "running", "cancelling"].includes(detail.status)) {
        task = "";
        if (["failed", "partial", "paused"].includes(detail.status)) message(detail.message || "部分掃描資料未取得，可續跑。", true);
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 1200));
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
    actions.append(button("開啟", () => showAsset(row, index)), button("加入比較", () => addCompare(row, index)));
    line.append(meta, actions);
    return line;
  }
  const categoryKey = row => JSON.stringify([row.section_label || "", row.category_label || ""]);
  function renderScanRows(target, data) {
    target.replaceChildren();
    const records = data.records || [...(data.scans || []), ...(data.unclassified || [])];
    const term = searchText.trim().toLocaleLowerCase();
    const visible = records.filter(row => {
      const categoryMatches = categoryFilter === "all" ||
        (categoryFilter === "eye" && row.eye) ||
        (categoryFilter === "unknown" && !row.category_label) ||
        (categoryFilter.startsWith("category:") && categoryKey(row) === categoryFilter.slice(9));
      return categoryMatches && (!term || [row.category_label, row.section_label, row.date,
        row.section, row.case_no, row.record_type].some(value => (value || "").toLocaleLowerCase().includes(term)));
    });
    target.append(make("p", `顯示 ${visible.length} / ${records.length} 筆掃描病歷`, "scan-caption"));
    if (visible.length) visible.forEach((row, index) => target.append(scanRow(row, index)));
    else target.append(make("p", "此篩選條件沒有掃描病歷。", "scan-caption"));
  }
  function render(result, job = null) {
    const data = result.data, body = content();
    body.replaceChildren();
    if (job && ["queued", "running", "cancelling"].includes(job.status)) body.append(window.JobProgress?.create(job) || make("p", job.message || "掃描資料查詢中…", "scan-caption"));
    if (!data) {
      body.append(make("p", "沒有已存掃描病歷；連線後可取得。", "scan-caption"));
      if (canFetch()) body.append(button("重試取得", () => start({kind:"history", ...base()})));
      return;
    }
    const workspace = make("div", undefined, "scan-browser-workspace");
    const catalog = make("div", undefined, "scan-browser-catalog");
    const previewHost = make("div", undefined, "scan-browser-preview-host");
    previewHost.id = "scanPreviewHost";
    workspace.append(catalog, previewHost);
    body.append(workspace);
    if (context.resource === "scans") {
      catalog.append(make("p", data.history_loaded ?
        `完整歷年索引已保存 · ${data.records?.length || 0} 筆 · ${data.categories?.length || 0} 種院方類別` :
        data.cache_error ? "已存索引無法核對；連線後可重新取得完整類別清單。" :
        data.index_loaded ? "目前顯示舊版索引；連線後會更新類別與日期。" : "尚未取得歷年掃描索引。", "scan-caption"));
      if (data.link_error) catalog.append(make("p", "已存就診連結無法核對；歷年類別清單仍可查看。", "scan-error"));
      if (canFetch()) catalog.append(button("更新索引", () => start({kind:"history", ...base(), force:true})));
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
      const list = make("div", undefined, "scan-group");
      catalog.append(list);
      categorySelect.addEventListener("change", () => {categoryFilter = categorySelect.value; renderScanRows(list, data);});
      search.addEventListener("input", () => {searchText = search.value; renderScanRows(list, data);});
      renderScanRows(list, data);
      if (data.visits_loaded) catalog.append(make("p", `眼科就診參照已核對 ${data.checked_case_count || 0} / ${data.eye_case_count || 0} 次；此數量不影響歷年類別清單。`, "scan-caption"));
      if (!data.links_complete && canFetch()) catalog.append(button("補查眼科就診連結", () => start({kind:"history", ...base(), backfill:true})));
    } else {
      catalog.append(make("p", [data.case?.date, data.case?.section, data.case?.case_no].filter(Boolean).join(" · ") || "該次就診", "scan-caption"));
      if (canFetch()) catalog.append(button("更新該次掃描", () => start({kind:"history", ...base(), force:true})));
      if (data.scans?.length) data.scans.forEach((row, i) => catalog.append(scanRow(row, i)));
      else catalog.append(make("p", "該次 SOAP 未提供掃描病歷。", "scan-caption"));
    }
    previewHost.append(selected || make("p", "從左側選擇掃描病歷，在這裡查看 PDF。", "scan-empty-preview"));
  }
  async function asset(row) {
    const values = {...base(), resource:"scan_asset", reference:row.id};
    let result = await request("/reviews/history/read", values);
    if (!result.data && canFetch()) {
      const started = await request("/tasks/start", {kind:"history", ...values});
      for (;;) {
        const job = await request("/tasks/detail?id=" + encodeURIComponent(started.task_id));
        if (!["queued", "running", "cancelling"].includes(job.status)) {
          if (job.status !== "completed") throw new Error(job.message || "掃描 PDF 尚未取得。");
          break;
        }
        await new Promise(resolve => setTimeout(resolve, 800));
      }
      result = await request("/reviews/history/read", values);
    }
    if (!result.data) throw new Error("此掃描 PDF 尚未保存；離線時無法下載。");
    return result.data;
  }
  async function addCompare(row, index) {
    const revision = serial;
    const file = await asset(row);
    if (revision !== serial || !dialog()?.open) return;
    window.FileCompare.add({account:context.account, mrn:context.mrn, digest:file.digest, mime:file.mime,
      name:`${row.category_label || "掃描病歷"} ${index + 1}`, source:"掃描病歷", date:row.date || ""});
  }
  async function showAsset(row, index) {
    const revision = serial;
    const file = await asset(row);
    if (revision !== serial || !dialog()?.open) return;
    const preview = make("section", undefined, "scan-preview");
    const head = make("div", undefined, "scan-preview-head");
    head.append(make("h3", `${row.category_label || "掃描病歷"} · ${row.date || "日期未提供"}`),
      button("加入比較", () => addCompare(row, index)), button("收起", () => {
        selected = null; selectedId = "";
        document.getElementById("scanPreviewHost")?.replaceChildren(make("p", "從左側選擇掃描病歷，在這裡查看 PDF。", "scan-empty-preview"));
        content().querySelectorAll(".scan-row.active").forEach(line => line.classList.remove("active"));
      }));
    const frame = make(file.mime === "application/pdf" ? "iframe" : "img");
    frame.src = `/api/accounts/${context.account}/analysis/asset?id=${file.digest}`;
    frame.title = "掃描病歷 PDF";
    if (frame.tagName === "IMG") frame.alt = frame.title;
    preview.append(head, frame);
    selected?.remove();
    selected = preview;
    selectedId = row.id;
    document.getElementById("scanPreviewHost").replaceChildren(preview);
    content().querySelectorAll(".scan-row").forEach(line => line.classList.remove("active"));
    const chosen = [...content().querySelectorAll(".scan-row")].find(line => line.dataset.scanId === row.id);
    chosen?.classList.add("active");
    if (matchMedia("(max-width: 760px)").matches) preview.scrollIntoView({block:"start"});
  }
  function reset() {if (dialog()?.open) dialog().close();context = null; task = ""; starting = false; serial++;}
  document.addEventListener("DOMContentLoaded", mount);
  return {open, image, reset};
})();
