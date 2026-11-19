"use strict";

const assert = require("node:assert/strict");
const {surface, tick, deferred} = require("./review_ui.js");

function setup(request, options = {}) {
  const ui = surface(), timers = new Map(), editors = [];
  const prototype = Object.getPrototypeOf(ui.node("select"));
  const append = prototype.append;
  prototype.append = function (...nodes) {
    for (const node of nodes) node.remove();
    append.call(this, ...nodes);
  };
  Object.defineProperty(prototype, "options", {get() { return this.children; }});
  ui.context.document.body = ui.$("#body");
  let sequence = 0, workspace;
  Object.assign(ui.context, {
    el(tag, text, className) {
      const node = ui.node(tag, text, className);
      if (tag === "select") node.value = "selected";
      return node;
    },
    button(text, run, className) {
      const node = ui.node("button", text, className);
      node.addEventListener("click", run);
      return node;
    },
    setTimeout(run) { timers.set(++sequence, run); return sequence; },
    clearTimeout(id) { timers.delete(id); }
  });
  ui.context.window.ClinicalUI = {pdfURL: (path, page) => `${path}#page=${page}`};
  ui.context.window.CataractBiometryReview = {
    create(options) {
      const editor = {...options, element: ui.node("section", "Editable fields")};
      editor.setDocument = index => { editor.documentIndex = index; };
      editors.push(editor);
      return editor;
    }
  };
  ui.run("vghks_bot/static/cataract-biometry.js");
  const control = ui.context.window.CataractBiometry.create({account: "test-account", cohortId: "test-cohort",
    mrn: "TEST001", source: "orders", request, range: () => ({start: "2026-01-01", end: "2026-06-30"}),
    mountWorkspace(node) {
      workspace = node;
      ui.$("#workbench").append(node);
      return () => node.remove();
    }, assetURL: digest => `/asset?id=${digest}`, ...options});
  ui.$("#workbench").append(control.element);
  const findButton = label => [...control.element.querySelectorAll("button"), ...(workspace?.querySelectorAll("button") || [])]
    .find(node => node.textContent === label);
  const switchMode = async value => {workspace.querySelector("select").value = value; await workspace.querySelector("select").emit("change");};
  return {control, editors, get workspace() {return workspace;}, findButton, switchMode, ui, timers,
    async poll() {
      const pending = [...timers.values()];
      timers.clear();
      for (const run of pending) await run();
    }};
}

const click = async node => { assert.ok(node); await node.emit("click"); await tick(); };
const ready = {state: "idle"};
const docs = [{id: "order-1", digest: "a".repeat(64), date: "2026-01-10", label: "DBR · 檔案 1"},
  {id: "order-2", digest: "b".repeat(64), date: "2026-02-10", label: "DBR · 檔案 2"}];
const exported = {schema_version: 1, source: "orders", category: "iol", documents: [docs[0], {...docs[1], pages: [2, 3]}]};

