"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const context = {window:{addEventListener(){}}, document:{createElement:tag=>({tagName:tag.toUpperCase()})}, Intl, Date};
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
console.log("Demographic age display checks passed.");
