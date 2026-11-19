"use strict";

window.CataractBiometryExchange = (() => {
  const scalarUnits = {al: "mm", acd: "mm", lt: "mm", cct: "um", wtw: "mm", pupil: "mm",
    mean_k: "D", corneal_astigmatism: "D", corneal_astigmatism_axis: "degree"};
  const keratometryUnits = {power: "D", axis: "degree"};
  const keratometryFields = ["k1", "k2", "tk1", "tk2"];

  function transferFields(source, reviewed, units) {
    let fields = source;
    for (const [key, unit] of Object.entries(units)) {
      if (!Object.hasOwn(reviewed, key)) continue;
      const original = source[key];
      const value = reviewed[key];
      if (value === (original?.value ?? original?.candidate_value ?? null) ||
          (value !== null && (typeof value !== "number" || !Number.isFinite(value)))) continue;
      if (fields === source) fields = {...source};
      // Clip can load human edits as candidates without mistaking them for
      // independent parser verification. The original stays in parsed_data.
      fields[key] = {unit: original?.unit ?? unit, value: null, candidate_value: value,
        status: value === null ? "blank" : "unverified", data_status: value === null ? "blank" : "present",
        verification_status: "unverified"};
    }
    return fields;
  }

  function transferEyes(eyes, reviewed) {
    let result = eyes;
    for (const eye of ["OD", "OS"]) {
      const values = reviewed[eye];
      if (!values) continue;
      const source = eyes[eye] || {};
      let fields = transferFields(source, values, scalarUnits);
      for (const key of keratometryFields) {
        if (!values[key]) continue;
        const group = source[key] || {};
        const updated = transferFields(group, values[key], keratometryUnits);
        if (updated !== group) fields = {...fields, [key]: updated};
      }
      if (fields !== source) result = {...result, [eye]: fields};
    }
    return result;
  }

  function transferKeys(source, reviewed, keys) {
    let result = source;
    for (const key of keys) {
      if (!Object.hasOwn(reviewed, key) || (source[key] ?? null) === reviewed[key]) continue;
      if (result === source) result = {...source};
      result[key] = reviewed[key];
    }
    return result;
  }

  function transferList(source, values, transfer) {
    if (!Array.isArray(source) || !Array.isArray(values)) return source;
    const result = source.map((item, index) => values[index] ? transfer(item, values[index]) : item);
    return result.some((item, index) => item !== source[index]) ? result : source;
  }

  function transferTable(source, reviewed) {
    if (source.table_id !== reviewed.table_id || source.page_number !== reviewed.page_number) return source;
    const table = transferKeys(source, reviewed, ["eye", "lens_model", "lens_type", "formula", "keratometry_source", "target_refraction"]);
    const constants = transferList(source.constants, reviewed.constants, (constant, value) => {
      if (value.name !== constant.name || !Object.hasOwn(value, "value")) return constant;
      return transferKeys(constant, {value: value.value ?? ""}, ["value"]);
    });
    const rows = transferList(source.rows, reviewed.rows, (row, value) =>
      transferKeys(row, value, ["iol_power", "predicted_refraction", "toric_power", "axis"]));
    if (source === table && source.constants === constants && source.rows === rows) return source;
    return {...table, constants, rows, verification_status: "unverified"};
  }

  function exportDocument({id, digest, date, label, pages, data, review}) {
    const document = {id, digest, date, label, pages, data, review};
    if (!data || !review?.confirmed) return document;
    const eyes = data.eyes && review.eyes ? transferEyes(data.eyes, review.eyes) : data.eyes;
    const tables = transferList(data.iol_calculations, review.iol_calculations, transferTable);
    if (eyes === data.eyes && tables === data.iol_calculations) return document;
    const updated = {...data};
    if (eyes !== data.eyes) {
      updated.eyes = eyes;
      updated.verification_state = "needs_review";
      delete updated.completeness;
    }
    if (tables !== data.iol_calculations) updated.iol_calculations = tables;
    return {...document, data: updated, parsed_data: data};
  }

  function exportResult(result, documents, category, omitted = []) {
    return {schema_version: 1, source: result.source, category, documents: documents.map(exportDocument),
      ...(omitted.length ? {omitted_documents: omitted} : {})};
  }

  return {exportResult};
})();
