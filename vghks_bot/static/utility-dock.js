"use strict";
// One ordinary DOM container follows the active modal; dialogs themselves never move.
window.UtilityDock = (() => {
  const embedded = window.parent !== window;
  const documents = new Map(), slots = new Map(), reservations = new Set();
  let dock = null, owner = "", previousHost = null, resize = null;
  function register(doc) {
    if (!doc?.body || documents.has(doc)) return;
    const state = {stack:[...doc.querySelectorAll("dialog[open]")]};
    documents.set(doc, state);
    if (typeof MutationObserver === "function") {
      state.observer = new MutationObserver(records => {
        for (const record of records) {
          const target = record.target;
          if (target.tagName !== "DIALOG") continue;
          state.stack = state.stack.filter(item => item !== target);
          if (target.open) state.stack.push(target);
        }
        sync();
      });
      state.observer.observe(doc.body, {attributes:true,subtree:true,attributeFilter:["open"]});
    }
  }
  function top(doc) {
    register(doc);
    const state = documents.get(doc);
    if (!state) return null;
    state.stack = state.stack.filter(item => item.open && item.isConnected);
    for (const item of doc.querySelectorAll("dialog[open]")) if (!state.stack.includes(item)) state.stack.push(item);
    return state.stack.at(-1) || null;
  }
  function host() {
    const modal = top(document);
    if (modal) return modal;
    for (const frame of document.querySelectorAll("#moduleFrame,#cataractFrame")) {
      if (frame.hidden || !frame.getClientRects().length || frame.dataset.account !== owner) continue;
      try {const inner = top(frame.contentDocument);if (inner) return inner;} catch (_) { /* Cross-origin frames cannot host clinical controls. */ }
    }
    return document.body;
  }
  function reserve() {
    if (!dock || !previousHost) return;
    const height = dock.hidden ? 0 : Math.ceil(dock.getBoundingClientRect().height);
    previousHost.style.setProperty("--utility-dock-height", height + "px");
  }
  function cleanDocuments() {
    const live = new Set([document]);
    for (const frame of document.querySelectorAll("#moduleFrame,#cataractFrame")) {
      try {if (frame.contentDocument?.body) live.add(frame.contentDocument);} catch (_) { /* Ignore foreign documents. */ }
    }
    for (const [doc,state] of documents) if (!live.has(doc)) {
      state.observer?.disconnect();documents.delete(doc);
      for (const reserved of reservations) if (reserved.ownerDocument === doc) {
        reserved.classList.remove("utility-dock-host", "utility-dock-active");reserved.style.removeProperty("--utility-dock-height");reservations.delete(reserved);
      }
    }
  }
  function sync() {
    if (embedded || !document.body) return;
    cleanDocuments();
    if (!dock) {
      dock = document.createElement("aside");dock.id = "utilityDock";dock.className = "utility-dock";
      dock.setAttribute("aria-label", "任務進度與檔案比較");
      if (typeof ResizeObserver === "function") {resize = new ResizeObserver(reserve);resize.observe(dock);}
    }
    const target = host();
    for (const reserved of reservations) {
      if (reserved !== target && (!reserved.isConnected || (reserved.tagName === "DIALOG" && !reserved.open))) {
        reserved.classList.remove("utility-dock-host", "utility-dock-active");reserved.style.removeProperty("--utility-dock-height");reservations.delete(reserved);
      }
    }
    const confirmation = target.dataset?.utilityDock === "hide" || target.id === "confirmDialog" || target.id === "sheetManagementFinalizeDialog";
    if (previousHost !== target) {
      // Keep the underlying open reader's geometry while comparison/confirmation is on top.
      previousHost?.classList.remove("utility-dock-active");
      if (previousHost && !previousHost.isConnected) {
        previousHost.classList.remove("utility-dock-host");previousHost.style.removeProperty("--utility-dock-height");reservations.delete(previousHost);
      }
      if (target.moveBefore && dock.isConnected && target.ownerDocument === dock.ownerDocument) target.moveBefore(dock, null);
      else target.append(dock);
      previousHost = target;
    }
    // A modal footer remains in its native focus scope and consumes real layout
    // space. Fixed viewport positioning would still cover the report images.
    dock.classList.toggle("utility-dock-modal", target.tagName === "DIALOG");
    dock.style.removeProperty("right");dock.style.removeProperty("bottom");dock.style.removeProperty("max-width");
    for (const {node,order} of slots.values()) {
      node.style.order = String(order);
      if (node.parentElement !== dock) dock.append(node);
    }
    dock.hidden = confirmation || ![...slots.values()].some(({node}) => !node.hidden);
    target.classList.toggle("utility-dock-host", !dock.hidden);
    target.classList.toggle("utility-dock-active", !dock.hidden);
    if (dock.hidden) reservations.delete(target);else reservations.add(target);
    reserve();
  }
  function attach(name, node, order = 0) {if (embedded) return;slots.set(name,{node,order});sync();}
  function context(account) {owner = account || "";sync();}
  document.addEventListener("DOMContentLoaded", sync);
  window.addEventListener("resize", sync);
  window.addEventListener("filecomparechange", sync);
  return {attach,context,sync};
})();
