"use strict";

window.CataractScans = (() => {
  const categoryKey = row => JSON.stringify([row.section_label || "", row.category_label || ""]);
  const label = row => row.category_label || (row.section ? "SOAP 掃描" : "掃描病歷");
  const title = row => `${label(row)} · ${row.date || "日期未提供"}`;

  function create({account, cohortId, mrn, soap, canFetch, readOnly, isCurrent, biometryRequest, textOnly, mountBiometryWorkspace}) {
    const scope = {cohort_id: cohortId, mrn};
    const request = (path, body) => api("/api" + path, body);
    const files = new Map(), pendingFiles = new Map(), compareIntents = new Map();
    let catalogIds = new Set();
    let result, indexBusy = false, autoFetchAttempted = false, selectedId = "", previewRevision = 0;
    let disposed = false, previewExtraction;
    const current = () => !disposed && isCurrent();
    const page = el("div", undefined, "cataract-view cataract-scans-view");
    const workspace = el("div", undefined, "cataract-scans-workspace");
    const navigation = el("nav", undefined, "cataract-mobile-switch");
    navigation.setAttribute("aria-label", "病歷、掃描清單與 PDF");
    const views = new Map();
    function show(subview) {
      page.dataset.mobile = subview;
      for (const [name, control] of views) control.setAttribute("aria-pressed", String(subview === name));
    }
    for (const [key, name] of [["soap", "SOAP"], ["catalog", "掃描清單"], ["preview", "PDF"]]) {
      const control = button(name, () => show(key), "quiet-button");
      views.set(key, control);
      navigation.append(control);
    }
    show("catalog");

    const catalog = el("section", undefined, "cataract-section cataract-scans-catalog");
    const controls = el("div", undefined, "cataract-scans-controls");
    const update = button("↻", () => void updateIndex(true), "cataract-order-refresh quiet-button");
    update.title = "更新歷年掃描索引";
    update.setAttribute("aria-label", update.title);
    const reread = button("重讀索引", () => void load(), "quiet-button");
    reread.hidden = true;
    const status = el("p", "", "cataract-scans-status small muted");
    status.setAttribute("role", "status");
    const filters = el("div", undefined, "cataract-scans-filters");
    const category = document.createElement("select");
    const categoryLabel = el("label", "病歷類別");
    categoryLabel.append(category);
    const search = el("input");
    search.type = "search";
    search.placeholder = "類別、日期或就診資料";
    const searchLabel = el("label", "搜尋掃描病歷");
    searchLabel.append(search);
    filters.append(categoryLabel, searchLabel);
    filters.hidden = true;
    const list = el("div", undefined, "cataract-scans-list");
    const preview = el("section", undefined, "cataract-section cataract-scans-preview");
    const initialPreview = el("p", "選擇一份掃描病歷以查看 PDF。", "small muted");
    preview.append(initialPreview);

    const biometry = biometryRequest && window.CataractBiometry?.create({account, cohortId, mrn, source:"scans",
      readonly:readOnly, request:biometryRequest, current, textOnly,
      mountWorkspace: mountBiometryWorkspace,
      prepareDocument:async (doc, selectionCurrent) => {
        if (selectionCurrent() && current()) return readAsset(doc);
      }});
    controls.append(update, reread);
    if (biometry) controls.append(biometry.element);
    catalog.append(el("h3", "歷年掃描病歷"), controls, status, filters, list);
    workspace.append(soap, catalog, preview);
    page.append(navigation, workspace);
    category.addEventListener("change", renderRows);
    search.addEventListener("input", renderRows);

    function setStatus(message, error = false) {
      status.textContent = message;
      status.classList.toggle("form-error", error);
      status.classList.toggle("muted", !error);
    }
    const records = () => result?.data?.records || [...(result?.data?.scans || []), ...(result?.data?.unclassified || [])];

    function render(value) {
      const selectedDigest = files.get(selectedId)?.digest;
      result = value;
      reread.hidden = true;
      const rows = records();
      catalogIds = new Set(rows.map(row => row.id));
      files.clear();
      for (const row of rows) if (row.asset) files.set(row.id, row.asset);
      for (const [id, intent] of compareIntents)
        if (!catalogIds.has(id) || intent.digest && intent.digest !== files.get(id)?.digest) compareIntents.delete(id);
      if (selectedId && (!catalogIds.has(selectedId) || selectedDigest && selectedDigest !== files.get(selectedId)?.digest)) {
        selectedId = "";
        previewRevision++;
        previewExtraction?.dispose();
        previewExtraction = null;
        preview.replaceChildren(initialPreview);
      }
      const prior = category.value || "eye";
      const categories = [["eye", "眼科紀錄"], ["all", "全部類別"], ["unknown", "類別未提供"],
        ...(value.data?.categories || []).map(row => [categoryKey(row),
          [row.section_label, row.category_label, `(${row.count})`].filter(Boolean).join(" ")])];
      category.replaceChildren(...categories.map(([key, name]) => {
        const option = el("option", name);
        option.value = key;
        return option;
      }));
      category.value = categories.some(([key]) => key === prior) ? prior : "eye";
      filters.hidden = !rows.length;
      if (biometry) {
        const documents = new Map(rows.filter(row => !row.asset || row.asset.mime === "application/pdf")
          .map(row => [row.id, {id:row.id, label:label(row), date:row.date, digest:row.asset?.digest}]));
        biometry.setDocuments([...documents.values()], {allowAll:!!value.complete});
      }
      renderRows();
      setStatus(value.complete ? "" : rows.length ? "目前顯示已存的部分掃描索引。" : "尚未取得歷年掃描索引。");
    }
    function renderRows() {
      const term = search.value.trim().toLocaleLowerCase();
      const visible = records().filter(row => {
        const matches = category.value === "all" || (category.value === "eye" && row.eye) ||
          (category.value === "unknown" && !row.category_label) || categoryKey(row) === category.value;
        return matches && (!term || [row.category_label, row.section_label, row.date, row.section, row.case_no, row.record_type]
          .some(value => String(value || "").toLocaleLowerCase().includes(term)));
      });
      list.replaceChildren();
      if (!records().length) {
        if (result?.complete) list.append(el("p", "此病人沒有歷年掃描病歷。", "small muted"));
        return;
      }
      list.append(el("p", `顯示 ${visible.length} / ${records().length} 筆`, "small muted"));
      if (!visible.length) list.append(el("p", "此篩選條件沒有掃描病歷。", "small muted"));
      for (const row of visible) {
        const line = el("div", undefined, "cataract-scans-row");
        const open = button("", () => void openPreview(row), "cataract-order-row");
        open.dataset.scanReference = row.id;
        open.title = title(row);
        open.append(el("strong", title(row)),
          el("span", [row.section || row.section_label, row.case_no, row.record_type].filter(Boolean).join(" · "), "small muted"));
        line.append(comparison(row), open);
        list.append(line);
      }
      syncControls();
    }
    function comparison(row) {
      const check = el("input");
      check.type = "checkbox";
      check.dataset.scanCompare = row.id;
      check.setAttribute("aria-label", `將 ${title(row)} 加入比較`);
      check.addEventListener("change", () => {
        if (!current() || !catalogIds.has(row.id)) return;
        const file = files.get(row.id);
        const intent = {checked:check.checked};
        compareIntents.set(row.id, intent);
        if (file || !intent.checked) return applyComparison(row, intent, file);
        return readAsset(row).then(asset => applyComparison(row, intent, asset))
          .catch(error => rejectComparison(row.id, intent, error));
      });
      return check;
    }
    function applyComparison(row, intent, file) {
      if (!current() || compareIntents.get(row.id) !== intent) return;
      try {
        if (!file) compareIntents.delete(row.id);
        else {
          intent.digest = file.digest;
          if (intent.checked) window.FileCompare.add({account, mrn, digest:file.digest, mime:file.mime,
            name:label(row), source:"掃描病歷", date:row.date || ""});
          else window.FileCompare.remove(file.digest);
        }
      } catch (error) {
        rejectComparison(row.id, intent, error);
        return;
      }
      syncControls();
    }
    function rejectComparison(id, intent, error) {
      if (!current() || compareIntents.get(id) !== intent) return;
      compareIntents.delete(id);
      syncControls();
      notify(error.message, true);
    }
    function comparisonFailed() {
      for (const [id, intent] of compareIntents) if (intent.digest) compareIntents.delete(id);
      syncControls();
    }
    function syncControls() {
      const online = canFetch();
      update.hidden = !online;
      update.disabled = indexBusy;
      reread.disabled = indexBusy;
      for (const [id, intent] of compareIntents)
        if (intent.digest && window.FileCompare.has(intent.digest) === intent.checked) compareIntents.delete(id);
      for (const control of list.querySelectorAll("[data-scan-reference]")) {
        const id = control.dataset.scanReference;
        control.disabled = !files.has(id) && !online;
        control.setAttribute("aria-current", String(id === selectedId));
      }
      for (const check of page.querySelectorAll("[data-scan-compare]")) {
        const id = check.dataset.scanCompare, file = files.get(id), intent = compareIntents.get(id);
        const busy = pendingFiles.has(id) || !!intent;
        check.disabled = busy || !file && !online;
        if (busy) check.setAttribute("aria-busy", "true");
        else check.removeAttribute("aria-busy");
        check.checked = intent ? intent.checked : !!file && window.FileCompare.has(file.digest);
      }
    }
    async function load() {
      if (!current() || indexBusy) return;
      indexBusy = true;
      syncControls();
      setStatus("正在讀取已存掃描資料…");
      try {
        const value = await request("/reviews/history/read", {...scope, resource:"scans"});
        if (current()) render(value);
      } catch (error) {
        if (current()) {setStatus(error.message, true); reread.hidden = false;}
      } finally {
        indexBusy = false;
        if (current()) syncControls();
      }
      if (current() && result) maybeFetchIndex();
    }
    function maybeFetchIndex() {
      if (!result?.complete && canFetch() && !autoFetchAttempted) void updateIndex(false);
    }
    async function updateIndex(force) {
      if (!current() || indexBusy || !canFetch()) return;
      indexBusy = autoFetchAttempted = true;
      syncControls();
      setStatus("正在取得歷年掃描索引…");
      try {
        const values = {...scope, resource:"scans"};
        const job = await window.ScanBrowser.runResource(request, {...values, force}, current, window.JobProgress.publish);
        if (!job || !current()) return;
        const value = await request("/reviews/history/read", values);
        if (!current()) return;
        render(value);
        if (job.status !== "completed") setStatus(job.message || "掃描索引尚未取得，可再更新。", true);
      } catch (error) {
        if (current()) setStatus(error.message, true);
      } finally {
        indexBusy = false;
        if (current()) syncControls();
      }
    }
    function readAsset(row) {
      const verified = () => current() && catalogIds.has(row.id);
      if (!verified()) return Promise.resolve(null);
      if (files.has(row.id)) return Promise.resolve(files.get(row.id));
      if (!pendingFiles.has(row.id)) {
        const values = {...scope, resource:"scan_asset", reference:row.id};
        const pending = window.ScanBrowser.readAsset(request, values, canFetch, verified, window.JobProgress.publish)
          .then(file => {
            if (!file || !verified()) return null;
            if (!files.has(row.id)) files.set(row.id, file);
            return files.get(row.id);
          })
          .finally(() => {pendingFiles.delete(row.id); if (current()) syncControls();});
        pendingFiles.set(row.id, pending);
        syncControls();
      }
      return pendingFiles.get(row.id);
    }
    async function openPreview(row) {
      if (!current() || !catalogIds.has(row.id)) return;
      show("preview");
      if (window.matchMedia?.("(max-width: 780px)").matches) views.get("preview").focus();
      if (selectedId === row.id && preview.querySelector("iframe,img")) return;
      const revision = ++previewRevision;
      selectedId = row.id;
      previewExtraction?.dispose();
      previewExtraction = null;
      syncControls();
      const waiting = el("p", "正在讀取掃描 PDF…", "small muted");
      waiting.setAttribute("role", "status");
      preview.replaceChildren(waiting);
      try {
        const file = await readAsset(row);
        if (!file || !current() || revision !== previewRevision) return;
        const head = el("div", undefined, "cataract-scans-preview-head");
        head.append(el("h3", title(row)));
        const pdf = file.mime === "application/pdf";
        const url = window.LocalSession.resourceURL(scopedPath("/api/analysis/asset?id=" + encodeURIComponent(file.digest)));
        const viewer = el(pdf ? "iframe" : "img");
        viewer.title = title(row);
        if (!pdf) viewer.alt = viewer.title;
        viewer.src = pdf ? ClinicalUI.pdfURL(url) : url;
        if (pdf && !readOnly && biometry) {
          previewExtraction = window.CataractUI.createPdfExtraction(biometry, {id:row.id, digest:file.digest}, viewer);
          head.append(previewExtraction.element);
          workspace.append(previewExtraction.panel);
        } else if (!pdf) {
          head.append(ClinicalUI.assetTools({url, asset:file, name:[row.date,label(row)].filter(Boolean).join(" "), image:viewer, compact:true}));
        }
        preview.replaceChildren(head, viewer);
        previewExtraction?.activate();
        syncControls();
      } catch (error) {
        if (current() && revision === previewRevision) preview.replaceChildren(el("p", error.message, "form-error"));
      }
    }
    function activate() {
      if (!current()) return;
      syncControls();
      if (result) maybeFetchIndex();
      else if (reread.hidden) void load();
    }
    function clear() {
      disposed = true;
      previewExtraction?.dispose();
      previewExtraction = null;
      biometry?.clear();
      files.clear();
      catalogIds.clear();
      pendingFiles.clear();
      compareIntents.clear();
      result = null;
      preview.replaceChildren(initialPreview);
      list.replaceChildren();
    }
    return {element:page, activate, clear, syncControls, comparisonFailed, dismissBiometry:() => biometry?.dismiss?.(),
      refreshBiometryReview:() => previewExtraction?.refreshReview(),
      updateSOAP:next => {workspace.firstElementChild.replaceWith(next);}};
  }
  return {create};
})();
