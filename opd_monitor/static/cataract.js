"use strict";

// The embedded cataract view reuses the account-scoped analysis cache and
// requests order contents only after a clinician opens a specific order.
window.CataractUI = (() => {
  let status = null, orderSequence = 0;
  let currentKey = "", currentState = null, currentUI = null;
  const patientStates = new Map();
  let expandedStates = new Map();
  try {
    if (parent !== window && parent.location.origin === location.origin) {
      parent.CataractExpanded ||= new Map();
      expandedStates = parent.CataractExpanded;
    }
  } catch (_) { /* The tool can also run outside the same-origin workbench. */ }
  const activeView = () => !!embeddedAccount && $("#analysisView").value === "cataract";

  function stateFor(member) {
    const key = [embeddedAccount, currentCohort?.id, member.mrn].join(":");
    const expansionKey = [embeddedAccount, member.mrn].join(":");
    if (!patientStates.has(key)) patientStates.set(key, {
      view: "soap", mobile: "numeric", numericOpen: true, expansionKey,
      expanded: new Set(expandedStates.get(expansionKey) || []),
      initialized: expandedStates.has(expansionKey), orderInitialized: false, orderExpanded: new Set(),
      selectedOrderId: "", orderRow: null, report: null
    });
    if (key !== currentKey) {
      currentKey = key;
      orderSequence++;
      patientStates.get(key).view = "soap";
    }
    currentState = patientStates.get(key);
    return currentState;
  }

  function layout() {
    const enabled = activeView();
    $("#cataractLayout").classList.toggle("active", enabled);
    $("#cataractPatients").hidden = !enabled;
    $("#analysisPatient").closest("label").hidden = enabled;
    $("#cataractUpdate").hidden = !enabled;
    $("#reloadAnalysis").hidden = enabled;
    if (enabled) renderPatients();
  }

  async function refreshStatus() {
    layout();
    if (!activeView() || !currentCohort) {status = null; renderPatients(); return null;}
    status = await ap("cataract/status", {cohort_id: currentCohort.id});
    renderPatients();
    return status;
  }

  function renderPatients() {
    const list = $("#cataractPatients");
    list.replaceChildren();
    if (!activeView() || !currentCohort) return;
    const heading = el("h3", `病人清單 · ${currentCohort.members.length} 位`);
    list.append(heading);
    const states = new Map((status?.members || []).map(row => [row.mrn, row]));
    for (const member of currentCohort.members) {
      const state = states.get(member.mrn);
      const label = state?.ready ? "已保存" : status?.active_run_id ? "等待／抓取中" : "待補抓";
      const item = button("", () => action(async () => {
        $("#analysisPatient").value = member.mrn;
        await loadAnalysisResult();
        $("#analysisResults").scrollTop = 0;
      }), "cataract-patient");
      item.setAttribute("aria-current", String($("#analysisPatient").value === member.mrn));
      item.append(el("strong", member.name || "姓名未提供"), el("span", member.mrn, "small muted"),
        el("span", label, "cataract-patient-status"));
      list.append(item);
    }
  }

  async function start({refresh: update = false} = {}) {
    if (!currentCohort || config?.read_only) return;
    const current = status || await refreshStatus();
    if (!update && (current?.ready || current?.active_run_id)) return;
    if (update && current?.active_run_id) {notify("目前仍在抓取，請等待完成後再更新。", true); return;}
    const pending = JobProgress.pending("#analysisRuns", "白內障術前分析", "正在建立背景抓取任務…");
    try {
      await saveAnalysisAccounts();
      const request = !update && current?.resume_run_id ? {resume: current.resume_run_id} :
        {cohort_id: currentCohort.id, modules: ["cataract"], start: "", end: "", refresh: update, force: false};
      await ap("start", request);
      parent.postMessage({type: "bot:task-started"}, location.origin);
      await refresh();
      await refreshStatus();
      await loadAnalysisResult();
    } finally {pending?.remove();}
  }

  function numericPanel(data, state) {
    const patientKey = state.expansionKey;
    const section = el("section", undefined, "cataract-section cataract-numeric");
    section.append(el("h3", "歷年眼科數值"),
      el("p", "六種檢查分組；可同時展開，最新紀錄列在最上方。", "small muted"));
    const groups = analysisState.modules.cataract.numeric.map(exam => {
      const entries = [];
      for (const [rowIndex, row] of (data.numeric || []).entries()) {
        if (!(row.exams || []).includes(exam)) continue;
        const cells = (row.cells || []).filter(cell => cell.exam === exam);
        for (const [cellIndex, cell] of (cells.length ? cells : [null]).entries())
          entries.push({row, cell, rowIndex, cellIndex});
      }
      entries.sort((a, b) => (b.cell?.date || b.row.date || "").localeCompare(a.cell?.date || a.row.date || "") ||
        a.rowIndex - b.rowIndex || a.cellIndex - b.cellIndex);
      return {exam, entries};
    });
    if (!state.initialized && groups.some(group => group.entries.length)) {
      state.expanded.add(groups.find(group => group.entries.length).exam);
      expandedStates.set(patientKey, new Set(state.expanded));
      state.initialized = true;
    }
    for (const {exam, entries} of groups) {
      const group = el("details", undefined, "cataract-numeric-group");
      group.dataset.exam = exam;
      group.open = state.expanded.has(exam);
      const summary = el("summary"), title = el("strong", exam);
      const latest = entries[0], when = latest?.cell?.date || latest?.row.date || "日期未提供";
      const value = latest?.cell ? [latest.cell.side, latest.cell.raw + (latest.cell.unit ? " " + latest.cell.unit : "")]
        .filter(Boolean).join(" ") : "需核對原始表格";
      summary.append(title, el("span", `${entries.length} 筆`, "cataract-numeric-count"),
        el("span", latest ? `最新 ${when} · ${value}` : "尚無資料", "cataract-numeric-latest"));
      group.append(summary);
      if (entries.length) {
        const list = el("div", undefined, "cataract-reading-list");
        for (const {row, cell} of entries) {
          const card = el("div", undefined, "cataract-reading");
          const line = el("div", undefined, "cataract-reading-line");
          line.append(el("time", cell?.date || row.date || "日期未提供"),
            el("span", cell?.side || "—"),
            el("strong", cell ? cell.raw + (cell.unit ? " " + cell.unit : "") : "需核對"));
          const source = el("details"), wrap = el("div", undefined, "table-wrap");
          wrap.append(rawTable(row));
          source.append(el("summary", "原始表格"), wrap);
          card.append(line, el("span", cell?.metric || row.title || "項目未提供", "cataract-reading-metric"), source);
          list.append(card);
        }
        group.append(list);
      }
      group.addEventListener("toggle", () => {
        if (group.open) state.expanded.add(exam); else state.expanded.delete(exam);
        expandedStates.set(patientKey, new Set(state.expanded));
      });
      section.append(group);
    }
    return section;
  }

  function soapSection(data) {
    const section = el("section", undefined, "cataract-section cataract-soap");
    const record = data.latest_soap?.record;
    section.append(el("h3", "最新眼科門診 SOAP"));
    if (!record) {
      const message = data.latest_soap?.status === "no_visit" ? "沒有可核對的眼科門診就診。" :
        data.latest_soap?.status === "missing" ? "眼科門診就診沒有可讀的 SOAP。" :
        "尚未取得 SOAP；已保存的其他資料仍可檢閱。";
      section.append(el("p", message, "small muted"));
    } else {
      section.append(el("p", [record.date, record.section, record.case_no].filter(Boolean).join(" · "), "small muted"));
      section.append(SOAPView.create(record));
    }
    return section;
  }

  function orderPanel(data, state) {
    const section = el("section", undefined, "cataract-section cataract-orders");
    section.append(el("h3", "歷年醫囑"), el("p", "依名稱分類；選取一筆後才取得報告。", "small muted"));
    const preferred = analysisState.modules.cataract.orders;
    const groups = new Map();
    for (const row of data.orders || []) {
      const name = row.name?.trim() || "名稱未提供";
      if (!groups.has(name)) groups.set(name, []);
      if (!groups.get(name).some(item => item.id === row.id)) groups.get(name).push(row);
    }
    if (!groups.size) {section.append(el("p", "尚無符合的醫囑。", "small muted")); return section;}
    const rank = name => {
      const row = groups.get(name)[0];
      const index = preferred.findIndex(value => (row.exams || []).includes(value));
      return index < 0 ? preferred.length : index;
    };
    const names = [...groups.keys()].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
    if (!state.orderInitialized) {state.orderExpanded.add(names[0]); state.orderInitialized = true;}
    for (const name of names) {
      const rows = groups.get(name).sort((a, b) => (b.date || "").localeCompare(a.date || ""));
      const group = el("details", undefined, "cataract-order-group");
      group.open = state.orderExpanded.has(name);
      group.append(el("summary", `${name} · ${rows.length} 筆`));
      for (const row of rows) {
        const control = button("", () => action(() => openOrder(row)), "cataract-order-row");
        control.dataset.orderId = row.id;
        control.setAttribute("aria-current", String(row.id === state.selectedOrderId));
        control.append(el("strong", row.date || "日期未提供"),
          el("span", row.report_loaded ? "已保存" : "按需取得", "small muted"));
        group.append(control);
      }
      group.addEventListener("toggle", () => {
        if (group.open) state.orderExpanded.add(name); else state.orderExpanded.delete(name);
      });
      section.append(group);
    }
    return section;
  }

  function mobileSwitch(view, state) {
    const nav = el("nav", undefined, "cataract-mobile-switch");
    for (const [key, label] of [["numeric", "數值"], ["orders", "醫囑"], ["files", "檔案"]]) {
      const control = button(label, () => {
        state.mobile = key;
        if (key === "numeric" && state.view === "compare") state.numericOpen = true;
        currentUI.workspace.dataset.mobile = key;
        currentUI.comparison.dataset.mobile = key;
        currentUI.comparison.classList.toggle("numeric-closed", !state.numericOpen);
        currentUI.railToggle.setAttribute("aria-expanded", String(state.numericOpen));
        currentUI.railToggle.textContent = state.numericOpen ? "收合數值" : "展開數值";
        for (const button of $$(".cataract-mobile-switch button"))
          button.setAttribute("aria-pressed", String(button.dataset.mobile === key));
      }, "quiet-button");
      control.dataset.mobile = key;
      control.setAttribute("aria-pressed", String(state.mobile === key));
      nav.append(control);
    }
    nav.setAttribute("aria-label", view === "compare" ? "比較畫面內容" : "數值與醫囑內容");
    return nav;
  }

  function showView(view) {
    if (!currentUI || !currentState) return;
    currentState.view = view;
    for (const [name, page] of Object.entries(currentUI.pages)) page.hidden = name !== view;
    for (const [name, control] of Object.entries(currentUI.tabs))
      control.setAttribute("aria-pressed", String(name === view));
    const target = view === "compare" ? currentUI.comparison : currentUI.workspace;
    target.prepend(currentUI.orders);
    target.prepend(currentUI.numeric);
    currentUI.comparison.classList.toggle("numeric-closed", !currentState.numericOpen);
    currentUI.railToggle.setAttribute("aria-expanded", String(currentState.numericOpen));
    currentUI.railToggle.textContent = currentState.numericOpen ? "收合數值" : "展開數值";
    currentUI.workspace.dataset.mobile = currentState.mobile;
    currentUI.comparison.dataset.mobile = currentState.mobile;
    for (const control of $$(".cataract-mobile-switch button"))
      control.setAttribute("aria-pressed", String(control.dataset.mobile === currentState.mobile));
    if (view === "compare") window.FileCompare.renderInline(currentUI.files);
  }

  function render(data) {
    layout(); renderPatients();
    const area = $("#analysisResults"), member = data.member, state = stateFor(member);
    area.replaceChildren();
    if (state.selectedOrderId) {
      state.orderRow = (data.orders || []).find(row => row.id === state.selectedOrderId) || null;
      if (!state.orderRow) {state.selectedOrderId = ""; state.report = null;}
    }
    const heading = el("div", undefined, "cataract-result-heading");
    heading.append(el("h2", `${member.name || "姓名未提供"} · ${member.mrn}`, "cataract-identity"));
    const tabs = {}, nav = el("nav", undefined, "cataract-view-nav");
    nav.setAttribute("aria-label", "術前分析檢視");
    for (const [key, label] of [["soap", "SOAP"], ["work", "數值與醫囑"], ["compare", "報告比較"]]) {
      const control = button(label, () => showView(key), "quiet-button");
      control.dataset.view = key; control.setAttribute("aria-pressed", String(state.view === key));
      tabs[key] = control; nav.append(control);
    }
    const count = el("span", `已選 ${window.FileCompare.count()} / 6 份`, "cataract-compare-count");
    nav.append(count); heading.append(nav);
    const soapPage = el("div", undefined, "cataract-view cataract-soap-view");
    soapPage.append(soapSection(data), button("查看數值與醫囑 →", () => showView("work"), "primary"));
    const workPage = el("div", undefined, "cataract-view");
    const workspace = el("div", undefined, "cataract-workspace");
    const numeric = numericPanel(data, state), orders = orderPanel(data, state);
    const preview = el("section", undefined, "cataract-section cataract-report-preview");
    workPage.append(mobileSwitch("work", state), workspace);
    workspace.append(numeric, orders, preview);
    const comparePage = el("div", undefined, "cataract-view");
    const compareHead = el("div", undefined, "cataract-compare-heading");
    const railToggle = button("", () => {
      state.numericOpen = !state.numericOpen;
      showView("compare");
    }, "quiet-button");
    const back = button("選取更多醫囑", () => showView("work"), "quiet-button");
    compareHead.append(el("h3", "報告比較"), railToggle, back);
    const comparison = el("div", undefined, "cataract-comparison");
    const files = el("div", undefined, "cataract-files");
    comparePage.append(compareHead, mobileSwitch("compare", state), comparison);
    comparison.append(files);
    currentUI = {pages:{soap:soapPage, work:workPage, compare:comparePage}, tabs,
      count, numeric, orders, preview, workspace, comparison, files, railToggle};
    renderOrder(state.report, state.orderRow);
    area.append(heading, soapPage, workPage, comparePage);
    showView(state.view);
    syncAttachments();
  }

  function syncAttachments() {
    if (!activeView()) return;
    for (const checkbox of $$("#analysisResults input[data-compare-digest]"))
      checkbox.checked = window.FileCompare.has(checkbox.dataset.compareDigest);
    const control = document.getElementById("cataractCompareOpen");
    if (control) {
      control.textContent = `查看比較（${window.FileCompare.count()} / 6）`;
      control.disabled = !window.FileCompare.count();
    }
    if (currentUI) {
      currentUI.count.textContent = `已選 ${window.FileCompare.count()} / 6 份`;
      currentUI.tabs.compare.textContent = `報告比較（${window.FileCompare.count()}）`;
    }
  }

  function renderOrder(value, fallback) {
    const area = currentUI?.preview, row = value?.order || fallback;
    if (!area) return;
    area.replaceChildren();
    area.append(el("h3", "醫囑報告"));
    if (!row) {
      area.append(el("p", "從醫囑清單選取一筆，這裡會顯示附件與報告。", "empty-result"));
      return;
    }
    area.append(el("p", [row.date, row.name, row.case_no].filter(Boolean).join(" · "), "small muted"));
    const actions = el("div", undefined, "cataract-preview-actions");
    if (!config?.read_only && value)
      actions.append(button("更新此報告", () => action(() => openOrder(row, true)), "quiet-button"));
    const open = button(`查看比較（${window.FileCompare.count()} / 6）`, () => showView("compare"), "quiet-button");
    open.disabled = !window.FileCompare.count();
    open.id = "cataractCompareOpen"; actions.append(open); area.append(actions);
    if (!value) {
      area.append(el("p", config?.read_only ? "此報告尚未保存，離線無法開啟。" : "正在讀取已存報告…", "small muted"));
      syncAttachments();
      return;
    }
    for (const [index, asset] of (value.assets || []).entries()) {
      const box = el("div", undefined, "cataract-asset");
      const url = scopedPath("/api/analysis/asset?id=" + encodeURIComponent(asset.digest));
      const file = el(asset.mime === "application/pdf" ? "iframe" : "img");
      file.src = url; file.title = `${row.name} 附件 ${index + 1}`;
      if (file.tagName === "IMG") file.alt = file.title;
      const label = el("label", undefined, "check-label"), check = el("input");
      check.type = "checkbox"; check.dataset.compareDigest = asset.digest;
      check.setAttribute("aria-label", `將 ${row.name} 附件 ${index + 1} 加入比較`);
      check.addEventListener("change", () => {
        if (check.checked) window.FileCompare.add({account: embeddedAccount,
          mrn: currentCohort.members.find(member => member.mrn === $("#analysisPatient").value).mrn,
          digest: asset.digest, mime: asset.mime, name: `${row.name} 附件 ${index + 1}`,
          source: "醫囑報告", date: row.date || ""});
        else window.FileCompare.remove(asset.digest);
      });
      label.append(check, el("span", "加入比較")); box.append(file, label); area.append(box);
    }
    for (const report of value.texts || []) {
      const detail = el("details"), summary = el("summary", "文字報告");
      detail.append(summary, el("pre", report.text || Object.entries(report.fields || {})
        .map(([key, item]) => `${key}：${item}`).join("\n"))); area.append(detail);
    }
    for (const detail of value.details || []) if (Object.keys(detail.fields || {}).length) {
      const block = el("details"); block.append(el("summary", "醫囑明細"),
        el("pre", Object.entries(detail.fields).map(([key, item]) => `${key}：${item}`).join("\n")));
      area.append(block);
    }
    if (value.issues?.length) area.append(el("p", value.issues.map(item => item.message).join("；"), "form-error"));
    if (!value.assets?.length && !value.texts?.length && !value.details?.length)
      area.append(el("p", "此醫囑沒有可顯示的報告內容。", "small muted"));
    syncAttachments();
  }

  async function openOrder(row, force = false) {
    const member = currentCohort?.members.find(item => item.mrn === $("#analysisPatient").value);
    if (!member || member.account_id !== embeddedAccount) throw new Error("此病人未指定給目前登入帳號。");
    const request = {cohort_id: currentCohort.id, mrn: member.mrn,
      resource: "order_report", reference: row.id};
    const sequence = ++orderSequence, key = currentKey, state = currentState;
    state.selectedOrderId = row.id; state.orderRow = row; state.report = null;
    state.mobile = "files";
    showView("work");
    for (const control of $$(".cataract-order-row"))
      control.setAttribute("aria-current", String(control.dataset.orderId === row.id));
    renderOrder(null, row);
    const stillCurrent = () => sequence === orderSequence && key === currentKey &&
      currentCohort?.id === request.cohort_id && $("#analysisPatient").value === member.mrn &&
      state.selectedOrderId === row.id;
    let stored;
    try {
      stored = await api("/api/reviews/history/read", request);
    } catch (error) {
      if (stillCurrent()) currentUI.preview.append(el("p", error.message, "form-error"));
      throw error;
    }
    if (!stillCurrent()) return;
    state.report = stored.data;
    renderOrder(stored.data, row);
    if ((stored.complete && !force) || config?.read_only) return;
    const progress = el("div"); currentUI.preview.prepend(progress);
    JobProgress.pending(progress, "醫囑報告", "正在取得報告與附件…");
    try {
      const started = await api("/api/tasks/start", {kind: "history", ...request, force});
      for (;;) {
        if (!stillCurrent()) return;
        const task = await api("/api/tasks/detail?id=" + encodeURIComponent(started.task_id));
        JobProgress.show(progress, task);
        if (!active.has(task.status)) {
          const updated = await api("/api/reviews/history/read", request);
          if (!stillCurrent()) return;
          state.report = updated.data;
          renderOrder(updated.data, row);
          await loadAnalysisResult();
          if (stillCurrent() && task.status !== "completed")
            currentUI.preview.append(el("p", task.message || "報告部分未取得，可稍後再試。", "form-error"));
          return;
        }
        await new Promise(resolve => setTimeout(resolve, 700));
      }
    } catch (error) {
      if (stillCurrent()) currentUI.preview.append(el("p", error.message, "form-error"));
    } finally {progress.remove();}
  }

  $("#cataractUpdate").addEventListener("click", () => action(() => start({refresh: true})));
  window.addEventListener("filecomparechange", syncAttachments);
  window.addEventListener("filecomparemessage", event => {if (activeView()) notify(event.detail, true);});
  window.addEventListener("message", event => {
    if (event.origin !== location.origin || event.source !== parent ||
        event.data?.type !== "bot:compare-inline-open" || !activeView()) return;
    if (event.data.account === embeddedAccount && event.data.mrn === $("#analysisPatient").value)
      showView("compare");
  });
  return {layout, refreshStatus, renderPatients, render, autoStart: () => start()};
})();
