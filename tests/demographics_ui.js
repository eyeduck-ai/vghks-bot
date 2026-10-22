"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
class Element {
  constructor(tag) {this.tagName = tag.toUpperCase();this.attributes = {};this.listeners = {};}
  setAttribute(name, value) {this.attributes[name] = value;}
  addEventListener(name, listener) {this.listeners[name] = listener;}
  replaceWith(element) {this.replacement = element;}
  focus() {this.focused = true;}
  select() {this.selected = true;}
}
const context = {window:{addEventListener(){}}, document:{createElement:tag=>new Element(tag)}, navigator:{}, Intl, Date};
vm.createContext(context);
vm.runInContext(fs.readFileSync("vghks_bot/static/clinical-ui.js","utf8"), context);
const ui = context.window.ClinicalUI, now = new Date("2026-10-02T16:05:00Z"); // Already Oct 3 in Taipei.
for (const [patient, expected] of [
  [{birthday:"1958-10-03", age:"stale"}, "68 歲"],
  [{birthday:"1958-10-04"}, "67 歲"],
  [{birthday:"2026-10-03"}, "0 歲"],
  [{birthday:"2026-10-04", age:68}, "未提供"],
  [{birthday:"1880-01-01"}, "未提供"],
  [{birthday:"1958-02-30", age:"68"}, "68 歲"],
  [{birthday:"", age:0}, "0 歲"],
  [{age:"68歲"}, "68歲"],
  [{}, "未提供"],
  [{mrn:"X", records:[{mrn:"X", birthday:"1958-10-03"}]}, "68 歲"],
  [{mrn:"X", records:[{mrn:"Y", birthday:"1958-10-03", age:68}]}, "未提供"],
]) assert.equal(ui.patientAge(patient, now), expected);
assert.equal(ui.patientAge({birthday:"2000-02-29"}, new Date("2026-02-28T04:00:00Z")), "25 歲");
assert.equal(ui.patientAge({birthday:"2000-02-29"}, new Date("2026-03-01T04:00:00Z")), "26 歲");
const badge = ui.ageBadge({age:68});
assert.equal(badge.tagName, "SPAN");
assert.equal(badge.textContent, "年齡 68 歲");
assert.equal(badge.className, "patient-age-badge");
for (const [age, expected] of [[54,"patient-age-badge patient-age-young"], ["54歲","patient-age-badge patient-age-young"],
  ["54 歲","patient-age-badge patient-age-young"], [0,"patient-age-badge patient-age-young"],
  [55,"patient-age-label"], [68,"patient-age-label"], [undefined,"patient-age-label"], ["未提供","patient-age-label"]]) {
  assert.equal(ui.ageBadge({age}, {highlightBelow:55}).className, expected, "age " + age);
}
let clock = "2026-10-02T15:59:59Z";
context.Date = class extends Date {constructor(...args) {super(...(args.length ? args : [clock]));}};
assert.equal(ui.ageBadge({birthday:"1971-10-03"}, {highlightBelow:55}).className, "patient-age-badge patient-age-young");
clock = "2026-10-02T16:00:00Z"; // Taipei birthday starts, even though UTC is still Oct 2.
assert.equal(ui.ageBadge({birthday:"1971-10-03"}, {highlightBelow:55}).className, "patient-age-label");
assert.equal(ui.ageBadge({birthday:"1971-10-03"}, {highlightBelow:55}).textContent, "年齡 55 歲");

async function checkCopyButtons() {
  const announcements = [], writes = [];
  context.navigator.clipboard = {writeText:async value => {writes.push(value);}};
  const mrn = ui.mrnBadge("00123456", message => announcements.push(message));
  assert.equal(mrn.tagName, "BUTTON");assert.equal(mrn.type, "button");
  assert.equal(mrn.className, "patient-mrn-copy review-mrn-copy");
  assert.equal(mrn.title, "點擊複製病歷號");
  assert.equal(mrn.attributes["aria-label"], "複製病歷號 00123456");
  await mrn.listeners.click();
  assert.deepEqual(writes, ["00123456"]);assert.equal(announcements.pop(), "已複製病歷號");

  const row = "00123456\t測試姓名\t0912345678";
  const name = ui.copyButton(row, {label:"刀表資料", text:"測試姓名", className:"review-name-copy", announce:message=>announcements.push(message)});
  assert.equal(name.textContent, "測試姓名");assert.equal(name.className, "review-name-copy");
  assert.equal(name.attributes["aria-label"], "複製刀表資料 測試姓名");
  await name.listeners.click();
  assert.equal(writes.at(-1), row, "TSV and leading zeros must be copied verbatim");
  assert.equal(announcements.pop(), "已複製刀表資料");
  await ui.copyButton("00123456\t測試姓名\t").listeners.click();
  assert.equal(writes.at(-1), "00123456\t測試姓名\t", "A missing mobile keeps its empty spreadsheet column");

  let clipboard = "Existing clipboard text";
  const rejectedWrites = [];
  context.navigator.clipboard = {writeText:async value => {
    rejectedWrites.push(value);
    if (rejectedWrites.length === 1) throw new Error("denied");
    clipboard = value;
  }};
  const denied = ui.copyButton(row, {label:"刀表資料", text:"測試姓名", announce:message=>announcements.push(message)});
  await denied.listeners.click();
  assert.equal(clipboard, "Existing clipboard text");
  assert.deepEqual(rejectedWrites, [row], "Failure must not trigger another clipboard write");
  assert.equal(denied.replacement.tagName, "INPUT");
  assert.equal(denied.replacement.value, row);
  assert.equal(denied.replacement.readOnly, true);
  assert.equal(denied.replacement.focused, true);assert.equal(denied.replacement.selected, true);
  assert.equal(denied.replacement.attributes["aria-label"], "刀表資料，請手動複製");
  assert.equal(announcements.pop(), "請複製已選取的刀表資料（Ctrl+C）");
  delete context.navigator.clipboard;
  const unavailable = ui.mrnBadge("00123456");
  await unavailable.listeners.click();
  assert.equal(unavailable.replacement.value, "00123456");
  assert.ok(unavailable.replacement.className.includes("patient-mrn-fallback"));
  assert.equal(clipboard, "Existing clipboard text");
  console.log("Demographic display and clipboard checks passed.");
}
checkCopyButtons().catch(error => {console.error(error);process.exitCode = 1;});
