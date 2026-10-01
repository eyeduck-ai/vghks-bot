"use strict";
window.LibraryDataUI = (() => {
  let mode = "soap", data = null, sequence = 0;
  const patients = new Set(), lists = new Set();
  const page = document.querySelector("#libraryPage");
  const tabs = document.createElement("nav"); tabs.className = "library-data-tabs";
  tabs.setAttribute("aria-label", "病歷管理資料類型");
  const tools = document.createElement("div"); tools.className = "library-data-tools"; tools.hidden = true;
  const query = document.createElement("input"); query.type = "search"; query.placeholder = "姓名／病歷號";
  query.setAttribute("aria-label", "搜尋已存病人快取"); query.maxLength = 100;
  const categories = document.createElement("div"); categories.className = "library-data-categories";
  const clear = document.createElement("button"); clear.type = "button"; clear.textContent = "清除勾選資料"; clear.className = "danger";
  const refreshButton = document.createElement("button"); refreshButton.type = "button"; refreshButton.textContent = "重新檢閱本機資料";
  tools.append(query, refreshButton, clear, categories);
  const content = document.createElement("div"); content.id = "libraryDataContent"; content.hidden = true;
  const heading = page.querySelector(".page-heading"); heading.after(tabs, tools, content);
  const buttons = new Map();
  for (const [key, label] of [["soap", "SOAP 病歷"], ["patients", "病人檢查快取"], ["lists", "門診掛號清單"]]) {
    const control = document.createElement("button"); control.type = "button"; control.textContent = label;
    control.addEventListener("click", () => act(async () => {mode = key; display(); await loadLibrary();}));
    buttons.set(key, control); tabs.append(control);
  }
  const bytes = value => value >= 1048576 ? (value / 1048576).toFixed(1) + " MB" : Math.ceil(value / 1024) + " KB";
  function display() {
    for (const [key, control] of buttons) control.setAttribute("aria-pressed", String(key === mode));
    heading.querySelector(".actions").hidden = mode !== "soap";
    for (const id of ["libraryForm", "librarySummary", "libraryRecords", "libraryPagination", "libraryToTools", "deleteRecords"])
      document.getElementById(id).hidden = mode !== "soap";
    tools.hidden = content.hidden = mode === "soap";
    query.hidden = categories.hidden = mode !== "patients";
    clear.disabled = !!root?.read_only;
  }
  function render() {
    if (!data) return;
    const names = new Map(data.categories.map(value => [value.id, value.name]));
    if (mode === "patients") {
      content.replaceChildren(table(["選取", "病人", "已存資料", "附件", "最後保存"], data.patients.map(row => [
        check("選取 " + row.mrn, patients.has(row.mrn), value => value ? patients.add(row.mrn) : patients.delete(row.mrn)),
        patientName(row), Object.entries(row.categories).map(([key, value]) => `${names.get(key)} ${value.count} 筆／${value.versions} 版本`).join(" · "),
        bytes(row.attachment_bytes), time(row.updated_at)
      ])));
      if (!data.patients.length) content.append(empty("沒有符合的病人檢查快取。"));
      if (!categories.childElementCount) for (const item of data.categories) {
        const label = node("label"), input = node("input"); input.type = "checkbox"; input.value = item.id;
        label.append(input, document.createTextNode(item.name)); categories.append(label);
      }
    } else if (mode === "lists") {
      content.replaceChildren(table(["選取", "日期", "查詢帳號", "病人數", "最後保存"], data.lists.map(row => {
        const key = JSON.stringify([row.account, row.day]);
        return [check("選取 " + row.day + " 的門診清單", lists.has(key), value => value ? lists.add(key) : lists.delete(key)),
          row.day, row.account, row.count, time(row.updated_at)];
      })));
      if (!data.lists.length) content.append(empty("沒有已保存的門診掛號清單。"));
    }
  }
  async function load() {
    const current = ++sequence;
    const result = await api("/library/data/read", {q: query.value});
    if (current !== sequence) return;
    data = result; render(); display();
  }
  async function changed(value) {
    const detail = {account, ...value};
    reviewSignature = ""; libraryRecordSequence++;
    for (const id of ["librarySoapDialog", "dataDialog", "scanBrowserDialog"]) document.getElementById(id)?.close();
    if (value.mrns?.includes(window.FileCompare?.patient?.())) window.FileCompare?.clear();
    if(value.mrns?.length){window.ReviewHistoryUI?.reset();window.ScanBrowser?.reset();}
    window.dispatchEvent(new CustomEvent("clinicaldatacleared", {detail}));
    for (const id of ["moduleFrame", "cataractFrame"]) document.getElementById(id)?.contentWindow?.postMessage({type:"bot:data-cleared", ...detail}, location.origin);
    await refresh();
  }
  clear.addEventListener("click", () => act(async () => {
    const targetAccount = account;
    const request = mode === "patients" ? {mrns:[...patients], categories:[...categories.querySelectorAll("input:checked")].map(input => input.value)} :
      {lists:[...lists].map(key => {const [account, day] = JSON.parse(key); return {account, day};})};
    const preview = await api("/library/data/preview-delete", request);
    if(targetAccount!==account)return;
    const names = new Map(data.categories.map(row => [row.id, row.name]));
    const people = data.patients.filter(row => preview.mrns.includes(row.mrn)).map(row => `${row.name || "姓名未提供"}（${row.mrn}）`).join("、");
    const message = preview.lists.length ? `清除 ${preview.lists.length} 天門診掛號清單快取？` :
      `清除 ${people} 的 ${preview.categories.map(key => names.get(key)).join("、")}？\n${preview.records} 筆資料、${preview.versions} 個版本；釋放 ${preview.attachments} 份附件（${bytes(preview.attachment_bytes)}）。`;
    const cascade = preview.cascaded.length ? "\n連帶清除：" + preview.cascaded.map(key => names.get(key)).join("、") + "。" : "";
    if (!await confirmDelete(message + cascade + "\n病人集合、SOAP、手動 TAG 與備註保留。")||targetAccount!==account) return;
    const deleted = await api("/library/data/delete", {...request, fingerprint:preview.fingerprint});
    if(targetAccount!==account)return;
    patients.clear(); lists.clear(); await changed(deleted); await load(); say("勾選資料已清除。");
  }));
  refreshButton.addEventListener("click", () => act(load));
  query.addEventListener("input", () => act(load));
  window.addEventListener("clinicaldatacleared", () => {data = null;});
  display();
  return {get mode(){return mode;}, load, changed, reset(){sequence++; patients.clear(); lists.clear(); data=null; query.value="";for(const input of categories.querySelectorAll("input"))input.checked=false;}};
})();
