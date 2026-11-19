"use strict";

window.CataractBiometryReview = (() => {
  const labels = {biometry: "眼科生物測量", iol: "水晶體度數結果", all: "全部"};
  const eyes = ["OD", "OS"];
  const measurements = [
    ["AL 眼軸長", "al"], ["ACD 前房深度", "acd"], ["LT 水晶體厚度", "lt"],
    ["CCT 中央角膜厚度", "cct"], ["WTW 角膜水平直徑", "wtw"], ["瞳孔直徑", "pupil"],
    ["平均角膜屈光度", "mean_k"], ["角膜散光", "corneal_astigmatism"], ["散光軸度", "corneal_astigmatism_axis"],
    ...["k1", "k2", "tk1", "tk2"].flatMap(key => [
      [`${key.toUpperCase()} 屈光度`, key, "power"], [`${key.toUpperCase()} 軸度`, key, "axis"]])];
  const measurementKeys = new Set(measurements.flatMap(([, ...path]) => [path[0], path.join("_")]));
  const rowFields = [["水晶體度數", "iol_power", "D"], ["預測屈光度", "predicted_refraction", "D"],
    ["散光水晶體度數", "toric_power", "D"], ["軸度", "axis", "°"]];
  const units = {um: "µm", degree: "°"};
  const has = (category, kind) => category === kind || category === "all";
  let nextId = 0;

  function finiteValue(text) {
    const normalized = text.normalize("NFKC").trim().replaceAll("−", "-");
    if (!normalized) return null;
    return /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(normalized) && Number.isFinite(Number(normalized))
      ? Number(normalized) : undefined;
  }

  function field(path, label, value, options = {}) {
    return {path, key: path.join("."), label, value: value ?? null, kind: path[0] === "eyes" ? "biometry" : "iol",
      type: "number", unit: "", warning: "", page: null, ...options};
  }

  function measurementWarning(result, evidence) {
    const status = result.status || result.data_status;
    if (status === "conflict" || result.data_status === "conflict") return "辨識結果不一致";
    if (["error", "unreadable"].includes(status) || result.data_status === "unreadable") return "來源數值無法辨識";
    const low = typeof evidence?.confidence === "number" && evidence.confidence >= 0 && evidence.confidence < 0.8 &&
      /^0\.\.1(?:\s|$)/.test(evidence.confidence_scale || "");
    if (low) return "辨識信心偏低";
    if (result.candidate_value != null && result.verification_status !== "cross_checked" && result.status !== "verified")
      return "辨識值待核對";
    return "";
  }

  function model(doc, capture) {
    const data = doc.data || {};
    const bio = !data.eyes || !has(capture, "biometry") ? [] : measurements.map(([label, ...path]) => ({label,
      items: eyes.map(eye => {
        const result = path.reduce((parent, key) => parent?.[key], data.eyes[eye]) || {};
        const evidence = doc.review?.eyes?.[eye]?.[path.join("_")];
        return field(["eyes", eye, ...path], `${eye} ${label}`, result.value ?? result.candidate_value,
          {unit: units[result.unit] || result.unit || "", page: evidence?.pages?.[0], warning: measurementWarning(result, evidence)});
      })}));
    const tables = !has(capture, "iol") ? [] : (data.iol_calculations || []).map((table, index) => {
      const path = ["iol_calculations", index];
      const flags = new Set(table.flags || []);
      const eyeChoices = [["", "未辨識"], ["OD", "OD"], ["OS", "OS"]];
      const lensChoices = [["unknown", "未辨識"], ["non_toric", "非散光"], ["toric", "散光"]];
      const keratometryChoices = [["unknown", "未辨識"], ["K", "K"], ["TK", "TK"]];
      const general = [
        field([...path, "eye"], "眼別", table.eye, {choices: eyeChoices, type: "text", warning: flags.has("eye_unresolved") ? "眼別無法辨識" : ""}),
        field([...path, "lens_model"], "水晶體型號", table.lens_model, {type: "text"}),
        field([...path, "formula"], "公式", table.formula, {type: "text"}),
        field([...path, "lens_type"], "水晶體類型", table.lens_type, {choices: lensChoices, type: "text"}),
        field([...path, "keratometry_source"], "角膜屈光度來源", table.keratometry_source, {choices: keratometryChoices, type: "text"}),
        field([...path, "target_refraction"], "目標屈光度", table.target_refraction,
          {unit: "D", warning: flags.has("target_refraction_unreadable") ? "來源數值無法辨識" : ""})];
      const constants = (table.constants || []).map((constant, number) =>
        field([...path, "constants", number, "value"], constant.name, constant.value, {type: "text"}));
      const rows = (table.rows || []).map((row, number) => ({row, items: rowFields.map(([label, key, unit]) =>
        field([...path, "rows", number, key], `第 ${number + 1} 列 ${label}`, row[key], {
          unit, warning: (row.flags || []).includes(`${key}_unreadable`) ? "來源數值無法辨識" :
            key === "axis" && (row.flags || []).includes("axis_out_of_range") ? "來源軸度超出解析範圍" : ""
        }))}));
      return {table, general, constants, rows};
    });
    return {doc, bio, tables, items: [...bio.flatMap(row => row.items), ...tables.flatMap(table =>
      [...table.general, ...table.constants, ...table.rows.flatMap(row => row.items)])]};
  }

  function setValue(record, path, value) {
    const parent = path.slice(0, -1).reduce((current, key, index) =>
      current[key] ??= typeof path[index + 1] === "number" ? [] : {}, record);
    parent[path.at(-1)] = value;
  }

  const iolIssue = issue => issue.code?.startsWith("iol_") || issue.code === "invalid_iol_cell";
  const biometryIssue = issue => !iolIssue(issue) && (measurementKeys.has(issue.field) ||
    ["no_biometry", "unpaired_keratometry"].includes(issue.code));

  function filteredData(source, category) {
    const data = {...source};
    if (!has(category, "iol")) {
      delete data.iol_calculations;
      if (data.issues) data.issues = data.issues.filter(issue => !iolIssue(issue));
    }
    if (!has(category, "biometry")) {
      for (const key of ["eyes", "completeness", "verification_state"]) delete data[key];
      if (data.issues) data.issues = data.issues.filter(issue => !biometryIssue(issue));
      if (data.metadata) {
        data.metadata = {...data.metadata};
        delete data.metadata.merge_policy_version;
        delete data.metadata.source_state_counts;
      }
    }
    // Older stored results may include provider-level observations for both categories.
    if (category !== "all" && data.runs) data.runs = data.runs.map(sourceRun => {
      const run = {...sourceRun};
      if (category === "biometry") delete run.iol_calculations;
      else delete run.observations;
      if (run.issues) run.issues = run.issues.filter(category === "biometry" ? issue => !iolIssue(issue) : issue => !biometryIssue(issue));
      return run;
    });
    return data;
  }

  function create({result, draft = new Map(), documentIndex = 0, onPageChange = () => {}, current = () => true,
    readonly = false, onDraftChange, retryDraft, useSavedDraft}) {
    const capture = ["biometry", "iol"].includes(result.category) ? result.category : "all";
    const kinds = ["biometry", "iol"].filter(kind => has(capture, kind));
    const records = (result.documents || []).map(doc => model(doc, capture));
    const states = records.map((_, index) => {
      if (!draft.has(index)) draft.set(index, {edits: new Map(), confirmed: false});
      return draft.get(index);
    });
    const baseId = `biometry-review-${++nextId}`;
    let index = -1, view = kinds[0], pageSelect, recoveryIndex = -1;
    const element = el("section", undefined, "cataract-biometry-review");
    element.setAttribute("aria-label", "核對擷取結果");
    const head = el("div", undefined, "cataract-biometry-review-head");
    const header = el("div", undefined, "cataract-biometry-review-title");
    const heading = el("h3", "核對擷取結果");
    const source = el("div", undefined, "cataract-biometry-review-source");
    const tabs = el("div", undefined, "cataract-biometry-review-tabs");
    tabs.setAttribute("role", "group");
    tabs.setAttribute("aria-label", "核對資料類型");
    const body = el("div", undefined, "cataract-biometry-review-fields");
    body.tabIndex = 0;
    body.setAttribute("role", "region");
    body.setAttribute("aria-label", "擷取資料欄位");
    const footer = el("footer", undefined, "cataract-biometry-review-footer");
    const confirm = el("input");
    confirm.type = "checkbox";
    const confirmLabel = el("label", undefined, "check-label");
    confirmLabel.append(confirm, el("span", "已核對這份文件的擷取資料"));
    const options = el("select");
    options.setAttribute("aria-label", "JSON 輸出範圍");
    for (const kind of capture === "all" ? ["all", ...kinds] : kinds) {
      const option = el("option", labels[kind]);
      option.value = kind;
      options.append(option);
    }
    options.value = capture;
    const copy = button("複製 JSON", () => void exportResult(true), "quiet-button");
    const save = button("下載 JSON", () => void exportResult(false), "quiet-button");
    const controls = el("div", undefined, "cataract-biometry-review-actions");
    controls.append(options, copy, save);
    const status = el("p", undefined, "small muted");
    status.setAttribute("role", "status");
    const storage = el("p", undefined, "small muted");
    storage.setAttribute("role", "status");
    const recovery = button("重試儲存", recoverDraft, "quiet-button");
    recovery.hidden = storage.hidden = true;
    footer.append(confirmLabel, controls, status, storage, recovery);
    header.append(heading);
    head.append(header, source);
    element.append(head, tabs, body, footer);

    const available = record => !!record?.items.some(item => item.value != null || item.warning);
    const reviewable = records.reduce((count, record) => count + Number(available(record)), 0);
    const unavailable = records.length - reviewable;
    const valueOf = (state, item) => state.edits.has(item.key) ? state.edits.get(item.key).value : item.value;
    const labelText = item => [item.label, item.unit && `(${item.unit})`].filter(Boolean).join(" ");
    const pageLabel = page => `第 ${page} 頁`;
    const blocked = state => ["changed", "conflict"].includes(state?.storage);

    function updateFooter() {
      const state = states[index];
      const invalid = state && [...state.edits.values()].some(edit => edit.value === undefined);
      const checked = states.filter((state, position) => available(records[position]) && state.confirmed).length;
      confirm.disabled = readonly || !available(records[index]) || invalid || blocked(state);
      confirm.checked = Boolean(state?.confirmed);
      options.disabled = !reviewable || options.options.length <= 1;
      const unsaved = states.findIndex(state => state.storage && state.storage !== "saved");
      copy.disabled = save.disabled = !reviewable || checked !== reviewable || unsaved >= 0;
      const message = invalid ? "修正格式有誤的數值後即可確認。" : reviewable
        ? `已核對 ${checked} / ${reviewable} 份文件${unavailable ? ` · ${unavailable} 份無擷取資料` : ""}` : "沒有可核對的擷取資料。";
      if (status.textContent !== message) status.textContent = message;
      recoveryIndex = unsaved >= 0 ? unsaved : index;
      const pending = states[recoveryIndex];
      const label = pending?.storage === "saving" ? "正在儲存核對資料…" : pending?.storage === "error"
        ? `未能儲存核對資料。${pending.storageError}` : pending?.storage === "changed"
          ? "原始 PDF 或擷取結果已變更，請重新開啟 PDF 核對。" : pending?.storage === "conflict"
            ? "此核對資料已由另一個畫面更新。可採用本機已儲存的版本。" : pending?.storage === "saved"
              ? "已儲存於本機。" : "";
      const storageMessage = label && recoveryIndex !== index ? `第 ${recoveryIndex + 1} 份：${label}` : label;
      if (storage.textContent !== storageMessage) storage.textContent = storageMessage;
      storage.hidden = !label;
      storage.classList.toggle("form-error", ["changed", "conflict", "error"].includes(pending?.storage));
      recovery.hidden = !["conflict", "error"].includes(pending?.storage);
      recovery.textContent = pending?.storage === "conflict" ? "採用本機版本" : "重試儲存";
    }

    function draftChanged(position) {
      const state = states[position];
      const promise = onDraftChange?.(position, state);
      updateFooter();
      if (promise) void promise.then(() => {
        if (!current()) return;
        if (position === index && blocked(state)) refresh();
        else updateFooter();
      });
    }

    async function recoverDraft() {
      if (!current() || recovery.hidden) return;
      const target = recoveryIndex;
      if (states[target]?.storage === "conflict") useSavedDraft?.(target);
      else {
        const saving = retryDraft?.(target);
        updateFooter();
        await saving;
      }
      if (current()) refresh();
    }

    function goToPage(page) {
      if (!current() || !Number.isSafeInteger(page) || page < 1) return;
      if (pageSelect && [...pageSelect.options].some(item => item.value === String(page))) pageSelect.value = String(page);
      onPageChange(page);
    }

    function pageButton(page) {
      return button(pageLabel(page), () => goToPage(page), "cataract-biometry-review-page quiet-button");
    }

    function inputFor(item, state, {compact = false, label = labelText(item)} = {}) {
      const box = el("div", undefined, "cataract-biometry-review-field");
      const input = el(item.choices ? "select" : "input");
      input.disabled = readonly || blocked(state);
      const raw = state.edits.get(item.key)?.raw ?? String(item.value ?? "");
      const warning = el("span", undefined, "cataract-biometry-review-warning");
      warning.id = `${baseId}-${item.key.replaceAll(".", "-")}-warning`;
      input.setAttribute("aria-label", labelText(item));
      if (item.choices) {
        for (const [value, label] of item.choices) {
          const option = el("option", label);
          option.value = value;
          input.append(option);
        }
      } else {
        input.type = "text";
        input.inputMode = item.type === "number" ? "decimal" : "text";
        input.autocomplete = "off";
      }
      input.value = raw;
      if (!compact) box.append(el("span", label, "cataract-biometry-review-label"));
      box.append(input, warning);
      if (item.page) box.append(pageButton(item.page));
      function renderWarning() {
        const edit = state.edits.get(item.key);
        const invalid = edit && edit.value === undefined;
        const text = invalid ? "請輸入數值" : edit ? "已手動修正" : item.warning;
        warning.textContent = text || "";
        warning.hidden = !text;
        input.classList.toggle("review-attention", Boolean(invalid || !edit && item.warning));
        warning.classList.toggle("is-attention", Boolean(invalid || !edit && item.warning));
        if (text) input.setAttribute("aria-describedby", warning.id); else input.removeAttribute("aria-describedby");
        if (invalid) input.setAttribute("aria-invalid", "true"); else input.removeAttribute("aria-invalid");
      }
      function edit() {
        if (!current() || input.disabled || blocked(state)) return;
        const value = item.type === "number" ? finiteValue(input.value) : input.value.trim() || null;
        if (value === item.value) state.edits.delete(item.key);
        else state.edits.set(item.key, {raw: input.value, value});
        state.confirmed = false;
        renderWarning();
        draftChanged(index);
      }
      input.addEventListener(item.choices ? "change" : "input", edit);
      renderWarning();
      return box;
    }

    function renderBiometry(record, state) {
      if (!record.bio.length) return el("p", "這份文件沒有眼科生物測量結果。", "small muted");
      const table = el("table", undefined, "cataract-biometry-review-measurements");
      const head = el("thead"), header = el("tr"), rows = el("tbody");
      for (const label of ["測量項目", ...eyes]) {
        const cell = el("th", label);
        cell.scope = "col";
        header.append(cell);
      }
      head.append(header);
      for (const row of record.bio) {
        const line = el("tr"), unit = row.items.find(item => item.unit)?.unit;
        const title = el("th", [row.label, unit && `(${unit})`].filter(Boolean).join(" "));
        title.scope = "row";
        line.append(title);
        for (const item of row.items) {
          const cell = el("td");
          cell.append(inputFor(item, state, {compact: true}));
          line.append(cell);
        }
        rows.append(line);
      }
      table.append(head, rows);
      return table;
    }

    function renderIol(record, state) {
      const section = el("div", undefined, "cataract-biometry-review-iol");
      if (!record.tables.length) section.append(el("p", "這份文件沒有水晶體度數結果。", "small muted"));
      for (const [number, model] of record.tables.entries()) {
        const group = el("section", undefined, "cataract-biometry-review-table");
        const title = el("div", undefined, "cataract-biometry-review-source");
        title.append(el("h4", [model.table.eye, model.table.lens_model || `水晶體度數表 ${number + 1}`].filter(Boolean).join(" · ")));
        if (model.table.page_number) title.append(pageButton(model.table.page_number));
        group.append(title);
        if (model.table.verification_status !== "cross_checked") group.append(el("p", "此表為原報告的辨識結果，請對照來源核對。", "small muted"));
        const fields = el("div", undefined, "cataract-biometry-review-grid");
        fields.append(...model.general.map(item => inputFor(item, state)));
        group.append(fields);
        if (model.constants.length) {
          const constants = el("details"), grid = el("div", undefined, "cataract-biometry-review-grid");
          constants.append(el("summary", "公式常數"), grid);
          grid.append(...model.constants.map(item => inputFor(item, state)));
          group.append(constants);
        }
        for (const [position, row] of model.rows.entries()) {
          const rowGroup = el("fieldset", undefined, "cataract-biometry-review-row");
          const sourceLabels = [row.row.row_kind === "emmetropia" ? "正視預測" : "", row.row.selection === "marked" ? "原報告選取標記" : "",
            row.row.highlight_status === "highlighted" ? "原報告重點標示" : ""].filter(Boolean);
          rowGroup.append(el("legend", [`第 ${position + 1} 列`, ...sourceLabels].join(" · ")));
          const grid = el("div", undefined, "cataract-biometry-review-grid");
          grid.append(...row.items.map(item => inputFor(item, state, {label: labelText(item).replace(/^第 \d+ 列 /, "")})));
          rowGroup.append(grid);
          group.append(rowGroup);
        }
        section.append(group);
      }
      return section;
    }

    function renderFields() {
      const record = records[index];
      for (const tab of tabs.children) tab.setAttribute("aria-pressed", String(tab.dataset.kind === view));
      body.scrollTop = 0;
      if (!available(record)) {
        body.replaceChildren(el("p", record?.doc.error || "這份文件沒有可用的擷取資料。", "small muted"));
        return;
      }
      body.replaceChildren(view === "biometry" ? renderBiometry(record, states[index]) : renderIol(record, states[index]));
    }

    function setDocument(next) {
      if (!Number.isSafeInteger(next) || next < 0 || next >= records.length || next === index) return;
      index = next;
      const doc = records[index].doc;
      pageSelect = null;
      source.replaceChildren();
      const title = [doc.date, doc.label].filter(Boolean).join(" · ");
      if (title) source.append(el("p", title, "small muted"));
      const pages = doc.data?.metadata?.selected_pages || doc.pages || [];
      if (pages.length === 1) source.append(pageButton(pages[0]));
      if (pages.length > 1) {
        pageSelect = el("select");
        pageSelect.setAttribute("aria-label", "來源頁碼");
        for (const page of pages) {
          const option = el("option", pageLabel(page));
          option.value = String(page);
          pageSelect.append(option);
        }
        pageSelect.addEventListener("change", () => goToPage(Number(pageSelect.value)));
        source.append(pageSelect);
      }
      refresh();
    }

    function refresh() {
      renderFields();
      updateFooter();
    }

    for (const kind of kinds) {
      const tab = button(labels[kind], () => {view = kind; renderFields();}, "quiet-button");
      tab.dataset.kind = kind;
      tabs.append(tab);
    }
    tabs.hidden = kinds.length <= 1;
    confirm.addEventListener("change", () => {
      if (current() && !confirm.disabled && states[index]) {
        states[index].confirmed = confirm.checked;
        draftChanged(index);
      }
    });

    function reviewedDocument(record, position, category) {
      const {doc} = record, state = states[position];
      const {data, ...identity} = doc;
      delete identity.review;
      delete identity.capture_id;
      delete identity.saved_review;
      const review = {confirmed: state.confirmed};
      if (has(category, "iol")) review.iol_calculations = record.tables.map(({table}) => ({
        table_id: table.table_id, page_number: table.page_number,
        constants: (table.constants || []).map(({name}) => ({name})),
        rows: (table.rows || []).map(({row_kind, selection, highlight_status}) => ({row_kind, selection, highlight_status}))
      }));
      for (const item of record.items)
        if (has(category, item.kind)) setValue(review, item.path, valueOf(state, item));
      return {...identity, data: filteredData(data, category), review};
    }

    async function exportResult(toClipboard) {
      if (!current()) return;
      updateFooter();
      if (copy.disabled) return;
      const category = options.value;
      const omitted = records.filter(record => !available(record)).map(({doc}) => ({
        id: doc.id, date: doc.date, label: doc.label, pages: doc.pages, error: doc.error || "無擷取資料。"
      }));
      const documents = records.flatMap((record, position) => available(record) && states[position].confirmed
        ? [reviewedDocument(record, position, category)] : []);
      const json = JSON.stringify(window.CataractBiometryExchange.exportResult(result, documents, category, omitted), null, 2);
      if (!toClipboard) {
        download(`vghks-biometry-${result.source}-${category}.json`, json, "application/json;charset=utf-8");
        status.textContent = "已下載 JSON。";
        return;
      }
      try {
        await navigator.clipboard.writeText(json);
        if (current() && element.isConnected) status.textContent = "已複製 JSON。";
      } catch (_) {
        if (current() && element.isConnected) status.textContent = "無法存取剪貼簿，可使用下載 JSON。";
      }
    }

    if (records.length) setDocument(Number.isSafeInteger(documentIndex) && records[documentIndex] ? documentIndex : 0);
    else refresh();
    return {element, header, setDocument, refresh};
  }

  return {create, labels};
})();
