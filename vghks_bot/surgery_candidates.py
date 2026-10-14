"""Surgery candidates with explicit field evidence, never inferred diagnoses."""
from __future__ import annotations

import re
import unicodedata

from .analysis_numeric import iso_day, norm, side
from .analysis_store import digest
from .settings import FOLLOWUP_TAG
from .surgery import parse_surgery
from .tags import classification

PROCEDURES = {"CATA": "Phaco-IOL", "LENSX+CATA": "LenSx-Phaco-IOL",
              "VT": "VT", "VT+MP": "VT+MP", "LMR": "LMR"}
FIELD_NAMES = {"date": "刀日", "hospital": "醫院", "mrn": "病歷號", "name": "姓名", "tel": "TEL",
               "ga": "GA", "side": "側別", "diagnosis": "診斷", "grade": "Grade",
               "procedure": "術式", "iol": "IOL", "target": "IOL Target", "plan": "Plan"}


def followup_candidates(records, settings, *, default_hospital="高榮"):
    """Use the user's existing FU classification, with one permanent draft per MRN."""
    result = {}
    for record in sorted(records, key=lambda r: (r.get("date", ""), r.get("updated_at", ""))):
        hits = [hit for hit in classification(record, settings)["matches"]
                if hit["category"] == FOLLOWUP_TAG.id or hit["category_name"] == FOLLOWUP_TAG.name]
        if not hits:
            continue
        mrn = norm(record["mrn"])
        key = str(int(mrn)) if mrn.isdigit() else mrn
        row = result.setdefault(key, {"id": digest(["fu", key])[:32], "kind": "fu",
            "fields": {"mrn": record["mrn"], "name": record.get("name", ""), "hospital": default_hospital, "tag": "FU"},
            "sources": [], "evidence": {}, "differences": {}, "ambiguities": {},
            "schedules": [], "date_conflict": False, "cancelled_hint": False})
        row["fields"]["name"] = record.get("name") or row["fields"]["name"]
        version = digest([record.get("soap", ""), record.get("soap_structure") or {}])
        row["sources"].append({"record_id": record["id"], "date": record.get("date", ""),
                               "excerpt": "\n\n".join(dict.fromkeys(hit["excerpt"] for hit in hits)),
                               "source_version": version, "version": version})
        row["source_records"] = [source["record_id"] for source in row["sources"]]
    return list(result.values())


def labelled(text, names):
    pattern = r"(?im)^\s*[-•]?\s*(?:" + names + r")\s*[:：=]\s*(.+)$"
    return list(dict.fromkeys(m.group(1).strip() for m in re.finditer(pattern, text)
                              if "?" not in m.group(1) and "{" not in m.group(1)))


