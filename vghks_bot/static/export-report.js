"use strict";
// Shipped inside the ZIP. Local script data works without fetch or a web server.
(() => {
  const data = window.EXPORT_REPORT;
  const node = (tag, text, cls) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (cls) item.className = cls;
    return item;
  };
  const link = (label, path, download = false) => {
    const item = node("a", label); item.href = path;
    if (download) item.download = "";
    return item;
  };
  const area = document.querySelector("#report");
  area.replaceChildren();
  const header = node("header");
  header.append(link("← 病人清單", "../index.html"), node("h1", `${data.member.name} · ${data.member.mrn}`),
    node("p", `白內障術前報告 · 已存資料 ${data.updated_at || "時間未提供"} · 匯出 ${data.exported_at}`));
  const controls = node("nav");
  for (const [label, id] of [["SOAP", "soap"], ["歷年數值", "numeric"], ["醫囑報告", "orders"]])
    controls.append(link(label, "#" + id));
  controls.append(link("SOAP 文字", "SOAP.txt", true), link("數值 CSV", "numeric.csv", true),
    link("完整資料 JSON", "report.json", true));
  const print = node("button", "列印摘要");print.type = "button";
  print.addEventListener("click", () => window.print());controls.append(print);
  header.append(controls);area.append(header);
  if (data.missing.length) {
    const warning = node("details", undefined, "export-missing");warning.open = true;
    warning.append(node("summary", "未保存／未完整取得的資料"));
    const list = node("ul");for (const text of data.missing) list.append(node("li", text));
    warning.append(list);area.append(warning);
  }
  const soap = node("section");soap.id = "soap";soap.append(node("h2", "SOAP"));
  const record = data.latest_soap?.record;
  if (record) soap.append(node("p", [record.date, record.section, record.case_no].filter(Boolean).join(" · ")),
    window.SOAPView.create(record));
  else soap.append(node("p", "尚無已保存的眼科 SOAP"));
  area.append(soap);
  const numeric = node("section");numeric.id = "numeric";
  numeric.append(node("h2", "歷年數值"), window.CataractNumeric.create(data.numeric));area.append(numeric);
  const orders = node("section");orders.id = "orders";orders.append(node("h2", "歷年醫囑報告"));
  const sorted = [...data.orders].sort((a,b) => Number(!/\bDBR\b/i.test(a.name)) - Number(!/\bDBR\b/i.test(b.name)) ||
    a.name.localeCompare(b.name, "zh-Hant") || b.date.localeCompare(a.date));
  for (const order of sorted) {
    const block = node("section", undefined, "export-order");
    block.append(node("h3", `${order.name} · ${order.date || "日期未提供"}`),
      link("下載文字報告與明細", order.text_file, true));
    const files = node("div", undefined, "export-files"), preview = node("div", undefined, "export-preview");
    for (const [index, asset] of order.attachments.entries()) {
      const item = node("div", undefined, "export-file");
      const type = asset.mime === "application/pdf" ? "PDF" : "圖片";
      if (!asset.file) item.append(node("span", `檔案 ${index + 1} · ${type} · 未保存`));
      else {
        const choose = node("button", `檢視檔案 ${index + 1} · ${type}`);choose.type = "button";
        choose.addEventListener("click", () => {
          if (preview.dataset.file === asset.file) return;
          preview.dataset.file = asset.file;
          for (const button of files.querySelectorAll("button")) button.setAttribute("aria-pressed", String(button === choose));
          const documentNode = node(type === "PDF" ? "iframe" : "img");
          documentNode.src = asset.file + (type === "PDF" ? "#navpanes=0&toolbar=1" : "");
          documentNode.title = `${order.name} 檔案 ${index + 1}`;
          if (type !== "PDF") documentNode.alt = documentNode.title;
          preview.replaceChildren(documentNode);
        });
        choose.setAttribute("aria-pressed", "false");
        const open = link("另開檔案", asset.file);open.target = "_blank";open.rel = "noopener";
        item.append(choose, link("下載原檔", asset.file, true), open);
      }
      files.append(item);
    }
    block.append(files, preview);
    for (const [label, rows] of [["文字報告", order.texts], ["醫囑明細", order.details]]) for (const row of rows) {
      const detail = node("details");detail.open = true;detail.append(node("summary", label));
      if (row.text) detail.append(node("pre", row.text));
      if (Object.keys(row.fields || {}).length) {
        const table = node("table"), body = node("tbody");
        for (const [key, value] of Object.entries(row.fields)) {
          const tr = node("tr"), th = node("th", key);th.scope = "row";
          tr.append(th, node("td", String(value ?? "")));body.append(tr);
        }
        table.append(body);detail.append(table);
      }
      block.append(detail);
    }
    if (order.issues?.length) block.append(node("p", order.issues.map(issue => issue.message).join("；"), "export-missing"));
    orders.append(block);
  }
  if (!sorted.length) orders.append(node("p", "尚無已保存的指定醫囑報告"));
  area.append(orders);
})();
