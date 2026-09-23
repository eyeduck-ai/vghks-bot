"""Conservative extraction: original tables remain authoritative and always visible."""
from __future__ import annotations

import math
import re
import unicodedata
from datetime import date

from .analysis_store import digest

MODULES = {
    "retina": {"name": "視網膜", "numeric": ["Va"], "orders": [
        "Fundus Color Photo Picture, eac", "Microsonography", "FAG"]},
    "cataract": {"name": "白內障", "numeric": ["Va", "驗光-散瞳前", "Endothelial No", "KM"],
                 "orders": ["DBR, free charge", "Keratomery", "Corneal Endothelial Microscopy",
                            "Photokeratoscopy (color)", "FUNCTIONAL VISUAL ANALYSIS"]},
    "surgery": {"name": "刀表更新", "numeric": [], "orders": []},
}


def norm(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value))).strip()


def term_match(text, term):
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(norm(term).casefold()) + r"(?![a-z0-9])",
                          norm(text).casefold()))


def exam_name(value):
    value = norm(value).replace("–", "-").replace("－", "-")
    for name in ("驗光-散瞳前", "Endothelial No", "Va", "KM"):
        if term_match(value, name):
            return name
    return ""


def iso_day(value):
    match = re.search(r"(?<!\d)(\d{3,4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)", norm(value))
    if not match:
        match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", norm(value))
    if match:
        year, month, day = map(int, match.groups())
        try:
            return date(year + 1911 if year < 1911 else year, month, day).isoformat()
        except ValueError:
            pass
    return ""


def side(value):
    found = set(re.findall(r"(?<![A-Z])(OD|OS|OU)(?![A-Z])", norm(value).upper()))
    if "右眼" in str(value):
        found.add("OD")
    if "左眼" in str(value):
        found.add("OS")
    return next(iter(found)) if len(found) == 1 else ""


def numeric(value):
    text = norm(value).replace("−", "-")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", text):
        return None
    result = float(text)
    return result if math.isfinite(result) else None


def extract_tables(payload, source_key, saved_at=""):
    """Support wide/date tables and named eye tables without guessing row context."""
    output = []
    case_day = (payload.get("case") or {}).get("visit_date", "")
    for ti, table in enumerate(payload.get("tables", [])):
        title = str(table.get("title", ""))
        headers = [str(v) for v in table.get("headers", [])]
        rows = table.get("rows", [])
        title_exam = exam_name(title)
        normalized = [norm(h).casefold() for h in headers]
        date_cols = [i for i, h in enumerate(normalized) if re.search(r"date|日期|時間", h)]
        eye_cols = [i for i, h in enumerate(normalized) if h in {"eye", "眼別", "側別", "laterality"}]
        unit_cols = [i for i, h in enumerate(normalized) if h in {"unit", "units", "單位"}]
        item_cols = [i for i, h in enumerate(normalized) if h in {"item", "test", "項目", "檢查項目", "名稱"}]
        detected = {exam_name(h) for h in headers} - {""}
        for ri, row in enumerate(rows):
            values = [str(v) for v in row]
            def get(i, values=values):
                return values[i] if i < len(values) else ""
            row_exam = next((exam_name(get(i)) for i in item_cols if exam_name(get(i))), "")
            row_day = next((iso_day(get(i)) for i in date_cols if iso_day(get(i))), "") or case_day or iso_day(title)
            row_side = next((side(get(i)) for i in eye_cols + item_cols if side(get(i))), "") or side(title)
            row_unit = next((get(i) for i in unit_cols if get(i)), "")
            cells = []
            for ci, value in enumerate(values):
                header = headers[ci] if ci < len(headers) else ""
                if ci in date_cols + eye_cols + unit_cols + item_cols or not value.strip():
                    continue
                category = exam_name(header) or row_exam or (title_exam if not detected else "")
                # Never infer a measurement from a bare unrelated numeric column.
                if not category:
                    continue
                when = row_day or iso_day(header)
                eye = side(header) or row_side
                units = re.search(r"[\[(]([^\])]+)[\])]", header)
                unit = row_unit or (units.group(1) if units and not side(units.group(1)) else "")
                metric = (next((get(i) for i in item_cols if get(i)), "") or title) if iso_day(header) else norm(header) or title
                # Preserve correction/measurement subtype in the trend key.
                trend = "|".join((category, metric.casefold(), unit.casefold(), eye))
                cells.append({"exam": category, "date": when, "side": eye, "unit": unit,
                              "metric": metric, "raw": value, "value": numeric(value),
                              "trend": trend, "column": ci})
            exams = ({c["exam"] for c in cells} | detected | {title_exam, row_exam}) - {""}
            if not exams:
                continue
            # Transposed tables put dates in columns; make each date selectable
            # and filterable, while retaining the complete source row.
            days = sorted({c["date"] for c in cells}) or [row_day]
            for day in days:
                dated = [c for c in cells if c["date"] == day]
                output.append({"id": digest([source_key, ti, ri, day])[:24], "source": source_key,
                               "saved_at": saved_at, "title": title, "headers": headers,
                               "values": values, "date": day, "exams": sorted(exams), "cells": dated,
                               "unparsed": not dated or not day})
    return output
