"use strict";

// The parent workbench owns the selection; embedded tools send only verified
// account-scoped attachment digests and display metadata.
window.FileCompare = (() => {
  const limit = 6;
  let account = "", mrn = "", patientName = "", items = [], activeIndex = 0, remoteCount = 0, feedbackTimer = 0, linkedFrame = null;
  let remoteDigests = new Set(), remoteItems = [], inlineHost = null, inlineActive = 0, inlineActiveDigest = "";
  let numericKey = "", clearing = null, selectionRevision = 0, compareTrigger = null;
  let trayNode = null, trayGroupNode = null, feedbackNode = null;
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
  const imageResize = typeof ResizeObserver === "function" ? new ResizeObserver(changes => {
    for (const {target} of changes) refreshImage(target.parentElement);
  }) : null;
  if (!imageResize) window.addEventListener("resize", () => {
    for (const current of document.querySelectorAll(".file-compare-pane")) refreshImage(current);
  });
  function releasePanes(host) {
    for (const current of host?.querySelectorAll(".file-compare-pane") || [])
      imageResize?.unobserve?.(current.querySelector(".file-compare-scroll"));
  }
  function clearPanes(host) {releasePanes(host);host?.replaceChildren();}
  function removePane(current) {
    imageResize?.unobserve?.(current.querySelector(".file-compare-scroll"));current.remove();
  }
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
    compareTrigger = null;
    selectionRevision++;
    items = [];
    activeIndex = 0;
    numericKey = "";
    document.getElementById("fileCompareNumeric")?.replaceChildren();
    clearPanes(document.getElementById("fileComparePanes"));
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
    const feedback = feedbackNode;
    if (!feedback) return;
    feedback.textContent = value;
    feedback.hidden = false;
    clearTimeout(feedbackTimer);
    feedbackTimer = setTimeout(() => {feedback.hidden = true;window.UtilityDock?.sync();}, 3500);
    window.UtilityDock?.sync();
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
    items.push({...value, zoom:1, fit:"width", selectionOrder:Math.max(0, ...items.map(item => item.selectionOrder)) + 1});
    selectionRevision++;
    activeIndex = items.length - 1;
    render();
    announce(`已加入比較清單（${items.length} / ${limit}）。`);
    return true;
  }
  function sendState(frame, error = "") {
    frame?.contentWindow?.postMessage({type:"bot:compare-state", account, mrn, count:items.length,
      digests:items.map(item => item.digest),
      items:items.map(({digest, mime, name, source, date, zoom, fit, fileIndex, fileCount, selectionOrder, reportId}) =>
        ({digest, mime, name, source, date, zoom, fit, fileIndex, fileCount, selectionOrder, reportId})),
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
    } else if (event.data?.type === "bot:compare-clear") {
      if (event.data.account !== activeAccount || event.data.mrn !== mrn) return;
      void requestClear();
    } else if (event.data?.type === "bot:compare-move") {
      if (event.data.account !== activeAccount || event.data.mrn !== mrn) return;
      const index = items.findIndex(item => item.digest === event.data.digest);
      if (index >= 0 && [-1, 1].includes(event.data.delta)) move(index, event.data.delta);
    } else if (event.data?.type === "bot:compare-zoom") {
      if (event.data.account !== activeAccount || event.data.mrn !== mrn) return;
      if (event.data.reset === true) zoom(event.data.digest, 0, true);
      else if (["width", "height"].includes(event.data.fit)) zoom(event.data.digest, 0, false, event.data.fit);
      else if ([-.25, .25].includes(event.data.delta)) {
        const scale = Number.isFinite(event.data.scale) && event.data.scale > 0 ? event.data.scale : null;
        zoom(event.data.digest, event.data.delta, false, "", scale);
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
    trayNode = tray;
    tray.id = "fileCompareTray";
    tray.type = "button";
    tray.hidden = true;
    tray.addEventListener("click", open);
    const trayGroup = make("div", undefined, "compare-tray-group");
    trayGroupNode = trayGroup;
    trayGroup.id = "fileCompareTrayGroup";trayGroup.hidden = true;
    const clearSelection = button("×", () => {void requestClear();});
    clearSelection.id = "fileCompareClear";clearSelection.className = "compare-clear";
    clearSelection.setAttribute("aria-label", "清除比較");clearSelection.title = "清除比較";
    const clearLabel = make("span", "清除比較", "compare-clear-label");clearSelection.append(clearLabel);
    trayGroup.append(tray, clearSelection);
    const feedback = make("div", "", "compare-feedback");
    feedbackNode = feedback;
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
    const clearAll = button("清除比較", () => {void requestClear();});
    clearAll.id = "fileCompareClearAll";
    heading.append(titleGroup, count, numericToggle, clearAll, close);
    dialog.setAttribute("aria-describedby", "fileComparePatient");
    const tabs = make("div", undefined, "file-compare-tabs");
    tabs.id = "fileCompareTabs";
    tabs.setAttribute("role", "group");
    tabs.setAttribute("aria-label", "比較檔案切換");
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
    dialog.addEventListener("close", () => {
      numericKey = "";render();
      const trigger = compareTrigger;compareTrigger = null;
      // Moving the dock back into an iframe/reader can interrupt native focus restoration.
      if (trigger?.isConnected && !trigger.disabled && !trigger.hidden &&
          (!trigger.getClientRects || trigger.getClientRects().length)) trigger.focus({preventScroll:true});
    });
    const confirmation = make("dialog", undefined, "compare-clear-dialog");
    confirmation.id = "fileCompareClearDialog";confirmation.dataset.utilityDock = "hide";
    confirmation.setAttribute("aria-labelledby", "fileCompareClearTitle");
    confirmation.setAttribute("aria-describedby", "fileCompareClearMessage");
    const form = make("form");form.method = "dialog";
    const clearTitle = make("h2", "清除比較");clearTitle.id = "fileCompareClearTitle";
    const clearMessage = make("p");clearMessage.id = "fileCompareClearMessage";
    const actions = make("div", undefined, "actions");
    const cancel = make("button", "取消");cancel.type = "submit";cancel.value = "cancel";cancel.autofocus = true;
    const confirm = make("button", "清除比較", "danger");confirm.type = "submit";confirm.value = "clear";
    actions.append(cancel, confirm);form.append(clearTitle, clearMessage, actions);confirmation.append(form);
    document.body.append(trayGroup, feedback, dialog, confirmation);
    if(window.UtilityDock){window.UtilityDock.attach("comparison",trayGroup,20);window.UtilityDock.attach("feedback",feedback,30);}
  }
  function requestClear() {
    if (embedded) {window.parent.postMessage({type:"bot:compare-clear",account,mrn},location.origin);return Promise.resolve(false);}
    if (clearing) return clearing;
    mount();
    if (!items.length) return Promise.resolve(false);
    const selectedAccount = account, selectedMrn = mrn, revision = selectionRevision;
    const prompt = document.getElementById("fileCompareClearDialog");
    document.getElementById("fileCompareClearMessage").textContent = "清除 " + items.length + " 份比較檔案？只會移出比較佇列，已保存的報告與附件會保留。";
    prompt.returnValue = "";
    clearing = new Promise(resolve => {
      prompt.addEventListener("close", () => {
        const confirmed = prompt.returnValue === "clear";prompt.returnValue = "";clearing = null;
        const same = account === selectedAccount && mrn === selectedMrn && selectionRevision === revision;
        if (confirmed && same) {clear();announce("已清除比較佇列，已保存附件仍保留。");}
        else if (confirmed) announce("比較清單已更新，請重新確認清除。");
        window.UtilityDock?.sync();resolve(confirmed && same);
      }, {once:true});
    });
    prompt.showModal();window.UtilityDock?.sync();return clearing;
  }
  function open(event) {
    if (embedded) {window.parent.postMessage({type:"bot:compare-open"}, location.origin); return;}
    mount();
    if (!items.length) {announce("請先加入要比較的檔案。"); return;}
    const dialog = document.getElementById("fileCompareDialog");
    if (!dialog.open) {
      compareTrigger = event?.currentTarget || trayNode?.ownerDocument?.activeElement || document.activeElement;
      dialog.showModal();
    }
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
    selectionRevision++;
    if (activeIndex === index) activeIndex = other;
    else if (activeIndex === other) activeIndex = index;
    render();
  }
  function remove(index) {
    items.splice(index, 1);
    selectionRevision++;
    activeIndex = Math.min(activeIndex, Math.max(0, items.length - 1));
    if (!items.length) document.getElementById("fileCompareDialog")?.close();
    render();
  }
  function zoom(digest, delta, reset = false, fit = "", sourceScale = null) {
    const item = (embedded ? remoteItems : items).find(row => row.digest === digest);
    // The native PDF viewer owns document zoom; resizing its iframe does not.
    if (!item || item.mime === "application/pdf") return;
    const host = embedded ? inlineHost : document.getElementById("fileComparePanes");
    const current = [...(host?.querySelectorAll(".file-compare-pane") || [])].find(node => node.dataset.digest === digest);
    const scale = sourceScale ?? (Number(current?.querySelector(".file-compare-scroll > :first-child")?.dataset.zoom) || item.zoom);
    if (reset || fit) {
      const scroll = current?.querySelector(".file-compare-scroll");
      if (scroll) scroll.scrollLeft = scroll.scrollTop = 0;
    }
    if (embedded) {
      window.parent.postMessage({type:"bot:compare-zoom", account, mrn, digest,
        ...(reset ? {reset:true} : fit ? {fit} : {delta, scale})}, location.origin);
      return;
    }
    item.fit = fit;
    if (reset) item.zoom = 1;
    else if (!fit) {
      const small = scale < .25 || scale === .25 && delta < 0;
      const next = small ? delta < 0 ? scale * .8 : Math.min(.25, scale * 1.25)
        : (delta > 0 ? Math.floor(scale * 4 + 1e-9) + 1 : Math.ceil(scale * 4 - 1e-9) - 1) / 4;
      item.zoom = Math.max(Math.min(.01, scale / 2), Math.min(4, next));
    }
    render();
  }
  function applyImageZoom(current, item) {
    const file = current.querySelector(".file-compare-scroll > :first-child"), scroll = current.querySelector(".file-compare-scroll");
    if (!file || !scroll) return;
    const ready = !!(file.naturalWidth && file.naturalHeight && scroll.clientWidth && scroll.clientHeight);
    let scale = item.fit ? 1 : item.zoom;
    if (ready && item.fit === "width") scale = scroll.clientWidth / file.naturalWidth;
    if (ready && item.fit === "height") scale = scroll.clientHeight / file.naturalHeight;
    file.style.width = ready ? Math.floor(file.naturalWidth * scale * 100) / 100 + "px" : scale * 100 + "%";
    file.style.height = "auto";file.dataset.zoom = String(scale);
    const reset = current.querySelector(".file-compare-zoom");
    const percent = Math.round(scale * 100) || +(scale * 100).toPrecision(2);
    reset.textContent = percent + "%";
    reset.setAttribute("aria-label", `目前縮放 ${percent}%，重設縮放為 100%`);
    for (const control of current.querySelectorAll(".file-compare-scale-step")) control.disabled = !ready;
    for (const control of current.querySelectorAll(".file-compare-fit")) {
      control.disabled = !ready;control.setAttribute("aria-pressed", String(item.fit === control.dataset.fit));
    }
  }
  function refreshImage(current) {
    if (!current) return;
    const item = (current.dataset.inline === "true" ? remoteItems : items).find(row => row.digest === current.dataset.digest);
    if (item && item.mime !== "application/pdf") applyImageZoom(current, item);
  }
  function pane(item, index, inline = false) {
    const collection = inline ? remoteItems : items;
    const pdf = item.mime === "application/pdf";
    const wrap = make("section", undefined, "file-compare-pane");
    wrap.dataset.digest = item.digest;
    wrap.dataset.inline = String(inline);
    wrap.dataset.active = String(index === (inline ? inlineActive : activeIndex));
    const columns = collection.length <= 2 ? collection.length : collection.length === 4 ? 2 : 3;
    wrap.style.setProperty("--pane-span", String(collection.length === 5 && index >= 3 ? 3 : 6 / columns));
    const header = make("div", undefined, "file-compare-pane-head");
    const label = ClinicalUI.comparisonName(item, collection);
    const name = make("strong", label);
    name.title = [label, item.name, item.source].filter(Boolean).join(" · ");
    const caption = make("span", [item.name, item.source].filter(Boolean).join(" · "));
    const buttons = make("div", undefined, "file-compare-pane-actions");
    const itemIndex = () => (inline ? remoteItems : items).findIndex(row => row.digest === item.digest);
    const moveItem = delta => inline
      ? window.parent.postMessage({type:"bot:compare-move", account, mrn, digest:item.digest, delta}, location.origin)
      : move(itemIndex(), delta);
    const left = button("←", () => moveItem(-1)), right = button("→", () => moveItem(1));
    left.setAttribute("aria-label", "將檔案向左移");right.setAttribute("aria-label", "將檔案向右移");
    buttons.append(left, right);
    if (!pdf) {
      const smaller = button("−", () => zoom(item.digest, -.25)), larger = button("＋", () => zoom(item.digest, .25));
      const resetZoom = button(Math.round(item.zoom * 100) + "%", () => zoom(item.digest, 0, true));
      resetZoom.className = "file-compare-zoom";
      resetZoom.setAttribute("aria-label", "重設縮放為 100%");resetZoom.title = "點擊重設為 100%";
      smaller.setAttribute("aria-label", "縮小檔案");larger.setAttribute("aria-label", "放大檔案");
      smaller.className = larger.className = "file-compare-scale-step";
      buttons.append(smaller, resetZoom, larger);
      for (const [mode, label] of [["width", "最適寬度"], ["height", "最適高度"]]) {
        const fit = button(label, () => zoom(item.digest, 0, false, mode));
        fit.className = "file-compare-fit";fit.dataset.fit = mode;buttons.append(fit);
      }
    }
    const removeFile = button("×", () => inline ? removeDigest(item.digest) : remove(itemIndex()));
    removeFile.setAttribute("aria-label", `移除 ${label}`);removeFile.className = "file-compare-remove";removeFile.title = "移除檔案";
    buttons.append(removeFile);
    buttons.children[0].disabled = index === 0;
    buttons.children[1].disabled = index === collection.length - 1;
    header.append(name, caption, buttons);
    const scroll = make("div", undefined, "file-compare-scroll");
    const path = location.pathname === "/tools" && !new URLSearchParams(location.search).has("account")
      ? `/api/analysis/asset?id=${item.digest}` : `/api/accounts/${item.account}/analysis/asset?id=${item.digest}`;
    const url = window.LocalSession.resourceURL(path);
    const documentNode = make(pdf ? "iframe" : "img");
    documentNode.src = pdf ? ClinicalUI.pdfURL(url) : url;
    documentNode.title = name.title;
    if (documentNode.tagName === "IMG") documentNode.alt = documentNode.title;
    buttons.append(ClinicalUI.assetTools({url,asset:item,name:[label,item.name].filter(Boolean).join(" "),image:documentNode,compact:true}));
    documentNode.style.width = pdf ? "100%" : (item.zoom * 100) + "%";
    documentNode.style.height = pdf ? "100%" : "auto";
    scroll.append(documentNode);
    wrap.append(header);
    if (pdf) wrap.append(make("p", "PDF 縮放請使用檢閱器內的比例調整。", "file-compare-pdf-hint"));
    wrap.append(scroll);
    if (!pdf) {
      documentNode.addEventListener("load", () => refreshImage(wrap));
      imageResize?.observe(scroll);
    }
    return wrap;
  }
  function updatePane(current, item, index, collection, active) {
    const label = ClinicalUI.comparisonName(item, collection);
    const name = current.querySelector(".file-compare-pane-head > strong");
    name.textContent = label;name.title = [label, item.name, item.source].filter(Boolean).join(" · ");
    current.dataset.active = String(index === active);
    current.style.order = String(index);
    const columns = collection.length <= 2 ? collection.length : collection.length === 4 ? 2 : 3;
    current.style.setProperty("--pane-span", String(collection.length === 5 && index >= 3 ? 3 : 6 / columns));
    const buttons = current.querySelectorAll(".file-compare-pane-actions button");
    buttons[0].disabled = index === 0;buttons[1].disabled = index === collection.length - 1;
    current.querySelector(".file-compare-remove").setAttribute("aria-label", `移除 ${label}`);
    const file = current.querySelector(".file-compare-scroll > :first-child");
    file.title = name.title;if (file.tagName === "IMG") file.alt = name.title;
    const download = current.querySelector("a.attachment-control");
    const downloadName = [label,item.name].filter(Boolean).join(" ");
    if (download.download !== ClinicalUI.fileName(downloadName, item.mime))
      download.replaceWith(ClinicalUI.downloadLink(download.href, downloadName, item.mime, true));
    const pdf = item.mime === "application/pdf";
    if (pdf) {file.style.width = "100%";file.style.height = "100%";}
    else applyImageZoom(current, item);
  }
  function updateSwitches(host, collection, active, activate) {
    const focused = host.ownerDocument.activeElement;
    const restoreFocus = host.contains(focused);
    const existing = new Map([...host.children].map(control => [control.dataset.digest, control]));
    for (const [index, item] of collection.entries()) {
      const label = ClinicalUI.comparisonName(item, collection);
      const control = existing.get(item.digest) || button(label, () => activate(item.digest));
      control.dataset.digest = item.digest;
      if (control.textContent !== label) control.textContent = label;
      control.setAttribute("aria-pressed", String(index === active));
      const before = host.children[index] || null;
      if (control !== before) {
        if (control.parentElement === host && host.moveBefore) host.moveBefore(control, before);
        else host.insertBefore(control, before);
      }
      existing.delete(item.digest);
    }
    for (const control of existing.values()) control.remove();
    if (restoreFocus && host.ownerDocument.activeElement !== focused) {
      const target = host.contains(focused) ? focused : host.children[active];
      target?.focus({preventScroll:true});
    }
  }
  function renderInline(host = inlineHost) {
    if (!embedded || !host) return;
    if (inlineHost && inlineHost !== host) releasePanes(inlineHost);
    inlineHost = host;
    if (!remoteItems.length) {
      clearPanes(host);
      host.append(make("p", "尚未選取檔案。請到「數值與醫囑」點開報告，再勾選附件加入比較。", "empty-result"));
      return;
    }
    const remembered = remoteItems.findIndex(item => item.digest === inlineActiveDigest);
    inlineActive = remembered >= 0 ? remembered : Math.min(inlineActive, remoteItems.length - 1);
    inlineActiveDigest = remoteItems[inlineActive].digest;
    let tabs = host.querySelector(".file-compare-inline-tabs"), panes = host.querySelector(".file-compare-inline-panes");
    if (!tabs || !panes) {
      releasePanes(host);
      tabs = make("div", undefined, "file-compare-inline-tabs");panes = make("div", undefined, "file-compare-inline-panes");
      tabs.setAttribute("role", "group");
      tabs.setAttribute("aria-label", "比較檔案切換");
      host.replaceChildren(tabs, panes);
    }
    updateSwitches(tabs, remoteItems, inlineActive, digest => {
      inlineActiveDigest = digest;
      renderInline();
    });
    const desired = new Set(remoteItems.map(item => item.digest));
    for (const existing of [...panes.children]) if (!desired.has(existing.dataset.digest)) removePane(existing);
    for (const [index, item] of remoteItems.entries()) {
      let current = [...panes.children].find(node => node.dataset.digest === item.digest);
      if (!current) {current = pane(item, index, true);panes.append(current);}
      updatePane(current, item, index, remoteItems, inlineActive);
    }
  }
  function render() {
    if (embedded) return;
    mount();
    const tray = trayNode;
    tray.hidden = !items.length;
    tray.textContent = `查看比較 · ${items.length} / ${limit}`;
    trayGroupNode.hidden = !items.length;
    document.getElementById("fileCompareClearAll").disabled = !items.length;
    document.getElementById("fileComparePatient").textContent = (patientName || "姓名未提供") + " · 病歷號 " + mrn;
    document.getElementById("fileCompareCount").textContent = `${items.length} / ${limit} 份`;
    const tabs = document.getElementById("fileCompareTabs"), panes = document.getElementById("fileComparePanes");
    changed();
    window.UtilityDock?.sync();
    const desired = new Set(items.map(item => item.digest));
    for (const existing of [...panes.children]) if (!desired.has(existing.dataset.digest)) removePane(existing);
    if (!document.getElementById("fileCompareDialog").open) {
      tabs.replaceChildren();
      if (!items.length) clearPanes(panes);
      sendState(linkedFrame); return;
    }
    updateSwitches(tabs, items, activeIndex, digest => {
      const index = items.findIndex(item => item.digest === digest);
      if (index >= 0) {activeIndex = index; render();}
    });
    for (const [index, item] of items.entries()) {
      let current = [...panes.children].find(element => element.dataset.digest === item.digest);
      if (!current) {current = pane(item, index); panes.append(current);}
      updatePane(current, item, index, items, activeIndex);
    }
    sendState(linkedFrame);
  }
  if (embedded) window.addEventListener("message", event => {
    if (event.origin !== location.origin || event.source !== window.parent || event.data?.type !== "bot:compare-state") return;
    if (event.data.account !== account || event.data.mrn !== mrn) return;
    remoteItems = (Array.isArray(event.data.items) ? event.data.items : []).slice(0, limit)
      .filter(item => valid({...item, account, mrn}))
      .map(item => ({...item, account, zoom:Math.max(.000001, Math.min(4, Number(item.zoom) || 1)),
        fit:["width", "height"].includes(item.fit) ? item.fit : ""}));
    remoteCount = remoteItems.length;
    remoteDigests = new Set(remoteItems.map(item => item.digest));
    renderInline();
    changed();
    if (event.data.error) announce(event.data.error);
  });
  document.addEventListener("DOMContentLoaded", mount);
  function clear(){selectionRevision++;items=[];remoteItems=[];remoteDigests.clear();remoteCount=0;numericKey="";document.getElementById("fileCompareDialog")?.close();render();if(embedded){renderInline();changed();}}
  return {add, context, count, has, remove:removeDigest, open, receive, renderInline, clear, requestClear, patient:()=>mrn};
})();