async function orderSelections() {
  const calls = [];
  const s = setup(async request => {
    calls.push(JSON.parse(JSON.stringify(request)));
    if (request.action === "pages") return {page_count: 3};
    if (request.action === "start") return {job_id: request.mode === "all" ? "job-2" : "job-1", view: "batch", state: "queued", total: 2, processed: 0};
    return calls.some(item => item.action === "start")
      ? {job_id: "job-1", view: "batch", state: "completed", total: 2, processed: 2, result: structuredClone(exported)}
      : ready;
  });
  s.control.setDocuments(docs);
  await click(s.findButton("擷取眼科測量"));
  assert.equal(s.workspace.parentElement, s.ui.$("#workbench"));
  assert.equal(s.workspace.querySelector("dialog"), null);
  const [first, second] = s.workspace.querySelectorAll(".cataract-biometry-document");
  first.querySelector("input").checked = true;
  await first.querySelector("input").emit("change");
  assert.equal(second.querySelector("input").checked, false);
  await click(second.querySelector("button"));
  assert.deepEqual(calls.at(-1), {cohort_id: "test-cohort", mrn: "TEST001", source: "orders", action: "pages",
    start: "2026-01-01", end: "2026-06-30", documents: [{id: docs[1].id, digest: docs[1].digest}]});
  const pageChecks = second.querySelector("fieldset").querySelectorAll("input");
  for (const index of [2, 1]) {pageChecks[index].checked = true; await pageChecks[index].emit("change");}
  s.workspace.querySelectorAll("select")[1].value = "iol";
  await click(s.findButton("開始背景擷取"));
  const selection = calls.at(-1);
  assert.equal(selection.mode, "selected");
  assert.equal(selection.category, "iol");
  assert.equal(selection.view, "batch");
  assert.deepEqual(selection.documents, [{id: docs[0].id, digest: docs[0].digest},
    {id: docs[1].id, digest: docs[1].digest, pages: [2, 3]}]);
  await s.poll();
  assert.equal(calls.at(-1).job_id, "job-1");
  assert.equal(s.workspace.querySelector(".cataract-biometry-selection").hidden, true, "completed results take priority");
  assert.equal(s.workspace.querySelector("textarea"), null, "raw JSON must not be shown");
  assert.equal(s.workspace.querySelector("iframe").src, `/asset?id=${docs[0].digest}#page=1`);
  await click(s.findButton("下一份"));
  assert.equal(s.editors[0].documentIndex, 1);
  assert.equal(s.workspace.querySelector("iframe").src, `/asset?id=${docs[1].digest}#page=2`);
  s.editors[0].onPageChange(3);
  const retainedPDF = s.workspace.querySelector("iframe");
  assert.equal(retainedPDF.src, `/asset?id=${docs[1].digest}#page=3`);
  const draft = s.control.reviewDraft("job-1");
  draft.set("field", "corrected");
  assert.equal(draft, s.editors[0].draft);
  await click(s.findButton("返回上一頁"));
  assert.equal(s.workspace.parentElement, null);
  assert.equal(s.ui.context.document.activeElement, s.findButton("擷取眼科測量"));
  await click(s.findButton("擷取眼科測量"));
  assert.equal(s.editors.length, 1);
  assert.equal(s.workspace.querySelector("iframe"), retainedPDF);
  assert.equal(s.control.reviewDraft("job-1").get("field"), "corrected");
  await click(s.findButton("重新選擇 PDF"));
  await s.switchMode("all");
  await click(s.findButton("開始背景擷取"));
  assert.deepEqual(calls.at(-1), {cohort_id: "test-cohort", mrn: "TEST001", source: "orders", action: "start",
    mode: "all", category: "iol", view: "batch", start: "2026-01-01", end: "2026-06-30"});
  assert.equal(s.control.reviewDraft("job-1"), null);
  assert.equal(s.control.reviewDraft("job-2").size, 0);
}

async function scanScopeAndReset() {
  const calls = [], pending = deferred();
  const s = setup(async request => {
    calls.push(JSON.parse(JSON.stringify(request)));
    if (request.action === "status") return ready;
    return pending.promise;
  }, {source: "scans", range: () => ({})});
  s.control.setDocuments([{id: "scan-1", label: "掃描病歷"}], {allowAll: false});
  await click(s.findButton("擷取眼科測量"));
  const check = s.workspace.querySelector(".cataract-biometry-document").querySelector("input");
  check.checked = true;
  await check.emit("change");
  assert.equal(s.workspace.querySelector("select").options[1].disabled, true);
  await click(s.findButton("開始背景擷取"));
  assert.deepEqual(calls.at(-1), {cohort_id: "test-cohort", mrn: "TEST001", source: "scans", action: "start",
    mode: "selected", category: "all", view: "batch", documents: [{id: "scan-1"}]});
  s.control.clear();
  pending.resolve({job_id: "scan-job", state: "completed", result: exported});
  await tick();
  assert.equal(s.workspace.parentElement, null);
  assert.equal(s.editors.length, 0);
  assert.equal(s.timers.size, 0);
}

