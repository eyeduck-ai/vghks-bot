"use strict";

// The parent workbench owns the selection; embedded tools send only verified
// account-scoped attachment digests and display metadata.
window.FileCompare = (() => {
  const limit = 6;
  let account = "", mrn = "", patientName = "", items = [], activeIndex = 0, remoteCount = 0, feedbackTimer = 0, linkedFrame = null;
  let remoteDigests = new Set(), remoteItems = [], inlineHost = null, inlineActive = 0, inlineActiveDigest = "";
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
    items.push({...value, zoom:1});
    activeIndex = items.length - 1;
    render();
    announce(`已加入比較清單（${items.length} / ${limit}）。`);
    return true;
  }
  function sendState(frame, error = "") {
    frame?.contentWindow?.postMessage({type:"bot:compare-state", account, mrn, count:items.length,
      digests:items.map(item => item.digest),
      items:items.map(({digest, mime, name, source, date, zoom}) => ({digest, mime, name, source, date, zoom})),
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
    heading.append(titleGroup, count, button("關閉", () => dialog.close()));
    dialog.setAttribute("aria-describedby", "fileComparePatient");
    const tabs = make("div", undefined, "file-compare-tabs");
    tabs.id = "fileCompareTabs";
    tabs.setAttribute("role", "tablist");
    const panes = make("div", undefined, "file-compare-panes");
    panes.id = "fileComparePanes";
    dialog.append(heading, tabs, panes);
    dialog.addEventListener("click", event => {if (event.target === dialog) dialog.close();});
    dialog.addEventListener("close", render);
    document.body.append(tray, feedback, dialog);
  }
  function open() {
    if (embedded) {window.parent.postMessage({type:"bot:compare-open"}, location.origin); return;}
    mount();
    if (!items.length) {announce("請先加入要比較的檔案。"); return;}
    if (linkedFrame && !document.getElementById("modulePage")?.hidden &&
        new URL(linkedFrame.src, location.href).searchParams.get("module") === "cataract") {
      linkedFrame.contentWindow.postMessage({type:"bot:compare-inline-open", account, mrn}, location.origin);
      return;
    }
    const dialog = document.getElementById("fileCompareDialog");
    if (!dialog.open) dialog.showModal();
    render();
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
    const name = make("strong", item.name || "檢查附件");
    const caption = make("span", [item.source, item.date || "日期未提供"].filter(Boolean).join(" · "));
    const buttons = make("div", undefined, "file-compare-pane-actions");
    const moveItem = delta => inline
      ? window.parent.postMessage({type:"bot:compare-move", account, mrn, digest:item.digest, delta}, location.origin)
      : move(index, delta);
    const zoomItem = delta => inline
      ? window.parent.postMessage({type:"bot:compare-zoom", account, mrn, digest:item.digest, delta}, location.origin)
      : (item.zoom = Math.max(.5, Math.min(3, item.zoom + delta)), render());
    buttons.append(button("←", () => moveItem(-1)), button("→", () => moveItem(1)),
      button("−", () => zoomItem(-.25)),
      make("span", Math.round(item.zoom * 100) + "%"),
      button("＋", () => zoomItem(.25)),
      button("移除", () => inline ? removeDigest(item.digest) : remove(index)));
    for (const [i, el] of [...buttons.children].entries()) if (i < 2) el.setAttribute("aria-label", i ? "將檔案向右移" : "將檔案向左移");
    buttons.children[2].setAttribute("aria-label", "縮小檔案");
    buttons.children[4].setAttribute("aria-label", "放大檔案");
    buttons.children[0].disabled = index === 0;
    buttons.children[1].disabled = index === collection.length - 1;
    header.append(name, caption, buttons);
    const scroll = make("div", undefined, "file-compare-scroll");
    const url = location.pathname === "/tools" && !new URLSearchParams(location.search).has("account")
      ? `/api/analysis/asset?id=${item.digest}` : `/api/accounts/${item.account}/analysis/asset?id=${item.digest}`;
    const documentNode = make(item.mime === "application/pdf" ? "iframe" : "img");
    documentNode.src = url;
    documentNode.title = item.name || "檢查附件";
    if (documentNode.tagName === "IMG") documentNode.alt = documentNode.title;
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
      const tab = button(item.name || `檔案 ${index + 1}`, () => {
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
    tabs.replaceChildren(); panes.replaceChildren();
    changed();
    if (!document.getElementById("fileCompareDialog").open) {sendState(linkedFrame); return;}
    for (const [index, item] of items.entries()) {
      const tab = button(item.name || `檔案 ${index + 1}`, () => {activeIndex = index; render();});
      tab.setAttribute("aria-pressed", String(index === activeIndex));
      tabs.append(tab);
      panes.append(pane(item, index));
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
  return {add, context, count, has, remove:removeDigest, open, receive, renderInline};
})();
