"use strict";

window.CataractNumeric = (() => {
  const exams = ["Va", "VAcC", "驗光-散瞳前", "KM", "Endothelial No", "IOP-pneumo",
    "IOP-recheck", "驗光-散瞳後", "配鏡", "CCT", "ACD", "LT", "AXL", "Basic Schirmer", "Ishihara"];
  const refraction = new Set(["驗光-散瞳前", "驗光-散瞳後", "配鏡"]);
  const hasValue = value => !["", "-", "—", "--", "N/A", "NA"].includes(String(value ?? "").trim().toUpperCase());
  const standardFormat = new Intl.NumberFormat("en-US", {maximumFractionDigits:3, useGrouping:false});
  const opticalFormat = new Intl.NumberFormat("en-US", {minimumFractionDigits:2, maximumFractionDigits:2, useGrouping:false});
  const number = (value, fixed = false, signed = false) => value !== null && value !== undefined && String(value).trim() !== "" && Number.isFinite(Number(value))
    ? (signed && Number(value) > 0 ? "+" : "") + (fixed ? opticalFormat : standardFormat).format(Number(value)) : "—";
  // Apply signs only to identified refraction fields; retain unrecognized source text.
  function displayValue(value, exam = "", metric = "") {
    const raw = String(value ?? "");
    const text = raw.normalize("NFKC").replace(/−/g, "-").trim();
    const labelOf = value => String(value).normalize("NFKC").replace(/[\[(][^\])]*[\])]/g, "")
      .replace(/\b(?:OD|OS|OU)\b/gi, "").replace(/驗光\s*[-－–−]\s*散瞳[前後]|配鏡/g, "").trim().replace(/[\s/:：_-]+/g, "");
    const decimal = "[+-]?(?:\\d+(?:\\.\\d*)?|\\.\\d+)";
    const signed = text => Number(text) > 0 && !text.startsWith("+") ? "+" + text : text;
    const field = /^(?:sph|sphere|cyl|cylinder|se)$/i;
    const datedColumn = /(?:\d{3,4}[-/.]\d{1,2}[-/.]\d{1,2})/.test(String(metric));
    if ((field.test(labelOf(metric)) || datedColumn && field.test(labelOf(exam))) && new RegExp("^" + decimal + "$").test(text)) return signed(text);
    if (/\bKM\b/i.test(exam + " " + metric) && /\bCYL\s+/i.test(text))
      return text.replace(new RegExp("(\\bCYL\\s+)(" + decimal + ")(?=\\s*(?:[x×]|$))", "gi"),
        (_, label, value) => label + signed(value));
    if (!/驗光\s*[-－–−]\s*散瞳[前後]|配鏡/.test(exam + " " + metric)) return raw;
    const match = text.match(new RegExp("^(" + decimal + ")(\\s+)(" + decimal + ")(\\s*[x×]\\s*)(" + decimal + ")$", "i"));
    if (!match || !Number.isInteger(Number(match[5])) || Number(match[5]) < 0 || Number(match[5]) > 180) return raw;
    return signed(match[1]) + match[2] + signed(match[3]) + match[4] + match[5];
  }
  function labelledText(value) {
    const text = node("span");
    for (const part of String(value ?? "").split(/\b(SE|K1|K2|Kavg|CYL)\b/gi)) {
      if (!part) continue;
      text.append(/^(?:SE|K1|K2|Kavg|CYL)$/i.test(part)
        ? node("span", part, "measurement-label") : document.createTextNode(part));
    }
    return text;
  }
  function measurementText(measurement, line) {
    const v = measurement.values;
    if (measurement.kind === "refraction") {
      return line ? `SE ${number(v.se, false, true)}` : `${number(v.sph, true, true)} ${number(v.cyl, true, true)} X ${number(v.axis)}`;
    }
    if (line === 2) return `Kavg ${number(v.kavg)} | CYL ${number(v.cyl, true, true)}${v.cyl_axis != null ? ` X ${number(v.cyl_axis)}` : ""}`;
    const k = line + 1;
    return `K${k} ${number(v[`k${k}`], true)}${v[`r${k}`] != null ? ` (${number(v[`r${k}`])})` : ""}${v[`axis${k}`] != null ? ` X ${number(v[`axis${k}`])}` : ""}`;
  }
  const node = (tag, value, className) => {
    const element = document.createElement(tag);
    if (value !== undefined) element.textContent = value;
    if (className) element.className = className;
    return element;
  };
  const stamp = entry => entry.row.date || entry.cells[0]?.date || "";
  function rawTable(row) {
    const table = node("table", undefined, "source-table");
    const body = node("tbody");
    for (let index = 0; index < Math.max(row.headers?.length || 0, row.values?.length || 0); index++) {
      const tr = node("tr"), th = node("th", row.headers?.[index] || `欄 ${index + 1}`);
      th.scope = "row";
      tr.append(th, node("td", row.values?.[index] || ""));
      body.append(tr);
    }
    table.append(body);
    return table;
  }
  function create(rows = [], state = {}, changed = () => {}) {
    state.eye ||= "both";
    state.expanded ||= new Set(exams);
    const section = node("section", undefined, "cataract-section cataract-numeric");
    section.dataset.eye = state.eye;
    const header = node("div", undefined, "cataract-numeric-head");
    header.append(node("h3", "歷年眼科數值"));
    const eyePicker = node("div", undefined, "cataract-eye-picker");
    eyePicker.setAttribute("role", "group");
    eyePicker.setAttribute("aria-label", "強調眼別");
    for (const [value, label] of [["both", "雙眼"], ["OD", "OD"], ["OS", "OS"]]) {
      const control = node("button", label);
      control.type = "button";
      control.setAttribute("aria-pressed", String(state.eye === value));
      control.addEventListener("click", () => {
        state.eye = value;
        section.dataset.eye = value;
        for (const item of eyePicker.children) item.setAttribute("aria-pressed", String(item === control));
        changed();
      });
      eyePicker.append(control);
    }
    header.append(eyePicker);
    section.append(header);
    for (const exam of exams) {
      const entries = rows.flatMap((row, index) => {
        if (!(row.exams || []).includes(exam)) return [];
        const cells = (row.cells || []).filter(cell => cell.exam === exam && hasValue(cell.raw));
        const original = row.unparsed && !row.cells?.length && (row.values || []).some(value =>
          hasValue(value) && !/^(?:\d{3,4}[-/.]\d{1,2}[-/.]\d{1,2}|OD|OS|OU)$/i.test(String(value).trim()));
        return cells.length || original ? [{row, cells, index}] : [];
      });
      if (!entries.length) continue;
      entries.sort((a, b) => stamp(b).localeCompare(stamp(a)) || a.index - b.index);
      const group = node("details", undefined, "cataract-numeric-group");
      group.dataset.exam = exam;
      group.open = state.expanded.has(exam);
      const summary = node("summary");
      summary.append(node("strong", exam), node("span", `${entries.length} 筆`, "cataract-numeric-count"));
      if (entries.length) summary.append(node("span", `最新 ${stamp(entries[0]) || "日期未提供"}`, "cataract-numeric-latest"));
      group.append(summary);
      if (entries.length) {
        const wrap = node("div", undefined, "cataract-numeric-table-wrap");
        const table = node("table", undefined, "cataract-numeric-table");
        const head = node("thead"), hr = node("tr");
        for (const label of ["日期", "OD", "OS"]) {
          const th = node("th", label); th.scope = "col"; hr.append(th);
        }
        head.append(hr);
        const body = node("tbody");
        for (const entry of entries) {
          const lines = exam === "KM" ? 3 : refraction.has(exam) ? 2 : 1;
          for (let index = 0; index < lines; index++) {
          const tr = node("tr"), dateCell = node("td");
          tr.className = index === lines - 1 ? "measurement-end" : "measurement-continuation";
          dateCell.rowSpan = lines;
          dateCell.append(node("time", stamp(entry) || "日期未提供"));
          const unknown = entry.cells.filter(cell => !["OD", "OS"].includes(cell.side));
          if (unknown.length) {
            const badge = node("small", "未分側", "cataract-unknown-eye");
            badge.title = "此列另有未標示眼別的數值，請查看下方原始資料";
            dateCell.append(badge);
          }
          if (!index) tr.append(dateCell);
          for (const eye of ["OD", "OS"]) {
            const cell = node("td", undefined, `eye-${eye}`);
            const matches = entry.cells.filter(item => item.side === eye);
            const parsed = entry.row.measurements?.find(item => item.exam === exam && item.side === eye && item.status !== "unparsed");
            if (lines > 1 && parsed) {
              const value = node("strong");value.append(labelledText(measurementText(parsed, index)));cell.append(value);
              tr.append(cell);
              continue;
            }
            if (index) continue;
            cell.rowSpan = lines;
            if (!matches.length) cell.append(node("span", "—", "muted"));
            const distinctMetrics = new Set(matches.map(item => String(item.metric || "").trim()).filter(Boolean));
            for (const measurement of matches) {
              const line = node("div", undefined, "cataract-measurement");
              const metric = String(measurement.metric || "").trim();
              if (metric) line.title = metric;
              // The exam heading and eye column already name a single value.
              // Keep a label only if one eye has different measurements in this row.
              if (matches.length > 1 && distinctMetrics.size > 1 && metric) {
                const label = node("small");label.append(labelledText(metric));line.append(label);
              }
              const value = node("strong");
              value.append(labelledText(displayValue(measurement.raw, exam, metric) + (measurement.unit ? ` ${measurement.unit}` : "")));
              line.append(value);
              cell.append(line);
            }
            tr.append(cell);
          }
          body.append(tr);
          }
        }
        table.append(head, body); wrap.append(table); group.append(wrap);
      } else group.append(node("p", "尚無資料", "small muted"));
      group.addEventListener("toggle", () => {
        if (group.open) state.expanded.add(exam); else state.expanded.delete(exam);
        changed();
      });
      section.append(group);
    }
    const sourceRows = rows.filter(row => (row.exams || []).some(exam => exams.includes(exam)))
      .sort((a, b) => String(b.date || "").localeCompare(String(a.date || "")));
    if (sourceRows.length) {
      const originals = node("details", undefined, "cataract-raw-group");
      originals.open = !!state.rawOpen;
      originals.append(node("summary", `查看原始資料（${sourceRows.length} 筆）`));
      const content = node("div", undefined, "cataract-raw-content");
      originals.append(content);
      const fill = () => {
        if (content.childElementCount) return;
        sourceRows.forEach((row, index) => {
          const item = node("section", undefined, "cataract-raw-item");
          item.append(node("h4", `${row.date || "日期未提供"} · ${row.title || (row.exams || []).join("、") || `紀錄 ${index + 1}`}`));
          const wrap = node("div", undefined, "table-wrap");
          wrap.append(rawTable(row));
          item.append(wrap);
          content.append(item);
        });
      };
      if (originals.open) fill();
      originals.addEventListener("toggle", () => {
        state.rawOpen = originals.open;
        if (originals.open) fill();
        changed();
      });
      section.append(originals);
    }
    return section;
  }
  return {create, exams, displayValue, labelledText};
})();