def candidates(records, *, structured_only=False, strict=False, default_hospital="高榮"):
    result = {}
    for record in sorted(records, key=lambda r: (r.get("date", ""), r.get("updated_at", ""))):
        if structured_only:
            structure = record.get("soap_structure") or {}
            source_texts = [(field, structure[field]) for field in ("assessment_plan", "assessment", "plan")
                            if isinstance(structure.get(field), str)]
        else:
            source_texts = [("soap", record.get("soap", ""))]
        for source_field, soap in source_texts:
          starts = list(re.finditer(r"(?im)^[ \t]*(?:P[ \t]*[:：][ \t]*)?(?P<tag>#[ \t]*Arrange\b[^\n]*)", soap))
          for ordinal, match in enumerate(starts):
            tail = soap[match.start("tag"):]
            stop = re.search(r"\n\s*#|\n\s*\n|\n[SOAP]\s*:", tail)
            excerpt = tail[:stop.start()] if stop else tail
            parsed = parse_surgery(excerpt)
            fields = {"date": parsed["date_iso"], "hospital": default_hospital, "mrn": record["mrn"], "name": record.get("name", ""),
                      "tel": parsed["tel"], "side": parsed["laterality"],
                      "procedure": PROCEDURES.get(parsed["procedure"], parsed["procedure"]),
                      "iol": parsed["iol"], "target": parsed["target"], "diagnosis": "",
                      "grade": "", "ga": "", "plan": ""}
            evidence = {k: {"record_id": record["id"], "date": record["date"], "text": excerpt}
                        for k, v in fields.items() if v and k != "hospital"}
            ambiguities = {}
            if strict:
                header = unicodedata.normalize("NFKC", excerpt.splitlines()[0])
                sides = re.findall(r"\b(?:OD|OS|OU)\b", header, re.I)
                procedures = re.findall(r"\b(?:LENSX\s*\+\s*CATA|VT\s*\+\s*MP|CATA|LMR|VT)\b", header, re.I)
                if len(sides) != 1 or (sides and re.search(r"\b" + sides[0] + r"\b\s*[/、?]|[/、]\s*\b" + sides[0] + r"\b", header, re.I)):
                    ambiguities["side"] = sides or ["缺少明確眼別"]
                if len(procedures) != 1:
                    ambiguities["procedure"] = procedures or ["缺少明確術式"]
                if not parsed["date_iso"]:
                    ambiguities["date"] = [parsed["scheduled_date"] or "缺少有效刀日"]
                targets = list(dict.fromkeys(re.findall(r"\b(?:Target|T)\s*[:=]?\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+))(?![\d.?])", header, re.I)))
                if len(targets) > 1:
                    ambiguities["target"] = targets
                lenses = re.findall(r"\([^()]*\)", header)
                if len(lenses) > 1:
                    ambiguities["iol"] = lenses
                phones = list(dict.fromkeys(re.findall(r"(?im)(?:^\s*[-•]?\s*|\s+)TEL\s*[:：][ \t]*([^\n]+)", excerpt)))
                if len(phones) > 1:
                    ambiguities["tel"] = phones
                if "?" in header or "{" in header:
                    ambiguities["header"] = ["手術標記包含未確認欄位"]
            # Only explicitly labelled fields in the Arrange paragraph are auto-filled.
            # Other SOAP paragraphs are provided beside the preview for human selection.
            for field, names in (("diagnosis", "Diagnosis|Dx|診斷"),
                                  ("grade", "Grade|分級"), ("plan", "Plan|計畫"),
                                  ("ga", "Anesthesia|麻醉|GA")):
                values = labelled(excerpt, names)
                if field == "ga":
                    values = ["GA" for v in values if norm(v).upper() in {"GA", "GENERAL", "GENERAL ANESTHESIA", "YES", "是", "全身麻醉"}]
                if len(set(values)) == 1:
                    fields[field] = values[0]
                    evidence[field] = {"record_id": record["id"], "date": record["date"], "text": excerpt}
                elif values:
                    ambiguities[field] = values
            event = [record["mrn"], fields["date"], fields["side"], fields["procedure"]]
            # Incomplete events cannot be merged across unrelated notes.
            key = digest(event if all(event) else [event, record["id"], match.start()])[:24]
            source_version = digest([record.get("soap", ""), record.get("soap_structure") or {}])
            source = {"record_id": record["id"], "date": record["date"], "excerpt": excerpt,
                      "field": source_field, "block_hash": digest(norm(excerpt)), "event_hash": digest(event),
                      "source_version": source_version, "block_count": len(starts), "ordinal": ordinal,
                      # Ordinals are safe lineage only for a single Arrange block.
                      "source_key": digest([record["id"], source_field, ordinal]) if len(starts) == 1 else ""}
            previous = result.get(key)
            sources = (previous["sources"] if previous else []) + [source]
            if strict and previous and any(s.get("record_id") == record["id"] and s.get("field") == source_field
                                           for s in previous["sources"]):
                ambiguities["source"] = ["同一 SOAP 有重複的手術標記"]
            differences = (previous or {}).get("differences", {})
            if previous:
                for field, value in fields.items():
                    before = previous["fields"].get(field)
                    if value and before and value != before:
                        differences[field] = list(dict.fromkeys([*differences.get(field, []), before, value]))
                    if not value and before:
                        fields[field] = before
                        evidence[field] = previous["evidence"].get(field, {})
            prior_ambiguities = (previous or {}).get("ambiguities", {})
            ambiguities = {**prior_ambiguities, **ambiguities}
            result[key] = {"id": key, "fields": fields, "hospital_default": True, "sources": sources, "evidence": evidence,
                           "differences": differences, "ambiguities": ambiguities,
                           "schedules": [], "date_conflict": False,
                           "cancelled_hint": bool((previous or {}).get("cancelled_hint") or re.search(r"(?i)取消|cancel(?:led|ed)?", excerpt))}
    for row in result.values():
        for field in set(row["differences"]) | set(row["ambiguities"]):
            if field in row["fields"]:
                row["fields"][field] = ""
        row["automatic_ready"] = (not row["differences"] and not row["ambiguities"] and not row["cancelled_hint"]
                                  and all(row["fields"].get(field) for field in ("mrn", "date", "side", "procedure")))
    return list(result.values())


def enrich(rows, steps):
    schedules = []
    patients = sorted((s for s in steps if s["kind"] == "surgery_patient"), key=lambda s: s["saved_at"])
    patient_step = patients[-1] if patients else None
    patient = (patient_step["payload"].get("patient") or {}) if patient_step else {}
    for step in steps:
        if step["kind"] == "surgery_schedule":
            for item in step["payload"]:
                schedules.append({**item, "fetched_at": step["saved_at"]})
    latest = {}
    for item in sorted(schedules, key=lambda s: s["fetched_at"]):
        extra = item.get("extra") or {}
        key = extra.get("orreqno") or digest([item.get("patient_mrn"), item.get("case_no"),
                                             item.get("surgery_date"), item.get("procedure")])
        latest[key] = item
    for row in rows:
        fields = row["fields"]
        row["patient_info"] = patient
        row["patient_info_at"] = patient_step["saved_at"] if patient_step else ""
        for field, labels in (("name", ("hnamec", "name", "姓名")),
                              ("tel", ("phone", "tel", "電話"))):
            values = {str(patient[k]).strip() for k in labels if isinstance(patient.get(k), (str, int)) and str(patient[k]).strip()}
            if len(values) == 1 and field not in row["differences"] and field not in row["ambiguities"]:
                value = next(iter(values))
                if fields[field] and fields[field] != value:
                    row["differences"][field] = [fields[field], value]
                    fields[field] = ""
                elif not fields[field]:
                    fields[field] = value
                    row["evidence"][field] = {"source": "OPPL patient", "date": row["patient_info_at"], "text": value}
            elif len(values) > 1:
                row["ambiguities"][field] = sorted(values)
                fields[field] = ""
        # Present all returned patient schedules; do not assume similar operation names
        # mean the same event, or that a blank side means the intended eye.
        row["schedules"] = [{
            "id": str(key), "date": iso_day(s.get("surgery_date", "")),
            "start_time": s.get("start_time", ""), "procedure": s.get("procedure", ""),
            "side": side(s.get("procedure", "")), "status": s.get("status", ""),
            "room": s.get("room", ""), "doctor": s.get("doctor_name", ""),
            "fetched_at": s["fetched_at"],
        } for key, s in latest.items() if s.get("patient_mrn") == fields["mrn"]]
        dates = {s["date"] for s in row["schedules"] if s["date"]}
        row["date_conflict"] = bool(dates and (len(dates) != 1 or fields["date"] not in dates))
    return rows
