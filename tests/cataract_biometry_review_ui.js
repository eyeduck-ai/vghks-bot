"use strict";

const assert = require("node:assert/strict");
const {surface, tick} = require("./review_ui.js");

async function run() {
  const ui = surface(), copied = [], downloads = [], pages = [];
  const prototype = Object.getPrototypeOf(ui.node("select"));
  Object.defineProperty(prototype, "options", {get() {return this.children;}});
  Object.assign(ui.context, {
    el: ui.node,
    button(label, action, className) {
      const node = ui.node("button", label, className);
      node.addEventListener("click", action);
      return node;
    },
    navigator: {clipboard: {async writeText(text) {copied.push(text);}}},
    download(name, text) {downloads.push({name, text});}
  });
  ui.run("vghks_bot/static/cataract-biometry-exchange.js");
  ui.run("vghks_bot/static/cataract-biometry-review.js");

  const parsed = {eyes: {OD: {al: {value: null, candidate_value: 23.62, unit: "mm", status: "unverified"}},
    OS: {al: {value: 23.41, unit: "mm", status: "verified"}}}, completeness: {expected: 2}, metadata: {
    selected_pages: [1, 4], merge_policy_version: "test"}, issues: [{code: "no_biometry"}, {code: "iol_eye_unresolved"}],
    iol_calculations: [{table_id: "SYN-IOL", eye: "OD", lens_model: "SYN-LENS", lens_type: "non_toric",
      page_number: 4, formula: "SYN-FORMULA", flags: [], constants: [{name: "A", value: "119.0"}],
      rows: [{iol_power: 19, predicted_refraction: -0.12, flags: ["iol_power_unreadable"]}]}]};
  const result = {schema_version: 1, source: "scans", category: "all", documents: [
    {id: "file-1", label: "合成眼科報告", digest: "b".repeat(64), data: parsed, capture_id: "test-capture",
      saved_review: {confirmed: false, edits: [], revision: 0},
      review: {eyes: {OD: {al: {pages: [4], confidence: 0.6, confidence_scale: "0..1 (uncalibrated)"}}}}},
    {id: "file-2", label: "合成無資料報告", error: "無法擷取。"},
    {id: "file-3", label: "合成第二報告", data: parsed},
    {id: "file-4", label: "合成無量測報告", data: {eyes: {OD: {}, OS: {}}, iol_calculations: []}}
  ]};
  const original = JSON.stringify(result), draft = new Map();
  const options = {result, draft, onPageChange: page => pages.push(page)};
  const {element, setDocument, refresh} = ui.context.window.CataractBiometryReview.create(options);
  ui.$("#workbench").append(element);
  const byLabel = (tag, label, root = element) => root.querySelectorAll(tag).find(node => node.getAttribute("aria-label") === label);
  const byText = (tag, text) => element.querySelectorAll(tag).find(node => node.textContent === text);
  const confirm = element.querySelectorAll("input").find(node => node.type === "checkbox");
  const copy = byText("button", "複製 JSON"), save = byText("button", "下載 JSON");
  const range = byLabel("select", "JSON 輸出範圍");
  const confirmFile = async () => {confirm.checked = true; await confirm.emit("change");};
  const change = async (input, value) => {input.value = value; await input.emit("input");};
  assert.equal(element.querySelector("textarea"), null);
  assert.equal(copy.disabled, true);
  const al = byLabel("input", "OD AL 眼軸長 (mm)");
  assert.equal(al.value, "23.62");
  assert.equal(al.classList.contains("review-attention"), true);
  assert.ok(element.querySelectorAll("span").some(node => node.textContent === "辨識信心偏低"));
  await byText("button", "第 4 頁").emit("click");
  assert.deepEqual(pages, [4]);
  await change(al, "incorrect");
  assert.equal(al.getAttribute("aria-invalid"), "true");
  assert.equal(confirm.disabled, true);
  await change(al, "２３．０５");
  assert.equal(al.getAttribute("aria-invalid"), null);
  assert.equal(al.classList.contains("review-attention"), false);
  await byText("button", "水晶體度數結果").emit("click");
  const power = byLabel("input", "第 1 列 水晶體度數 (D)");
  assert.equal(power.classList.contains("review-attention"), true);
  await change(power, "+18.50");
  await confirmFile();
  assert.equal(copy.disabled, true);

  setDocument(1);
  assert.equal(confirm.disabled, true);
  assert.match(element.querySelector(".cataract-biometry-review-fields").children[0].textContent, /無法擷取/);
  setDocument(2);
  await byText("button", "眼科生物測量").emit("click");
  assert.equal(byLabel("input", "OD AL 眼軸長 (mm)").value, "23.62");
  await confirmFile();
  assert.equal(copy.disabled, false);
  range.value = "biometry";
  await copy.emit("click");
  await tick();
  const bio = JSON.parse(copied[0]);
  assert.equal(bio.schema_version, 1);
  assert.equal(bio.category, "biometry");
  assert.equal(bio.documents[0].review.eyes.OD.al, 23.05);
  assert.equal(bio.documents[0].data.eyes.OD.al.candidate_value, 23.05);
  assert.equal(bio.documents[0].parsed_data.eyes.OD.al.candidate_value, 23.62);
  assert.equal(bio.documents[0].capture_id, undefined);
  assert.equal(bio.documents[0].saved_review, undefined);
  assert.equal(bio.documents[0].review.iol_calculations, undefined);
  assert.equal(bio.documents[0].data.iol_calculations, undefined);
  assert.deepEqual(bio.omitted_documents.map(item => item.id), ["file-2", "file-4"]);

  range.value = "iol";
  await save.emit("click");
  const iol = JSON.parse(downloads[0].text);
  assert.equal(downloads[0].name, "vghks-biometry-scans-iol.json");
  assert.equal(iol.documents[0].review.iol_calculations[0].rows[0].iol_power, 18.5);
  assert.equal(iol.documents[0].data.iol_calculations[0].rows[0].iol_power, 18.5);
  assert.equal(iol.documents[0].parsed_data.iol_calculations[0].rows[0].iol_power, 19);
  assert.equal(iol.documents[0].review.eyes, undefined);
  assert.equal(iol.documents[0].data.eyes, undefined);
  assert.deepEqual(iol.documents[0].data.issues, [{code: "iol_eye_unresolved"}]);

  range.value = "all";
  await save.emit("click");
  const all = JSON.parse(downloads[1].text);
  assert.equal(all.documents[0].review.eyes.OD.al, 23.05);
  assert.equal(all.documents[0].review.iol_calculations[0].rows[0].iol_power, 18.5);
  assert.equal(JSON.stringify(result), original);

  const reopened = ui.context.window.CataractBiometryReview.create(options);
  assert.equal(byLabel("input", "OD AL 眼軸長 (mm)", reopened.element).value, "２３．０５");
  await change(byLabel("input", "OD AL 眼軸長 (mm)", reopened.element), "23.00");
  assert.equal(reopened.element.querySelectorAll("button").find(node => node.textContent === "複製 JSON").disabled, true);
  await save.emit("click");
  assert.equal(downloads.length, 2, "edit in another view invalidates export even if the editor has not refreshed");
  setDocument(0);
  refresh();
  assert.equal(byLabel("input", "OD AL 眼軸長 (mm)").value, "23.00");
  console.log("Cataract review confirmed-document gating, page evidence, immutable source and selective corrected JSON passed.");
}

if (require.main === module) run().catch(error => {console.error(error); process.exitCode = 1;});
module.exports = run;
