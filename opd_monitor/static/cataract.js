"use strict";

// The embedded cataract view reuses the account-scoped analysis cache and
// requests order contents only after a clinician opens a specific order.
window.CataractUI = (() => {
  let status = null, orderSequence = 0;
  const activeView = () => !!embeddedAccount && $("#analysisView").value === "cataract";

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

  function numericTable(data) {
    const section = el("section", undefined, "cataract-section cataract-numeric");
    section.append(el("h3", "歷年眼科數值類報告"));
    const order = analysisState.modules.cataract.numeric;
    const entries = [];
    for (const [index, exam] of order.entries()) {
      for (const row of data.numeric.filter(value => value.exams.includes(exam))) {
        const cells = (row.cells || []).filter(cell => cell.exam === exam);
        for (const cell of cells.length ? cells : [null]) entries.push({index, exam, row, cell});
      }
    }
    entries.sort((a, b) => a.index - b.index || (b.cell?.date || b.row.date || "").localeCompare(a.cell?.date || a.row.date || "") || a.row.id.localeCompare(b.row.id));
    if (!entries.length) {section.append(el("p", "尚無已保存的眼科數值報告。", "small muted")); return section;}
    const wrap = el("div", undefined, "table-wrap"), table = el("table", undefined, "source-table");
    const head = el("thead"), headRow = el("tr"), body = el("tbody");
    for (const title of ["檢查", "日期", "眼別", "項目", "結果", "原始資料"]) {
      const th = el("th", title); th.scope = "col"; headRow.append(th);
    }
    head.append(headRow);
    for (const {exam, row, cell} of entries) {
      const tr = el("tr"), source = el("details"), summary = el("summary", "查看原始表格");
      source.append(summary, rawTable(row));
      tr.append(el("td", exam), el("td", cell?.date || row.date || "日期未提供"),
        el("td", cell?.side || "—"), el("td", cell?.metric || row.title || "—"),
        el("td", cell ? cell.raw + (cell.unit ? " " + cell.unit : "") : "需核對原始表格"));
      const td = el("td"); td.append(source); tr.append(td); body.append(tr);
    }
    table.append(head, body); wrap.append(table); section.append(wrap);
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

  function orderTable(data) {
    const section = el("section", undefined, "cataract-section cataract-orders");
    section.append(el("h3", "歷年眼科醫囑報告"));
    const preferred = analysisState.modules.cataract.orders;
    const rows = [...data.orders].sort((a, b) => preferred.findIndex(name => a.exams.includes(name)) -
      preferred.findIndex(name => b.exams.includes(name)) || b.date.localeCompare(a.date));
    if (!rows.length) {section.append(el("p", "尚無符合的醫囑。", "small muted")); return section;}
    const wrap = el("div", undefined, "table-wrap"), table = el("table", undefined, "source-table");
    const head = el("thead"), heading = el("tr"), body = el("tbody");
    for (const title of ["日期", "醫囑名稱", "報告", "檢視"]) {
      const th = el("th", title); th.scope = "col"; heading.append(th);
    }
    head.append(heading);
    for (const row of rows) {
      const tr = el("tr"), actionCell = el("td");
      actionCell.append(button("查看報告", () => action(() => openOrder(row)), "quiet-button"));
      tr.append(el("td", row.date || "日期未提供"), el("td", row.name),
        el("td", row.report_loaded ? "已保存" : "按需取得"), actionCell);
      body.append(tr);
    }
    table.append(head, body); wrap.append(table); section.append(wrap);
    return section;
  }

  function render(data) {
    layout();
    renderPatients();
    const area = $("#analysisResults");
    area.replaceChildren();
    const member = data.member;
    area.append(el("h2", `${member.name || "姓名未提供"} · ${member.mrn}`, "cataract-identity"),
      numericTable(data), soapSection(data), orderTable(data));
  }

  function syncAttachments() {
    for (const checkbox of $$("#cataractOrderDialog input[data-compare-digest]"))
      checkbox.checked = window.FileCompare.has(checkbox.dataset.compareDigest);
    const control = $("#cataractCompareOpen");
    if (control) {
      control.textContent = `查看比較（${window.FileCompare.count()} / 6）`;
      control.disabled = !window.FileCompare.count();
    }
  }

  function renderOrder(value, fallback) {
    const area = $("#cataractOrderContent"), row = value?.order || fallback;
    area.replaceChildren();
    if (!value) {area.append(el("p", "尚無已保存報告；正在按需查詢。", "small muted")); return;}
    area.append(el("p", [row.date, row.name, row.case_no].filter(Boolean).join(" · "), "small muted"));
    if (!config?.read_only) area.append(button("更新此報告", () => action(() => openOrder(row, true)), "quiet-button"));
    const open = button("查看比較", () => window.FileCompare.open(), "quiet-button");
    open.id = "cataractCompareOpen"; area.append(open);
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
    syncAttachments();
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
  }

  async function openOrder(row, force = false) {
    const member = currentCohort?.members.find(item => item.mrn === $("#analysisPatient").value);
    if (!member || member.account_id !== embeddedAccount) throw new Error("此病人未指定給目前登入帳號。");
    const request = {cohort_id: currentCohort.id, mrn: member.mrn,
      resource: "order_report", reference: row.id};
    const sequence = ++orderSequence, dialog = $("#cataractOrderDialog");
    $("#cataractOrderTitle").textContent = "醫囑報告 · " + row.name;
    $("#cataractOrderPatient").textContent = `${member.name || "姓名未提供"} · 病歷號 ${member.mrn}`;
    $("#cataractOrderContent").replaceChildren(el("p", "正在讀取已存報告…", "small muted"));
    if (!dialog.open) dialog.showModal();
    let stored;
    try {
      stored = await api("/api/reviews/history/read", request);
    } catch (error) {
      if (sequence === orderSequence && dialog.open)
        $("#cataractOrderContent").replaceChildren(el("p", error.message, "form-error"));
      throw error;
    }
    if (sequence !== orderSequence || !dialog.open) return;
    renderOrder(stored.data, row);
    if (stored.complete && !force || config?.read_only) return;
    const progress = el("div"); $("#cataractOrderContent").prepend(progress);
    JobProgress.pending(progress, "醫囑報告", "正在取得報告與附件…");
    try {
      const started = await api("/api/tasks/start", {kind: "history", ...request, force});
      for (;;) {
        if (sequence !== orderSequence || !dialog.open) return;
        const task = await api("/api/tasks/detail?id=" + encodeURIComponent(started.task_id));
        JobProgress.show(progress, task);
        if (!active.has(task.status)) {
          const updated = await api("/api/reviews/history/read", request);
          if (sequence !== orderSequence || !dialog.open) return;
          renderOrder(updated.data, row);
          if (task.status !== "completed") $("#cataractOrderContent").append(el("p", task.message || "報告部分未取得，可稍後再試。", "form-error"));
          await loadAnalysisResult();
          return;
        }
        await new Promise(resolve => setTimeout(resolve, 700));
      }
    } catch (error) {
      if (sequence === orderSequence && dialog.open) $("#cataractOrderContent").append(el("p", error.message, "form-error"));
    } finally {progress.remove();}
  }

  $("#cataractUpdate").addEventListener("click", () => action(() => start({refresh: true})));
  $("#cataractOrderDialog").addEventListener("close", () => {orderSequence++;});
  window.addEventListener("filecomparechange", syncAttachments);
  return {layout, refreshStatus, renderPatients, render, autoStart: () => start()};
})();