async function retryWithoutInvalidatedJobId() {
  const calls = [];
  const s = setup(async request => {
    calls.push(request);
    if (request.action === "start") return {job_id: "previous", state: "queued", total: 1, processed: 0};
    if (request.job_id) throw new Error("找不到此病人的擷取工作。");
    return ready;
  });
  s.control.setDocuments(docs);
  await click(s.findButton("擷取眼科測量"));
  const check = s.workspace.querySelector(".cataract-biometry-document").querySelector("input");
  check.checked = true;
  await check.emit("change");
  await click(s.findButton("開始背景擷取"));
  await s.poll();
  const retry = s.findButton("重讀擷取進度");
  assert.equal(retry.hidden, false);
  await click(retry);
  assert.equal(calls.at(-1).job_id, undefined);
  assert.equal(s.findButton("開始背景擷取").disabled, false);
}

async function scanIdentityAndPolling() {
  const firstDigest = "a".repeat(64), replacement = "c".repeat(64);
  const first = {id: "scan-1", label: "掃描病歷 1"};
  const other = {id: "scan-2", label: "掃描病歷 2", digest: "b".repeat(64)};
  const calls = [];
  const s = setup(async request => {
    calls.push(JSON.parse(JSON.stringify(request)));
    if (request.action === "pages") {
      const digest = request.documents[0].digest;
      return {page_count: digest === replacement ? 1 : 3, digest};
    }
    return request.action === "start" || request.job_id ? {job_id: "scan-job", state: "running", total: 1} : ready;
  }, {source: "scans", range: () => ({}), prepareDocument: doc => ({digest: doc.digest || firstDigest})});
  const rows = () => s.workspace.querySelectorAll(".cataract-biometry-document");
  s.control.setDocuments([first, other]);
  await click(s.findButton("擷取眼科測量"));
  const [firstRow, otherRow] = rows();
  await click(firstRow.querySelector("button"));
  const page = firstRow.querySelector("fieldset").querySelectorAll("input")[2];
  page.checked = true;
  await page.emit("change");
  assert.equal(calls.at(-1).documents[0].digest, firstDigest);
  await click(otherRow.querySelector("button"));
  s.control.setDocuments([{...first, digest: firstDigest}, other]);
  assert.equal(rows()[0], firstRow, "saving the same scan must preserve the open page controls");
  assert.equal(page.checked, true);

  s.control.setDocuments([{...first, digest: replacement}, other]);
  assert.notEqual(rows()[0], firstRow);
  assert.equal(rows()[0].querySelector("input").checked, false, "page selections must not carry over to a replacement PDF");
  assert.equal(rows()[1], otherRow, "refresh must preserve controls for other PDFs");
  assert.ok(otherRow.querySelector("fieldset"));
  assert.equal(s.findButton("開始背景擷取").disabled, true);
  await click(rows()[0].querySelector("button"));
  const replacementPages = rows()[0].querySelector("fieldset").querySelectorAll("input");
  assert.equal(replacementPages.length, 1);
  replacementPages[0].checked = true;
  await replacementPages[0].emit("change");
  await click(s.findButton("開始背景擷取"));
  assert.deepEqual(calls.at(-1).documents, [{id: first.id, digest: replacement, pages: [1]}]);

  const [pollTimer] = s.timers.keys();
  assert.ok(pollTimer);
  s.control.setDocuments([{...first, digest: replacement}, other, {id: "scan-3", label: "掃描病歷 3"}]);
  assert.deepEqual([...s.timers.keys()], [pollTimer], "catalog updates must not postpone a scheduled status check");
  await s.poll();
  assert.equal(calls.at(-1).job_id, "scan-job");
}

