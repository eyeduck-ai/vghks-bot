"use strict";

const assert = require("node:assert/strict");
const {surface} = require("./review_ui.js");

function sample() {
  const ui = surface();
  ui.run("vghks_bot/static/cataract-biometry-exchange.js");
  const {exportResult} = ui.context.window.CataractBiometryExchange;
  const verified = (value, unit) => ({value, candidate_value: value, unit, status: "verified",
    data_status: "present", verification_status: "cross_checked"});
  const table = {table_id: "synthetic-table", eye: "OD", page_number: 4, lens_model: "SYNTHETIC",
    constants: [{name: "A", value: "119.0"}], rows: [{iol_power: 19, predicted_refraction: 0.12, selection: "unknown"}]};
  const data = {schema_version: "1.2", source_sha256: "a".repeat(64), workflow: "synthetic-parser-test",
    verification_state: "verified", completeness: {present: 4}, issues: [], metadata: {selected_pages: [4]},
    eyes: {OD: {al: verified(23.62, "mm"), acd: verified(3.12, "mm"), k1: {power: verified(43.7, "D")}},
      OS: {al: verified(23.41, "mm")}}, iol_calculations: [table]};
  const reviewed = {confirmed: true, eyes: {OD: {al: 24.07, acd: null, k1: {power: 43.7, axis: 0}}, OS: {al: 23.41}},
    iol_calculations: [{table_id: table.table_id, page_number: table.page_number,
      constants: [{name: "A", value: null}], rows: [{iol_power: 18.5, selection: "marked"}]}]};
  const records = [
    {id: "synthetic-1", digest: data.source_sha256, date: "2026-11-18", label: "合成 PDF 1", pages: [4], data, review: reviewed,
      review_revision: 5, mrn: "SYNTHETIC-LOCAL-MRN"},
    {id: "synthetic-2", digest: "b".repeat(64), date: "2026-11-17", label: "合成 PDF 2", pages: [2],
      data: {...data, source_sha256: "b".repeat(64), metadata: {selected_pages: [2]},
        eyes: {OD: {}, OS: {al: verified(25.11, "mm")}}, iol_calculations: []},
      review: {confirmed: true, eyes: {OS: {al: 25.11}}}}
  ];
  const failures = [{id: "synthetic-failed", pages: [1], error: "無擷取資料。"}];
  const original = JSON.stringify(records);
  const payload = JSON.parse(JSON.stringify(exportResult({schema_version: 2, source: "orders", mrn: "SYNTHETIC-LOCAL-MRN"},
    records, "all", failures)));
  assert.equal(JSON.stringify(records), original, "JSON export must not mutate parsed or reviewed data");
  assert.equal(payload.schema_version, 1);
  assert.equal(payload.source, "orders");
  assert.equal(payload.category, "all");
  assert.deepEqual(payload.omitted_documents, failures);
  assert.equal(JSON.stringify(payload).includes("SYNTHETIC-LOCAL-MRN"), false);
  assert.equal(payload.documents[0].review_revision, undefined);
  const [first, second] = payload.documents;
  assert.equal(first.data.eyes.OD.al.candidate_value, 24.07);
  assert.equal(first.data.eyes.OD.al.value, null);
  assert.equal(first.data.eyes.OD.al.verification_status, "unverified");
  assert.equal(first.data.eyes.OD.acd.data_status, "blank");
  assert.equal(first.data.eyes.OD.k1.axis.unit, "degree");
  assert.equal(first.data.eyes.OD.k1.axis.candidate_value, 0);
  assert.equal(first.data.eyes.OD.k1.power.status, "verified");
  assert.equal(first.data.eyes.OS.al.candidate_value, 23.41);
  assert.equal(first.data.verification_state, "needs_review");
  assert.equal(first.data.completeness, undefined);
  assert.equal(first.data.iol_calculations[0].rows[0].iol_power, 18.5);
  assert.equal(first.data.iol_calculations[0].rows[0].selection, "unknown", "review cannot fabricate a printed selection");
  assert.equal(first.data.iol_calculations[0].constants[0].value, "");
  assert.equal(first.review.iol_calculations[0].constants[0].value, null);
  assert.deepEqual(first.parsed_data, data);
  assert.equal(first.data.iol_calculations[0].page_number, 4);
  assert.deepEqual(first.pages, [4]);
  assert.deepEqual(second.pages, [2]);
  assert.equal(second.data.eyes.OS.al.candidate_value, 25.11);
  assert.equal(second.data.eyes.OD.al, undefined);
  assert.equal(second.parsed_data, undefined);
  return payload;
}

if (require.main === module) {
  sample();
  console.log("Versioned Biometry-Clip candidates, page boundaries and preserved parser evidence passed.");
}
module.exports = {sample};
