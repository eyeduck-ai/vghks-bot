"use strict";

window.CataractBiometry = (() => {
  const active = new Set(["queued", "running"]);
  const jobLabels = {
    queued: "擷取排隊中", running: "擷取中", completed: "擷取完成", partial: "部分擷取完成",
    failed: "擷取失敗", cancelled: "擷取已停止", interrupted: "擷取已中斷"
  };
  const categories = [["biometry", "生物測量（Biometry）"], ["iol", "水晶體度數結果"], ["all", "全部"]];
  const assetSource = digest => window.LocalSession.resourceURL(scopedPath(`/api/analysis/asset?id=${encodeURIComponent(digest)}`));
  const unsaved = new Set();
  let nextWorkspace = 0;

  function applyReview(state, saved) {
    state.revision = saved.revision;
    state.confirmed = saved.confirmed;
    state.edits = new Map(saved.edits);
    state.storage = "saved";
    state.storageError = "";
  }

  async function flushAll() {
    while (unsaved.size) {
      const results = await Promise.all([...unsaved].map(entry => entry.flush()));
      if (!results.every(Boolean)) return false;
    }
    return true;
  }

  function create({account, cohortId, mrn, source, request, range = () => ({}), readonly = false,
    canStart = true, prepareDocument, textOnly = false, current = () => true,
    mountWorkspace = () => null, assetURL = assetSource}) {
    const id = `cataract-biometry-${++nextWorkspace}`;
    const scope = {cohort_id: cohortId, mrn, source};
    const selected = new Map(), pageCounts = new Map(), listeners = new Set();
    const localDocuments = new Map(), captures = new Map(), resultDrafts = new WeakMap();
    let documents = new Map(), rows = new Map(), allowAll = false, nextRow = 0;
    let job = null, error = "", starting = false, checking = false, statusKnown = false, timer = 0, revision = 0;
    let currentStatus, draft = new Map(), detachWorkspace = null, choosing = false;
    let reviewedResult = null, editor = null, shownDocument = 0;
    const attached = () => element.isConnected && current(account, cohortId, mrn);
    const keyOf = doc => [doc.id, source === "orders" ? doc.digest : ""].join(":");
    const reference = (doc, digest = doc.digest) => digest ? {id: doc.id, digest} : {id: doc.id};
    const send = values => request({...scope, ...values});
    const busy = () => starting || checking || active.has(job?.state);
    const cannotStart = () => !attached() || readonly || !canStart || busy();

    function captureState(doc) {
      if (!doc.capture_id || !doc.saved_review) return null;
      let capture = captures.get(doc.capture_id);
      if (!capture) {
        const state = {};
        applyReview(state, doc.saved_review);
        capture = {doc, state, pending: null, writing: null, conflict: null};
        capture.flush = () => flushCapture(capture);
        captures.set(doc.capture_id, capture);
      } else if (!capture.pending && doc.saved_review.revision > capture.state.revision) {
        applyReview(capture.state, doc.saved_review);
      }
      return capture;
    }

    function flushCapture(capture) {
      if (capture.writing) return capture.writing;
      if (capture.conflict) return Promise.resolve(false);
      if (!capture.pending) return Promise.resolve(true);
      const {doc, state} = capture;
      const store = async () => {
        state.storage = "saving";
        state.storageError = "";
        while (capture.pending) {
          const review = capture.pending;
          try {
            const response = await send({action: "save_review", documents: [reference(doc)],
              capture_id: doc.capture_id, revision: state.revision, review});
            if (response.state === "changed") {
              state.storage = "changed";
              capture.pending = null;
              unsaved.delete(capture);
              return true;
            }
            if (response.state === "conflict") {
              state.storage = "conflict";
              capture.conflict = response.saved_review;
              return false;
            }
            if (response.state !== "saved" || !response.saved_review) throw new Error("未收到儲存確認。");
            state.revision = response.saved_review.revision;
            if (capture.pending === review) capture.pending = null;
          } catch (failure) {
            state.storage = "error";
            state.storageError = failure.message || "無法儲存核對資料。";
            return false;
          }
        }
        state.storage = "saved";
        unsaved.delete(capture);
        return true;
      };
      capture.writing = store().finally(() => {capture.writing = null;});
      return capture.writing;
    }

    function reviewOptions(result) {
      let fields = resultDrafts.get(result);
      if (!fields) {
        fields = result === job?.result ? draft : new Map();
        result.documents.forEach((doc, index) => {
          const capture = captureState(doc);
          if (capture) fields.set(index, capture.state);
        });
        resultDrafts.set(result, fields);
      }
      const captureAt = index => captures.get(result.documents[index]?.capture_id);
      return {draft: fields, readonly,
        onDraftChange(index, state) {
          const capture = captureAt(index);
          if (!capture || readonly || state.storage === "changed") return Promise.resolve(!capture);
          capture.pending = {confirmed: state.confirmed, edits: [...state.edits].map(([key, value]) => [key, {...value}])};
          unsaved.add(capture);
          return capture.flush();
        },
        retryDraft(index) {
          const capture = captureAt(index);
          return capture ? capture.flush() : Promise.resolve(false);
        },
        useSavedDraft(index) {
          const capture = captureAt(index);
          if (!capture?.conflict) return false;
          applyReview(capture.state, capture.conflict);
          capture.pending = capture.conflict = null;
          unsaved.delete(capture);
          return true;
        }};
    }

    const element = el("div", undefined, "cataract-biometry");
    const launcher = button("擷取眼科測量", open, "cataract-biometry-launcher quiet-button");
    const summary = el("span", undefined, "small muted");
    const workspace = el("section", undefined, "cataract-biometry-workspace");
    workspace.id = id;
    launcher.setAttribute("aria-controls", id);
    launcher.setAttribute("aria-expanded", "false");
    const heading = el("h2", source === "orders" ? "從醫囑 PDF 擷取眼科測量" : "從掃描病歷 PDF 擷取眼科測量");
    heading.id = `${id}-title`;
    heading.tabIndex = -1;
    workspace.setAttribute("aria-labelledby", heading.id);
    const top = el("div", undefined, "cataract-biometry-heading");
    const back = button("返回上一頁", () => { dismiss(); launcher.focus({preventScroll: true}); }, "quiet-button");
    const revise = button("重新選擇 PDF", () => { choosing = true; renderStatus(); mode.focus(); }, "quiet-button");
    const showResult = button("查看核對結果", () => { choosing = false; renderStatus(); heading.focus(); }, "quiet-button");
    const topActions = el("div", undefined, "cataract-biometry-actions");
    topActions.append(revise, showResult);
    top.append(back, heading, topActions);

    const mode = el("select");
    for (const [value, label] of [["selected", "勾選 PDF／頁面"], ["all", "全部 PDF"]]) {
      const option = el("option", label);
      option.value = value;
      mode.append(option);
    }
    mode.addEventListener("change", renderSelection);
    const modeLabel = el("label", "擷取範圍");
    modeLabel.append(mode);
    const category = el("select");
    for (const [value, label] of categories) {
      const option = el("option", label);
      option.value = value;
      category.append(option);
    }
    category.value = "all";
    const categoryLabel = el("label", "擷取內容");
    categoryLabel.append(category);
    const catalogTools = el("div", undefined, "cataract-biometry-catalog-tools");
    const search = el("input");
    search.type = "search";
    search.placeholder = "搜尋 PDF 名稱或日期";
    search.setAttribute("aria-label", search.placeholder);
    search.addEventListener("input", renderSelection);
    catalogTools.append(modeLabel, categoryLabel, search);
    const catalog = el("div", undefined, "cataract-biometry-catalog");
    const empty = el("p", undefined, "small muted");
    const selectionStatus = el("span", undefined, "small muted");
    const submit = button("開始背景擷取", () => void startSelection(), "primary");
    const actions = el("div", undefined, "cataract-biometry-actions");
    actions.append(submit, selectionStatus);
    const progress = el("progress");
    progress.setAttribute("aria-label", "PDF 擷取進度");
    const status = el("p", undefined, "small muted");
    status.setAttribute("role", "status");
    const retry = button("重讀擷取進度", () => void readStatus({latest: true}), "quiet-button");
    const selection = el("section", undefined, "cataract-biometry-selection");
    const selectionTitle = el("h4", "PDF 清單");
    selectionTitle.id = `${id}-selection`;
    selection.setAttribute("aria-labelledby", selectionTitle.id);
    selection.append(selectionTitle, catalogTools, empty, catalog, actions);
    const jobStatus = el("div", undefined, "cataract-biometry-job-status");
    jobStatus.append(progress, status, retry);

    const review = el("div", undefined, "cataract-biometry-workspace-review");
    const sourcePanel = el("section", undefined, "cataract-biometry-source");
    const sourceHeading = el("h3", "原始 PDF");
    sourceHeading.id = `${id}-source`;
    sourcePanel.setAttribute("aria-labelledby", sourceHeading.id);
    const sourceTools = el("div", undefined, "cataract-biometry-source-tools");
    const documentSelect = el("select");
    documentSelect.setAttribute("aria-label", "核對檔案");
    documentSelect.addEventListener("change", () => selectDocument(Number(documentSelect.value)));
    const previous = button("上一份", () => selectDocument(shownDocument - 1));
    const next = button("下一份", () => selectDocument(shownDocument + 1));
    sourceTools.append(documentSelect, previous, next);
    const sourcePreview = el("div", undefined, "cataract-biometry-source-preview");
    sourcePanel.append(sourceHeading, sourceTools, sourcePreview);
    const reviewFields = el("div", undefined, "cataract-biometry-workspace-fields");
    review.append(sourcePanel, reviewFields);
    workspace.append(top);
    if (textOnly) {
      const description = el("p", "此版本從 PDF 文字層擷取資料。純影像掃描請使用離線 OCR 版。", "small muted");
      description.id = `${id}-description`;
      workspace.setAttribute("aria-describedby", description.id);
      workspace.append(description);
    }
    workspace.append(jobStatus, selection, review);
    element.append(launcher, summary);
    renderSelection();
    renderStatus();

    function updateSubmit() {
      const all = mode.value === "all";
      submit.disabled = cannotStart() ||
        (all ? !allowAll || !documents.size : !selected.size);
    }

    function renderSelection() {
      const all = mode.value === "all", query = search.value.trim().toLocaleLowerCase();
      let shown = 0;
      search.hidden = all || documents.size < 12;
      catalog.hidden = all;
      mode.options[1].disabled = !allowAll;
      for (const [key, row] of rows) {
        const visible = search.hidden || row.text.includes(query);
        row.element.hidden = !visible;
        if (visible) shown++;
        row.check.checked = selected.has(key);
        const pages = selected.get(key);
        row.caption.textContent = pages ? `已選 ${pages.size} 頁` : selected.has(key) ? "整份 PDF" : "";
        for (const [page, input] of row.inputs) input.checked = !!pages?.has(page);
      }
      empty.hidden = all || shown > 0;
      empty.textContent = documents.size ? "沒有符合搜尋條件的 PDF。" : "目前沒有可擷取的 PDF。";
      selectionStatus.textContent = readonly || !canStart ? "目前可查看擷取結果。" : all
        ? source === "orders" ? `擷取此日期範圍內的 ${documents.size} 份已保存 PDF` : `擷取索引中的 ${documents.size} 份 PDF`
        : `已選 ${selected.size} 份 PDF`;
      updateSubmit();
    }

    function renderStatus() {
      const running = active.has(job?.state), position = job?.total > 0 ? ` · ${job.processed || 0} / ${job.total} 份 PDF` : "";
      const label = jobLabels[job?.state] || "";
      summary.textContent = starting ? "正在送出…" : label;
      const message = error || (starting ? "正在送出擷取工作…" :
        [label + position, job?.message].filter(Boolean).join("。"));
      if (status.textContent !== message) status.textContent = message;
      status.className = error ? "small form-error" : "small muted";
      status.hidden = !status.textContent;
      retry.hidden = !error || starting || checking;
      progress.hidden = !running;
      if (job?.total > 0) {
        progress.max = job.total;
        progress.value = job.processed || 0;
      } else progress.removeAttribute("value");
      const resultData = starting ? undefined : job?.result;
      jobStatus.hidden = status.hidden && progress.hidden && retry.hidden;
      renderWorkspace(resultData);
      updateSubmit();
      currentStatus = {busy: busy(), error, state: job?.state || "idle", message, hasResult: !!resultData,
        origin: job?.view || "", jobId: job?.job_id || "", result: resultData};
      for (const listener of listeners) listener(currentStatus);
    }

    function renderWorkspace(result) {
      const available = !!result?.documents?.length && !!window.CataractBiometryReview;
      const reviewing = available && !choosing;
      const moveFocus = reviewing && !selection.hidden && selection.contains(document.activeElement);
      selection.hidden = reviewing;
      review.hidden = !reviewing;
      revise.hidden = !reviewing;
      showResult.hidden = reviewing || !available;
      if (moveFocus) heading.focus({preventScroll: true});
      if (!detachWorkspace || !reviewing || reviewedResult === result) return;
      reviewedResult = result;
      documentSelect.replaceChildren(...result.documents.map((doc, index) => {
        const label = [doc.date, doc.label || `PDF ${index + 1}`].filter(Boolean).join(" · ");
        const option = el("option", label);
        option.value = String(index);
        return option;
      }));
      sourceTools.hidden = result.documents.length === 1;
      editor = window.CataractBiometryReview.create({result, ...reviewOptions(result), documentIndex: shownDocument,
        onPageChange: showPage, current: () => !!detachWorkspace && attached() && !review.hidden && reviewedResult === result});
      reviewFields.replaceChildren(editor.element);
      selectDocument(shownDocument);
    }

    function selectDocument(index) {
      const doc = reviewedResult?.documents[index];
      if (!doc) return;
      shownDocument = index;
      documentSelect.value = String(index);
      sourceHeading.textContent = ["原始 PDF", doc.label].filter(Boolean).join(" · ");
      previous.disabled = index === 0;
      next.disabled = index === reviewedResult.documents.length - 1;
      editor?.setDocument(index);
      showPage(doc.pages?.[0] || doc.data?.metadata?.selected_pages?.[0] || 1);
    }

    function showPage(page) {
      const doc = reviewedResult?.documents[shownDocument];
      if (!doc || review.hidden || !attached()) return;
      if (!/^[a-f0-9]{64}$/.test(doc.digest || "")) {
        sourcePreview.replaceChildren(el("p", "找不到可預覽的原始 PDF。", "small muted"));
        return;
      }
      const number = Number.isSafeInteger(page) && page > 0 ? page : 1;
      let viewer = sourcePreview.querySelector("iframe");
      if (!viewer || viewer.dataset.digest !== doc.digest) {
        viewer = el("iframe");
        viewer.title = sourceHeading.textContent;
        viewer.dataset.digest = doc.digest;
        sourcePreview.replaceChildren(viewer);
      }
      const path = window.ClinicalUI.pdfURL(assetURL(doc.digest), number);
      if (viewer.src !== path) viewer.src = path;
    }

    function acceptJob(nextJob) {
      if (nextJob?.job_id !== job?.job_id) {
        draft = new Map();
        choosing = false;
        reviewedResult = editor = null;
        shownDocument = 0;
        sourcePreview.replaceChildren();
        reviewFields.replaceChildren();
      } else if (job?.result && nextJob?.result) {
        // Completed results are immutable for a job; keep the editor and its focus.
        nextJob = {...nextJob, result: job.result};
      }
      job = nextJob;
      statusKnown = true;
      for (const doc of job?.result?.documents || []) {
        if (!doc.id || !doc.digest) continue;
        const entry = localEntry(doc);
        if (doc.capture_id || !entry.result) {
          setLocalResult(entry, doc, job.result.category, false, job.job_id);
          if (doc.capture_id) entry.checked = true;
        }
      }
    }

    function schedule() {
      if (attached() && active.has(job?.state) && !error) {
        if (!timer) timer = setTimeout(() => { timer = 0; return readStatus(); }, 1200);
      } else if (timer) {
        clearTimeout(timer);
        timer = 0;
      }
    }

    async function readStatus({latest = false} = {}) {
      if (!attached() || starting || checking) return;
      const requestRevision = revision;
      checking = true;
      error = "";
      renderStatus();
      try {
        const next = await send({action: "status", ...(!latest && job?.job_id ? {job_id: job.job_id} : {})});
        if (revision === requestRevision && attached()) acceptJob(next);
      } catch (failure) {
        if (revision === requestRevision && attached()) error = failure.message || "無法讀取擷取進度。";
      } finally {
        if (revision === requestRevision) {
          checking = false;
          renderStatus();
          schedule();
        }
      }
    }

    function open() {
      if (!attached()) return;
      if (!detachWorkspace) detachWorkspace = mountWorkspace(workspace);
      if (!detachWorkspace) return;
      launcher.setAttribute("aria-expanded", "true");
      renderStatus();
      editor?.refresh?.();
      heading.focus();
      void readStatus({latest: !active.has(job?.state)});
    }

    function dismiss() {
      const detach = detachWorkspace;
      detachWorkspace = null;
      launcher.setAttribute("aria-expanded", "false");
      detach?.();
    }

    function startSelection() {
      if (submit.disabled) return;
      const values = {mode: mode.value, category: category.value, view: "batch"};
      if (mode.value === "selected") values.documents = [...selected].map(([key, pages]) => ({
        ...reference(documents.get(key), pages ? pageCounts.get(key)?.digest : undefined),
        ...(pages ? {pages: [...pages].sort((a, b) => a - b)} : {})}));
      return startJob(values);
    }

    function startDocument(doc, pages, category = "all") {
      if (!doc?.id || pages && !pages.length) return Promise.resolve(false);
      const file = reference(doc);
      if (pages) file.pages = [...new Set(pages)].sort((a, b) => a - b);
      return startJob({mode: "selected", category, view: "inline", documents: [file]});
    }

    async function startJob(values) {
      if (cannotStart()) return false;
      const requestRevision = revision;
      starting = true;
      statusKnown = false;
      error = "";
      renderStatus();
      try {
        const next = await send({action: "start", ...range(), ...values});
        if (revision === requestRevision && attached()) {
          acceptJob(next);
          return true;
        }
      } catch (failure) {
        if (revision === requestRevision && attached()) error = failure.message || "無法安排背景擷取。";
      } finally {
        if (revision === requestRevision) {
          starting = false;
          renderStatus();
          schedule();
        }
      }
      return false;
    }

    function subscribe(listener) {
      listeners.add(listener);
      listener(currentStatus);
      if (!statusKnown) void readStatus({latest: true});
      return () => listeners.delete(listener);
    }

    function localEntry(doc) {
      const key = [doc.id, doc.digest].join(":");
      if (!localDocuments.has(key)) localDocuments.set(key, {doc: reference(doc), subscribers: new Set()});
      return localDocuments.get(key);
    }

    function setLocalResult(entry, doc, category, saved, jobId) {
      const id = doc.capture_id || jobId;
      if (entry.id !== id || !entry.result) {
        entry.id = id;
        entry.result = {schema_version: 1, source, category, documents: [doc]};
      }
      captureState(doc);
      entry.saved = saved;
      entry.changed = false;
      entry.error = "";
    }

    async function restoreDocument(entry, retry = false) {
      if ((!retry && entry.checked) || entry.restoring || !attached()) return;
      const requestRevision = revision, before = entry.result;
      const publish = () => {for (const listener of entry.subscribers) listener(currentStatus);};
      entry.restoring = true;
      entry.error = "";
      publish();
      try {
        const saved = await send({...range(), action: "load", documents: [entry.doc]});
        if (requestRevision !== revision || !attached() || entry.result !== before) return;
        if (saved.state === "saved" && saved.document?.id === entry.doc.id && saved.document.digest === entry.doc.digest) {
          setLocalResult(entry, saved.document, saved.category, true);
        } else {
          entry.changed = saved.state === "changed";
          if (entry.changed || saved.state === "missing") entry.result = undefined;
        }
        entry.checked = true;
      } catch (failure) {
        if (requestRevision === revision && attached() && entry.result === before)
          entry.error = failure.message || "無法讀取本機核對資料。";
      } finally {
        entry.restoring = false;
        if (requestRevision === revision) publish();
      }
    }

    function subscribeDocument(doc, listener) {
      const entry = localEntry(doc);
      const publish = status => listener({...status, result: entry.result, jobId: entry.id || "",
        hasResult: !!entry.result, changed: !!entry.changed, saved: !!entry.saved,
        restoring: !!entry.restoring, restoreError: entry.error || ""});
      entry.subscribers.add(publish);
      const detach = subscribe(publish);
      void restoreDocument(entry);
      return () => {entry.subscribers.delete(publish); detach();};
    }

    function reloadDocument(doc) {
      return restoreDocument(localEntry(doc), true);
    }

    async function readPages(doc, currentRequest = attached) {
      if (!doc?.id || !attached()) return null;
      const requestRevision = revision;
      const valid = () => revision === requestRevision && attached() && currentRequest();
      const key = keyOf(doc);
      const prepared = !doc.digest && prepareDocument ? await prepareDocument(doc, valid) : null;
      if (!valid()) return null;
      const file = reference(doc, prepared?.digest);
      const cached = pageCounts.get(key);
      if (cached && cached.digest === file.digest) return cached;
      const pages = await send({action: "pages", ...range(), documents: [file]});
      if (!valid()) return null;
      if (!Number.isSafeInteger(pages.page_count) || pages.page_count < 1) throw new Error("無法讀取此 PDF 的頁數。");
      const result = {count: pages.page_count, digest: pages.digest || file.digest};
      pageCounts.set(key, result);
      return result;
    }

    function renderPages(row, key, count) {
      const group = el("fieldset", undefined, "cataract-biometry-page-group");
      const numbers = el("div", undefined, "cataract-biometry-page-numbers");
      group.append(el("legend", `勾選要擷取的頁碼（共 ${count} 頁）`), numbers);
      for (let page = 1; page <= count; page++) {
        const input = el("input");
        input.type = "checkbox";
        input.addEventListener("change", () => {
          const pages = selected.get(key) || new Set();
          if (input.checked) pages.add(page);
          else pages.delete(page);
          if (pages.size) selected.set(key, pages);
          else selected.delete(key);
          renderSelection();
        });
        const label = el("label", undefined, "check-label");
        label.append(input, el("span", String(page)));
        numbers.append(label);
        row.inputs.set(page, input);
      }
      const whole = button("選取整份 PDF", () => {
        selected.set(key, null);
        renderSelection();
      }, "quiet-button");
      row.pages.replaceChildren(group, whole);
      renderSelection();
    }

    async function togglePages(doc, row, key) {
      if (rows.get(key) !== row || !attached()) return;
      const opening = row.pages.hidden;
      row.pages.hidden = !opening;
      row.toggle.setAttribute("aria-expanded", String(opening));
      if (!opening || row.inputs.size || row.loading) return;
      const requestRevision = revision;
      const stillCurrent = () => revision === requestRevision && attached() && rows.get(key) === row;
      row.loading = true;
      const waiting = el("p", "正在讀取 PDF 頁數…", "small muted");
      waiting.setAttribute("role", "status");
      row.pages.replaceChildren(waiting);
      try {
        const pages = await readPages(doc, stillCurrent);
        if (pages && stillCurrent()) renderPages(row, key, pages.count);
      } catch (failure) {
        if (stillCurrent()) {
          waiting.textContent = failure.message || "無法讀取此 PDF 的頁數。";
          waiting.className = "small form-error";
        }
      } finally {
        row.loading = false;
      }
    }

    function documentRow(doc, key) {
      const row = {inputs: new Map(), loading: false};
      row.element = el("div", undefined, "cataract-biometry-document");
      row.check = el("input");
      row.check.type = "checkbox";
      row.check.addEventListener("change", () => {
        if (rows.get(key) !== row || !attached()) return;
        if (row.check.checked) selected.set(key, null);
        else selected.delete(key);
        renderSelection();
      });
      const title = [doc.date, doc.label || doc.name || "PDF"].filter(Boolean).join(" · ");
      row.text = title.toLocaleLowerCase();
      const label = el("label", undefined, "check-label");
      label.append(row.check, el("span", title));
      row.caption = el("span", undefined, "small muted");
      row.toggle = button("指定頁面", () => void togglePages(documents.get(key), row, key), "quiet-button");
      row.pages = el("div", undefined, "cataract-biometry-pages");
      row.pages.id = `${id}-pages-${++nextRow}`;
      row.pages.hidden = true;
      row.toggle.setAttribute("aria-controls", row.pages.id);
      row.toggle.setAttribute("aria-expanded", "false");
      row.toggle.setAttribute("aria-label", `${title}，指定頁面`);
      const line = el("div", undefined, "cataract-biometry-document-line");
      const info = el("div", undefined, "cataract-biometry-document-info");
      info.append(label, row.caption);
      line.append(info, row.toggle);
      row.element.append(line, row.pages);
      return row;
    }

    function setDocuments(items, {allowAll: all = true} = {}) {
      allowAll = all;
      if (!allowAll && mode.value === "all") mode.value = "selected";
      const nextDocuments = new Map(items.map(doc => [keyOf(doc), doc]));
      for (const key of rows.keys()) {
        if (!nextDocuments.has(key)) {
          selected.delete(key);
          pageCounts.delete(key);
        }
      }
      const nextRows = new Map();
      for (const [key, doc] of nextDocuments) {
        const prior = documents.get(key);
        const previousDigest = prior?.digest || pageCounts.get(key)?.digest;
        const sameFile = prior && (prior.digest === doc.digest || previousDigest === doc.digest);
        if (prior && !sameFile) {
          pageCounts.delete(key);
          if (selected.get(key)) selected.delete(key);
        }
        const unchanged = sameFile && prior.label === doc.label &&
          prior.name === doc.name && prior.date === doc.date;
        nextRows.set(key, unchanged ? rows.get(key) : documentRow(doc, key));
      }
      for (const [key, row] of rows) if (nextRows.get(key) !== row) row.element.remove();
      documents = nextDocuments;
      rows = nextRows;
      let index = 0;
      for (const {element} of rows.values()) {
        const before = catalog.children[index++] || null;
        if (before !== element) catalog.insertBefore(element, before);
      }
      renderSelection();
      schedule();
    }

    function clear() {
      revision++;
      clearTimeout(timer);
      timer = 0;
      dismiss();
      acceptJob(null);
      error = "";
      checking = starting = statusKnown = false;
      search.value = "";
      mode.value = "selected";
      category.value = "all";
      selected.clear();
      pageCounts.clear();
      localDocuments.clear();
      captures.clear();
      setDocuments([], {allowAll: false});
      renderStatus();
    }

    const reviewDraft = jobId => jobId && jobId === job?.job_id ? draft : null;
    return {element, setDocuments, readPages, startDocument, subscribe, subscribeDocument, reloadDocument,
      reviewOptions, reviewDraft, dismiss, clear};
  }

  return {create, categories, flushAll, hasUnsaved: () => unsaved.size > 0};
})();