async function inlineSinglePdf() {
  const calls = [], state = [], submission = deferred();
  const file = {id: "scan-1", digest: "d".repeat(64)};
  const oldResult = {source: "scans", documents: [{id: "old"}]};
  const newResult = {source: "scans", category: "biometry", documents: [{...file, pages: [2, 4]}]};
  const s = setup(async request => {
    calls.push(JSON.parse(JSON.stringify(request)));
    if (request.action === "pages") return {page_count: 4, digest: file.digest};
    if (request.action === "start") return submission.promise;
    if (request.job_id || calls.some(item => item.action === "start"))
      return {job_id: "latest-job", view: "inline", state: "completed", result: newResult};
    return {job_id: "old-job", view: "batch", state: "completed", result: oldResult};
  }, {source: "scans", range: () => ({})});
  s.control.setDocuments([file, {id: "scan-2"}]);
  const removeListener = s.control.subscribe(values => state.push(values));
  await tick();
  assert.equal(calls[0].action, "status");
  assert.equal(state.at(-1).hasResult, true);
  await click(s.findButton("擷取眼科測量"));
  await click(s.findButton("重新選擇 PDF"));
  const check = s.workspace.querySelectorAll(".cataract-biometry-document")[1].querySelector("input");
  check.checked = true;
  await check.emit("change");
  await click(s.findButton("返回上一頁"));
  const pageInfo = await s.control.readPages(file);
  assert.equal(pageInfo.count, 4);
  assert.equal(pageInfo.digest, file.digest);
  assert.equal(await s.control.readPages(file), pageInfo, "preview page count should use the attachment cache");
  assert.deepEqual(calls.at(-1).documents, [file]);

  const started = s.control.startDocument(file, [4, 2, 2], "biometry");
  assert.equal(s.workspace.parentElement, null, "inline submit must not open the batch workspace");
  assert.deepEqual(calls.at(-1).documents, [{...file, pages: [2, 4]}]);
  assert.equal(calls.at(-1).category, "biometry");
  assert.equal(calls.at(-1).view, "inline");
  assert.equal(await s.control.startDocument(file, [1]), false, "prevent a duplicate extraction during submit");
  assert.equal(state.at(-1).busy, true);
  assert.equal(state.at(-1).hasResult, false);
  submission.resolve({job_id: "latest-job", view: "inline", state: "running"});
  assert.equal(await started, true);
  await s.poll();
  assert.equal(state.at(-1).busy, false);
  assert.equal(state.at(-1).hasResult, true);
  assert.equal(state.at(-1).origin, "inline");
  assert.equal(state.at(-1).jobId, "latest-job");
  assert.deepEqual(state.at(-1).result, newResult);
  assert.equal(check.checked, true, "inline extraction must not overwrite batch selections");
  await click(s.findButton("擷取眼科測量"));
  assert.equal(s.editors.at(-1).draft, s.control.reviewDraft("latest-job"));
  removeListener();
  const emitted = state.length;
  s.control.clear();
  assert.equal(state.length, emitted);
}

