"use strict";

// The parent workbench owns the selection; embedded tools send only verified
// account-scoped attachment digests and display metadata.
window.FileCompare = (() => {
  const limit = 6;
  let account = "", mrn = "", patientName = "", items = [], activeIndex = 0, remoteCount = 0, feedbackTimer = 0, linkedFrame = null;
  let remoteDigests = new Set(), remoteItems = [], inlineHost = null, inlineActive = 0, inlineActiveDigest = "";
  let numericKey = "";
  const embedded = window.parent !== window;
  const make = (tag, label, cls) => {
    const el = document.createElement(tag);
    if (label !== undefined) el.textContent = label;
    if (cls) el.className = cls;
    return el;
  };
  const button = (label, fn) => {
    const el = make("button", label);
    el.type = "button";
    el.addEventListener("click", fn);
    return el;
  };
  function valid(value) {
    return value && /^[a-f0-9]{32}$/.test(value.account) && /^\S{1,32}$/.test(value.mrn) &&
      /^[a-f0-9]{64}$/.test(value.digest) && ["application/pdf", "image/png", "image/jpeg", "image/gif"].includes(value.mime);
  }
  function context(nextAccount, nextMrn, nextName = "") {
    if (embedded) {
      if (account !== nextAccount || mrn !== nextMrn) {
        remoteCount = 0; remoteDigests = new Set(); remoteItems = []; inlineActive = 0; inlineActiveDigest = "";
        renderInline();
      }
      account = nextAccount || "";
      mrn = nextMrn || "";
      patientName = nextName || "";
      changed();
      window.parent.postMessage({type:"bot:compare-context", account:nextAccount, mrn:nextMrn, patient_name:nextName}, location.origin);
      return;
    }
    if (account === nextAccount && mrn === nextMrn) {
      if (nextName && patientName !== nextName) {patientName = nextName; render();}
      return;
    }
    account = nextAccount || "";
    mrn = nextMrn || "";
    patientName = nextName || "";
    items = [];
    activeIndex = 0;
    numericKey = "";
    document.getElementById("fileCompareNumeric")?.replaceChildren();
    document.getElementById("fileComparePanes")?.replaceChildren();
    document.getElementById("fileCompareTabs")?.replaceChildren();
    document.getElementById("fileCompareDialog")?.close();
    render();
  }
  function changed() {
    window.dispatchEvent(new CustomEvent("filecomparechange", {detail:{count:count(), limit}}));
  }
  function count() {return embedded ? remoteCount : items.length;}
  function has(digest) {return embedded ? remoteDigests.has(digest) : items.some(item => item.digest === digest);}
  function announce(value) {
    if (embedded) {
      window.dispatchEvent(new CustomEvent("filecomparemessage", {detail:value}));
      return;
    }
    const feedback = document.getElementById("fileCompareFeedback");
    if (!feedback) return;
    feedback.textContent = value;
    feedback.hidden = false;
    clearTimeout(feedbackTimer);
    feedbackTimer = setTimeout(() => {feedback.hidden = true;}, 3500);
  }
  function add(value) {
    if (!valid(value)) throw new Error("附件資訊不完整，無法加入比較。");
    if (embedded) {
      window.parent.postMessage({type:"bot:compare-add", item:value}, location.origin);
      return;
    }
    if (account !== value.account || mrn !== value.mrn) throw new Error("病人或帳號已切換，請從目前畫面重新選取檔案。");
    if (items.some(item => item.digest === value.digest)) {announce("此檔案已在比較清單中。"); return false;}
    if (items.length >= limit) {announce("比較清單最多可加入 6 份檔案。"); throw new Error("比較清單最多可加入 6 份檔案。");}
    items.push({...value, zoom:1, selectionOrder:Math.max(0, ...items.map(item => item.selectionOrder)) + 1});
    activeIndex = items.length - 1;
    render();
    announce(`已加入比較清單（${items.length} / ${limit}）。`);
    return true;
  }
  function sendState(frame, error = "") {
    frame?.contentWindow?.postMessage({type:"bot:compare-state", account, mrn, count:items.length,
      digests:items.map(item => item.digest),
      items:items.map(({digest, mime, name, source, date, zoom, fileIndex, fileCount, selectionOrder, reportId}) =>
        ({digest, mime, name, source, date, zoom, fileIndex, fileCount, selectionOrder, reportId})),
      limit, error}, location.origin);
  }
  function receive(event, expectedFrame, activeAccount) {
    if (event.origin !== location.origin || event.source !== expectedFrame?.contentWindow) return;
    linkedFrame = expectedFrame;
    if (event.data?.type === "bot:compare-context") {
      if (event.data.account === activeAccount) context(activeAccount, event.data.mrn, event.data.patient_name);
      sendState(expectedFrame);
    } else if (event.data?.type === "bot:compare-add") {
      if (event.data.item?.account !== activeAccount) return;
      let error = "";
      try {add(event.data.item);} catch (exc) {error = exc.message;}
      sendState(expectedFrame, error);
    } else if (event.data?.type === "bot:compare-remove") {
      if (event.data.account !== activeAccount || event.data.mrn !== mrn) return;
      removeDigest(event.data.digest);
      sendState(expectedFrame);
    } else if (event.data?.type === "bot:compare-move") {
      if (event.data.account !== activeAccount || event.data.mrn !== mrn) return;
      const index = items.findIndex(item => item.digest === event.data.digest);
      if (index >= 0 && [-1, 1].includes(event.data.delta)) move(index, event.data.delta);
    } else if (event.data?.type === "bot:compare-zoom") {
      if (event.data.account !== activeAccount || event.data.mrn !== mrn) return;
      const item = items.find(row => row.digest === event.data.digest);
      if (item && [-.25, .25].includes(event.data.delta)) {
        item.zoom = Math.max(.5, Math.min(3, item.zoom + event.data.delta)); render();
      }
    }
  }
  function removeDigest(digest) {
    if (!/^[a-f0-9]{64}$/.test(digest)) return;
    if (embedded) {
      window.parent.postMessage({type:"bot:compare-remove", account, mrn, digest}, location.origin);
      return;
    }
    const index = items.findIndex(item => item.digest === digest);
    if (index >= 0) remove(index);
  }
  function mount() {
    if (embedded || document.getElementById("fileCompareDialog")) return;
    const tray = make("button", "檔案比較", "compare-tray");
    tray.id = "fileCompareTray";
    tray.type = "button";
    tray.hidden = true;
    tray.addEventListener("click", open);
    const feedback = make("div", "", "compare-feedback");
    feedback.id = "fileCompareFeedback";
    feedback.setAttribute("role", "status");
    feedback.hidden = true;
    const dialog = make("dialog", undefined, "file-compare-dialog");
    dialog.id = "fileCompareDialog";
    dialog.setAttribute("aria-labelledby", "fileCompareTitle");
    const heading = make("div", undefined, "file-compare-heading");
    const titleGroup = make("div", undefined, "file-compare-title-group");
    const title = make("h2", "檢查檔案比較");
    title.id = "fileCompareTitle";
    const patient = make("p", "", "dialog-patient");
    patient.id = "fileComparePatient";
    titleGroup.append(title, patient);
    const count = make("span", "", "file-compare-count");
    count.id = "fileCompareCount";
    const numericToggle = button("收合數值", () => {
      const workspace = document.getElementById("fileCompareWorkspace");
      const closed = workspace.classList.toggle("numeric-closed");
      numericToggle.textContent = closed ? "展開數值" : "收合數值";
      numericToggle.setAttribute("aria-expanded", String(!closed));
    });
    numericToggle.id = "fileCompareNumericToggle";
    numericToggle.setAttribute("aria-controls", "fileCompareNumeric");
    numericToggle.setAttribute("aria-expanded", "true");
    numericToggle.hidden = true;
    const close = button("×", () => dialog.close());
    close.setAttribute("aria-label", "關閉檢查檔案比較");close.title="關閉";
    heading.append(titleGroup, count, numericToggle, close);
    dialog.setAttribute("aria-describedby", "fileComparePatient");
    const tabs = make("div", undefined, "file-compare-tabs");
    tabs.id = "fileCompareTabs";
    tabs.setAttribute("role", "tablist");
    const panes = make("div", undefined, "file-compare-panes");
    panes.id = "fileComparePanes";
    const workspace = make("div", undefined, "file-compare-workspace");
    workspace.id = "fileCompareWorkspace";
    const numeric = make("aside", undefined, "file-compare-numeric");
    numeric.id = "fileCompareNumeric";
    numeric.setAttribute("aria-label", "歷年眼科數值");
    numeric.hidden = true;
    workspace.append(numeric, panes);
    dialog.append(heading, tabs, workspace);
    dialog.addEventListener("click", event => {if (event.target === dialog) dialog.close();});
    dialog.addEventListener("close", () => {numericKey = ""; render();});
    document.body.append(tray, feedback, dialog);
  }
  function open() {
    if (embedded) {window.parent.postMessage({type:"bot:compare-open"}, location.origin); return;}
    mount();
    if (!items.length) {announce("請先加入要比較的檔案。"); return;}
    const dialog = document.getElementById("fileCompareDialog");
    if (!dialog.open) dialog.showModal();
    render();
    void loadNumeric();
  }
  async function loadNumeric() {
    if (embedded) return;
    const frame = document.getElementById("cataractFrame");
    const visible = frame?.hasAttribute("src") && !frame.hidden &&
      !document.getElementById("modulePage")?.hidden && frame.dataset.account === account;
    const toggle = document.getElementById("fileCompareNumericToggle");
    const rail = document.getElementById("fileCompareNumeric");
    toggle.hidden = !visible;
    rail.hidden = !visible;
    if (!visible) return;
    const key = [account, frame.dataset.cohort, mrn].join(":");
    if (key === numericKey) return;
    numericKey = key;
    rail.replaceChildren(make("p", "正在讀取已存數值…", "small muted"));
    try {
      const data = await api("/analysis/results", {cohort_id: frame.dataset.cohort,
        mrn, module: "cataract", start: "", end: ""});
      if (numericKey !== key || data.member.account_id !== account || data.member.mrn !== mrn) return;
      rail.replaceChildren(window.CataractNumeric.create(data.numeric || [],
        {eye:"both", expanded:new Set(window.CataractNumeric.exams)}));
    } catch (error) {
      if (numericKey === key) rail.replaceChildren(make("p", error.message || "無法讀取已存數值。", "small muted"));
    }
  }
  function move(index, delta) {
    const other = index + delta;
    if (other < 0 || other >= items.length) return;
    [items[index], items[other]] = [items[other], items[index]];
    if (activeIndex === index) activeIndex = other;
    else if (activeIndex === other) activeIndex = index;
    render();
  }
  function remove(index) {
    items.splice(index, 1);
    activeIndex = Math.min(activeIndex, Math.max(0, items.length - 1));
    if (!items.length) document.getElementById("fileCompareDialog")?.close();
    render();
  }
  function pane(item, index, inline = false) {
    const collection = inline ? remoteItems : items;
    const wrap = make("section", undefined, "file-compare-pane");
    wrap.dataset.digest = item.digest;
    wrap.dataset.active = String(index === (inline ? inlineActive : activeIndex));
    const columns = collection.length <= 2 ? collection.length : collection.length === 4 ? 2 : 3;
    wrap.style.setProperty("--pane-span", String(collection.length === 5 && index >= 3 ? 3 : 6 / columns));
    const header = make("div", undefined, "file-compare-pane-head");
    const label = ClinicalUI.comparisonName(item, collection);
    const name = make("strong", label);
    name.title = [label, item.name, item.source].filter(Boolean).join(" · ");
    const caption = make("span", [item.name, item.source].filter(Boolean).join(" · "));
    const buttons = make("div", undefined, "file-compare-pane-actions");
    const itemIndex = () => collection.findIndex(row => row.digest === item.digest);
    const moveItem = delta => inline
      ? window.parent.postMessage({type:"bot:compare-move", account, mrn, digest:item.digest, delta}, location.origin)
      : move(itemIndex(), delta);
    const zoomItem = delta => inline
      ? window.parent.postMessage({type:"bot:compare-zoom", account, mrn, digest:item.digest, delta}, location.origin)
      : (item.zoom = Math.max(.5, Math.min(3, item.zoom + delta)), render());
    buttons.append(button("←", () => moveItem(-1)), button("→", () => moveItem(1)),
      button("−", () => zoomItem(-.25)),
      make("span", Math.round(item.zoom * 100) + "%"),
      button("＋", () => zoomItem(.25)),
      button("×", () => inline ? removeDigest(item.digest) : remove(itemIndex())));
    for (const [i, el] of [...buttons.children].entries()) if (i < 2) el.setAttribute("aria-label", i ? "將檔案向右移" : "將檔案向左移");
    buttons.children[2].setAttribute("aria-label", "縮小檔案");
    buttons.children[4].setAttribute("aria-label", "放大檔案");
    buttons.children[5].setAttribute("aria-label", `移除 ${label}`);
    buttons.children[5].title = "移除檔案";
    buttons.children[0].disabled = index === 0;
    buttons.children[1].disabled = index === collection.length - 1;
    header.append(name, caption, buttons);
    const scroll = make("div", undefined, "file-compare-scroll");
    const url = location.pathname === "/tools" && !new URLSearchParams(location.search).has("account")
      ? `/api/analysis/asset?id=${item.digest}` : `/api/accounts/${item.account}/analysis/asset?id=${item.digest}`;
    const documentNode = make(item.mime === "application/pdf" ? "iframe" : "img");
    documentNode.src = item.mime === "application/pdf" ? ClinicalUI.pdfURL(url) : url;
    documentNode.title = name.title;
    if (documentNode.tagName === "IMG") documentNode.alt = documentNode.title;
    buttons.append(ClinicalUI.assetTools({url,asset:item,name:[label,item.name].filter(Boolean).join(" "),image:documentNode,compact:true}));
    documentNode.style.width = (item.zoom * 100) + "%";
    documentNode.style.height = item.mime === "application/pdf" ? (item.zoom * 100) + "%" : "auto";
    scroll.append(documentNode);
    wrap.append(header, scroll);
    return wrap;
  }
  function renderInline(host = inlineHost) {
    if (!embedded || !host) return;
    inlineHost = host;
    const positions = new Map([...host.querySelectorAll(".file-compare-pane")].map(node => {
      const scroll = node.querySelector(".file-compare-scroll");
      return [node.dataset.digest, [scroll?.scrollLeft || 0, scroll?.scrollTop || 0]];
    }));
    host.replaceChildren();
    if (!remoteItems.length) {
      host.append(make("p", "尚未選取檔案。請到「數值與醫囑」點開報告，再勾選附件加入比較。", "empty-result"));
      return;
    }
    const remembered = remoteItems.findIndex(item => item.digest === inlineActiveDigest);
    inlineActive = remembered >= 0 ? remembered : Math.min(inlineActive, remoteItems.length - 1);
    inlineActiveDigest = remoteItems[inlineActive].digest;
    const tabs = make("div", undefined, "file-compare-inline-tabs");
    tabs.setAttribute("aria-label", "比較檔案切換");
    const panes = make("div", undefined, "file-compare-inline-panes");
    for (const [index, item] of remoteItems.entries()) {
      const tab = button(ClinicalUI.comparisonName(item, remoteItems), () => {
        inlineActive = index; inlineActiveDigest = item.digest; renderInline();
      });
      tab.setAttribute("aria-pressed", String(index === inlineActive));
      tabs.append(tab);
      const node = pane(item, index, true);
      panes.append(node);
    }
    host.append(tabs, panes);
    for (const node of panes.children) {
      const previous = positions.get(node.dataset.digest);
      if (!previous) continue;
      const scroll = node.querySelector(".file-compare-scroll");
      scroll.scrollLeft = previous[0]; scroll.scrollTop = previous[1];
    }
  }
  function render() {
    if (embedded) return;
    mount();
    const tray = document.getElementById("fileCompareTray");
    tray.hidden = !items.length;
    tray.textContent = `查看比較 · ${items.length} / ${limit}`;
    document.getElementById("fileComparePatient").textContent = (patientName || "姓名未提供") + " · 病歷號 " + mrn;
    document.getElementById("fileCompareCount").textContent = `${items.length} / ${limit} 份`;
    const tabs = document.getElementById("fileCompareTabs"), panes = document.getElementById("fileComparePanes");
    tabs.replaceChildren();
    changed();
    if (!document.getElementById("fileCompareDialog").open) {
      if (!items.length) panes.replaceChildren();
      sendState(linkedFrame); return;
    }
    const desired = new Set(items.map(item => item.digest));
    for (const existing of [...panes.children]) if (!desired.has(existing.dataset.digest)) existing.remove();
    for (const [index, item] of items.entries()) {
      const label = ClinicalUI.comparisonName(item, items);
      const tab = button(label, () => {activeIndex = index; render();});
      tab.setAttribute("aria-pressed", String(index === activeIndex));
      tabs.append(tab);
      let current = [...panes.children].find(element => element.dataset.digest === item.digest);
      if (!current) {current = pane(item, index); panes.append(current);}
      const name = current.querySelector(".file-compare-pane-head > strong");
      name.textContent = label;name.title = [label, item.name, item.source].filter(Boolean).join(" · ");
      current.dataset.active = String(index === activeIndex);
      current.style.order = String(index);
      const columns = items.length <= 2 ? items.length : items.length === 4 ? 2 : 3;
      current.style.setProperty("--pane-span", String(items.length === 5 && index >= 3 ? 3 : 6 / columns));
      const buttons = current.querySelectorAll(".file-compare-pane-actions button");
      buttons[0].disabled = index === 0;
      buttons[1].disabled = index === items.length - 1;
      buttons[4].setAttribute("aria-label", `移除 ${label}`);
      const file = current.querySelector(".file-compare-scroll > :first-child");
      file.title = name.title;
      if (file.tagName === "IMG") file.alt = name.title;
      const download = current.querySelector("a.attachment-control");
      const downloadName = [label,item.name].filter(Boolean).join(" ");
      if (download.download !== ClinicalUI.fileName(downloadName, item.mime))
        download.replaceWith(ClinicalUI.downloadLink(download.href, downloadName, item.mime, true));
      file.style.width = (item.zoom * 100) + "%";
      file.style.height = item.mime === "application/pdf" ? (item.zoom * 100) + "%" : "auto";
      current.querySelector(".file-compare-pane-actions span").textContent = Math.round(item.zoom * 100) + "%";
    }
    sendState(linkedFrame);
  }
  if (embedded) window.addEventListener("message", event => {
    if (event.origin !== location.origin || event.source !== window.parent || event.data?.type !== "bot:compare-state") return;
    if (event.data.account !== account || event.data.mrn !== mrn) return;
    remoteItems = (Array.isArray(event.data.items) ? event.data.items : []).slice(0, limit)
      .filter(item => valid({...item, account, mrn}))
      .map(item => ({...item, account, zoom:Math.max(.5, Math.min(3, Number(item.zoom) || 1))}));
    remoteCount = remoteItems.length;
    remoteDigests = new Set(remoteItems.map(item => item.digest));
    renderInline();
    changed();
    if (event.data.error) announce(event.data.error);
  });
  document.addEventListener("DOMContentLoaded", mount);
  function clear(){items=[];remoteItems=[];remoteDigests.clear();remoteCount=0;numericKey="";document.getElementById("fileCompareDialog")?.close();render();changed();}
  return {add, context, count, has, remove:removeDigest, open, receive, renderInline, clear, patient:()=>mrn};
})();
