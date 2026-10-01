"""Versioned, conservative ophthalmic measurements derived from original SDK rows."""
from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation, localcontext

PARSER_VERSION = 1
REFRACTION = {"驗光-散瞳前", "驗光-散瞳後", "配鏡"}
REF_FIELDS = ("sph", "cyl", "axis", "se")
KM_FIELDS = ("k1", "r1", "axis1", "k2", "r2", "axis2", "cyl", "cyl_axis", "kavg")
NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
REF_PATTERN = re.compile(rf"({NUMBER})\s+({NUMBER})\s*[x×]\s*({NUMBER})", re.I)
KM_PATTERN = re.compile(
    rf"\(?K1\)?\s*({NUMBER})\s+({NUMBER})\s*[x×]\s*({NUMBER})\s+"
    rf"\(?K2\)?\s*({NUMBER})\s+({NUMBER})\s*[x×]\s*({NUMBER})\s+"
    rf"\(?CYL\)?\s*({NUMBER})\s*[x×]\s*({NUMBER})", re.I)


def decimal_value(text):
    try:
        value = Decimal(str(text).replace("−", "-"))
        return value if value.is_finite() and math.isfinite(float(value)) else None
    except (InvalidOperation, OverflowError):
        return None


def axis_value(value):
    return value is not None and value == value.to_integral_value() and 0 <= value <= 180


def structure(exam, cells):
    from .analysis_numeric import norm

    kind = "refraction" if exam in REFRACTION else "keratometry"
    fields = REF_FIELDS if kind == "refraction" else KM_FIELDS
    values = dict.fromkeys(fields)
    conflict = False
    recognized = set()
    for cell in cells:
        raw = norm(cell["raw"]).replace("−", "-")
        match = (REF_PATTERN if kind == "refraction" else KM_PATTERN).fullmatch(raw)
        found = {}
        if match:
            keys = ("sph", "cyl", "axis") if kind == "refraction" else KM_FIELDS[:8]
            found = dict(zip(keys, map(decimal_value, match.groups()), strict=True))
        else:
            # A split field is accepted only when its header names it unambiguously.
            metric = norm(cell["metric"]).casefold()
            metric = re.sub(r"[\[(][^\])]*[\])]", "", metric)
            metric = re.sub(r"\b(?:od|os|ou)\b", "", metric)
            metric = metric.replace(exam.casefold(), "").strip(" /:：")
            metric = re.sub(r"[\s_-]+", "", metric)
            aliases = {"sphere": "sph", "cylinder": "cyl", "ax": "axis"}
            metric = aliases.get(metric, metric)
            if kind == "keratometry" and metric in {"axis", "cylaxis", "axiscyl"}:
                metric = "cyl_axis"
            if metric in fields and metric not in {"se", "kavg"} and re.fullmatch(NUMBER, raw):
                found = {metric: decimal_value(raw)}
        for key, value in found.items():
            if value is None or (key in {"axis", "axis1", "axis2", "cyl_axis"} and not axis_value(value)):
                conflict = True
            if values[key] is not None and values[key] != value:
                conflict = True
            values[key] = value
        if found:
            recognized.add(cell["column"])
    if conflict or len(recognized) != len(cells):
        values = dict.fromkeys(fields)
        status = "unparsed"
    else:
        present = [value for value in values.values() if value is not None]
        with localcontext() as context:
            if present:
                context.prec = max(28, max(value.adjusted() for value in present)
                                   - min(value.as_tuple().exponent for value in present) + 4)
            if kind == "refraction" and values["sph"] is not None and values["cyl"] is not None:
                values["se"] = values["sph"] + values["cyl"] / 2
            if kind == "keratometry" and values["k1"] is not None and values["k2"] is not None:
                values["kavg"] = (values["k1"] + values["k2"]) / 2
        status = "parsed" if all(value is not None for value in values.values()) else "partial"
    return {"kind": kind, "status": status,
            "values": {key: float(value) if value is not None else None for key, value in values.items()},
            "exact": {key: format(value, "f") if value is not None else None for key, value in values.items()}}


def enrich_rows(rows):
    """Preserve every source row and side; never combine different observations."""
    for row in rows:
        groups = {}
        measurements = []
        for cell in row["cells"]:
            if cell["exam"] in REFRACTION or cell["exam"] == "KM":
                groups.setdefault((cell["exam"], cell["side"]), []).append(cell)
            else:
                parsed = {"kind": "scalar", "status": "parsed" if cell["value"] is not None else "text",
                          "values": {"value": cell["value"]}, "exact": {"value": None}}
                cell["parsed"] = parsed
                measurements.append({"exam": cell["exam"], "side": cell["side"], "date": cell["date"],
                                     "unit": cell["unit"], "raw": [cell["raw"]], **parsed})
        for (exam, eye), cells in groups.items():
            parsed = structure(exam, cells)
            for cell in cells:
                cell["parsed"] = parsed
            measurements.append({"exam": exam, "side": eye, "date": row["date"],
                                 "unit": cells[0]["unit"], "raw": [cell["raw"] for cell in cells], **parsed})
        row["measurements"] = measurements
        row["parser_version"] = PARSER_VERSION
    return rows