async function persistentDocumentDraft() {
  const file = docs[0], replacement = {...file, digest: "c".repeat(64)}, events = [], changed = [], sent = [];
  const original = {...file, pages: [2], capture_id: "saved-1", data: {eyes: {}}};
  const firstSave = deferred();
  const s = setup(async request => {
    if (request.action === "status") return ready;
    if (request.action === "load") return request.documents[0].digest === file.digest ? {
      state: "saved", category: "biometry", document: {...original,
        saved_review: {revision: 2, confirmed: true, edits: [["eyes.OD.al", {raw: "23.62", value: 23.62}]]}}
    } : {state: "changed"};
    if (request.action === "save_review") {
      sent.push(JSON.parse(JSON.stringify(request)));
      if (sent.length === 1) return firstSave.promise;
      return {state: "saved", saved_review: {revision: 4, ...request.review}};
    }
    throw new Error("unexpected request");
  });
  const stop = s.control.subscribeDocument(file, status => events.push(status));
  const stopOther = s.control.subscribeDocument(replacement, status => changed.push(status));
  await tick();
  assert.equal(changed.at(-1).changed, true);
  assert.equal(changed.at(-1).hasResult, false, "replacement PDF must not get the original review");
  assert.equal(events.at(-1).jobId, original.capture_id);
  assert.equal(events.at(-1).saved, true);
  const options = s.control.reviewOptions(events.at(-1).result), draft = options.draft.get(0);
  assert.equal(draft.confirmed, true);
  assert.equal(draft.edits.get("eyes.OD.al").value, 23.62);
  draft.confirmed = false;
  draft.edits.set("eyes.OD.al", {raw: "23."});
  const queuedEdit = options.onDraftChange(0, draft);
  assert.equal(draft.storage, "saving");
  draft.edits.set("eyes.OD.al", {raw: "24.00", value: 24});
  draft.confirmed = true;
  void options.onDraftChange(0, draft);
  assert.equal(sent.length, 1, "the follow-up must wait for the acknowledged revision");
  assert.equal(sent[0].revision, 2);
  assert.deepEqual(sent[0].review, {confirmed: false, edits: [["eyes.OD.al", {raw: "23."}]]});
  stop(); stopOther(); s.control.clear();
  assert.equal(s.ui.context.window.CataractBiometry.hasUnsaved(), true);
  const flush = s.ui.context.window.CataractBiometry.flushAll();
  firstSave.resolve({state: "saved", saved_review: {revision: 3, ...sent[0].review}});
  assert.equal(await queuedEdit, true);
  assert.equal(await flush, true);
  assert.equal(s.ui.context.window.CataractBiometry.hasUnsaved(), false);
  assert.deepEqual(sent[1], {cohort_id: "test-cohort", mrn: "TEST001", source: "orders", action: "save_review",
    documents: [{id: file.id, digest: file.digest}], capture_id: original.capture_id, revision: 3,
    review: {confirmed: true, edits: [["eyes.OD.al", {raw: "24.00", value: 24}]]}});
  assert.equal(draft.storage, "saved");
}

async function staleReviewConflict() {
  const file = docs[1], snapshots = [];
  let attempts = 0, loads = 0;
  const s = setup(async request => {
    if (request.action === "status") return ready;
    if (request.action === "load") {
      if (++loads === 1) throw new Error("無法暫時讀取本機資料。");
      return {state: "saved", category: "biometry", document: {...file, capture_id: "conflict-1", data: {},
        saved_review: {revision: 0, confirmed: false, edits: []}}};
    }
    if (++attempts === 1) return {state: "conflict", saved_review: {revision: 1,
      confirmed: true, edits: [["eyes.OD.al", {raw: "23", value: 23}]]}};
    assert.equal(request.revision, 1);
    return {state: "saved", saved_review: {revision: 2, ...request.review}};
  });
  const stop = s.control.subscribeDocument(file, state => snapshots.push(state));
  await tick();
  assert.match(snapshots.at(-1).restoreError, /無法暫時讀取/);
  await s.control.reloadDocument(file);
  assert.equal(snapshots.at(-1).hasResult, true);
  assert.equal(snapshots.at(-1).restoreError, "");
  const options = s.control.reviewOptions(snapshots.at(-1).result), draft = options.draft.get(0);
  draft.edits.set("eyes.OD.al", {raw: "24", value: 24});
  assert.equal(await options.onDraftChange(0, draft), false);
  assert.equal(draft.storage, "conflict");
  assert.equal(await s.ui.context.window.CataractBiometry.flushAll(), false);
  assert.equal(attempts, 1, "conflicted revisions must not silently overwrite local data");
  assert.equal(options.useSavedDraft(0), true);
  assert.equal(draft.confirmed, true);
  assert.equal(draft.edits.get("eyes.OD.al").value, 23);
  draft.confirmed = false;
  assert.equal(await options.onDraftChange(0, draft), true);
  assert.equal(attempts, 2);
  stop();
  s.control.clear();
}

(async () => {
  await orderSelections();
  await scanScopeAndReset();
  await retryWithoutInvalidatedJobId();
  await scanIdentityAndPolling();
  await inlineSinglePdf();
  await persistentDocumentDraft();
  await staleReviewConflict();
  process.stdout.write("Biometry review, local restore, ordered writes, replacement detection and conflict recovery passed\n");
})().catch(error => { console.error(error); process.exitCode = 1; });
