"""Read-only surgery counts: schedule rows and eyes are separate quantities."""
from __future__ import annotations

import re
from collections import Counter

from .analysis_numeric import norm
from .sheet_identity import MONTH_PATTERN, header_columns, schema_kind
from .sheet_schema import cell_text

ARCHIVE_CONTRACT_NOTE = "VGHKS_ARCHIVE_CONTRACT_V2"
LEGACY_ARCHIVE_NOTE = "SYSTEM_ANNUAL_SURGERY_ARCHIVE_V1"
_CATA = {"CATA", "CATARACT", "PHACO-IOL", "MSICS-IOL"}


def procedure_components(value):
    """Split explicit combinations, never hyphenated procedure names or words."""
    result = set()
    for part in re.split(r"[+、;\n]+", norm(value).upper()):
        part = re.sub(r"\s*-\s*", "-", part.strip())
        part = re.sub(r"\s+(?:OD|OS|OU)$", "", part).strip()
        if not part:
            continue
        if part == "LENSX-PHACO-IOL":
            result.update(("LenSx", "CATA"))
        elif part == "LENSX":
            result.add("LenSx")
        elif part in _CATA:
            result.add("CATA")
        else:
            result.add(part)
    return sorted(result)


def iol_model(value):
    """One model per row; labelled/multiple-eye values need human review."""
    model = norm(value).upper()
    ambiguous = bool(model and (re.search(r"\b(?:OD|OS|OU)\b", model)
                               or re.search(r"[+/,;、]", model) or "\n" in str(value)))
    return model, ambiguous


def available_months(book):
    months = set()
    for sheet in book.get("sheets", []):
        kind, header = schema_kind(sheet)
        if kind == "month":
            months.add(sheet["properties"]["title"])
        elif kind == "archive":
            try:
                cols = header_columns(sheet, header)
                source, row_type = cols["ArchiveSourceMonth"], cols["ArchiveRowType"]
                for row in sheet.get("rows", [])[header + 1:]:
                    month = cell_text(row[source]).strip() if source < len(row) else ""
                    tag = cell_text(row[row_type]).strip() if row_type < len(row) else ""
                    if tag == "MONTH_HEADER" and MONTH_PATTERN.fullmatch(month):
                        months.add(month)
            except (ValueError, KeyError):
                continue
    return months


def _months(start, end, present):
    first = start[:7].replace("-", "") if start else min(present, default="")
    last = end[:7].replace("-", "") if end else max(present, default="")
    if not first or not last:
        return []
    year, month = int(first[:4]), int(first[4:])
    result = []
    while f"{year:04}{month:02}" <= last:
        result.append(f"{year:04}{month:02}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return result


def _counts(rows, field):
    counted, eyes = Counter(), Counter()
    for row in rows:
        labels = row["procedures"] if field == "procedure" else [row["iol_model"] or "未填"]
        for label in labels:
            counted[label] += 1
            eyes[label] += row["eye_count"] if field == "procedure" else row["iol_eye_count"]
    key = "procedure" if field == "procedure" else "model"
    return [{key: label, "count": count, "eye_count": eyes[label]}
            for label, count in sorted(counted.items())]


def statistics(candidates, book, start="", end=""):
    items, issues, cancelled = [], [], Counter()
    for source in candidates:
        month = source["date"][:7].replace("-", "")
        if source["_cancelled"]:
            cancelled[month] += 1
            continue
        row = {key: value for key, value in source.items() if not key.startswith("_")}
        row["eye_count"] = {"OD": 1, "OS": 1, "OU": 2}.get(norm(row["side"]).upper(), 0)
        row["procedures"] = procedure_components(row["procedure"])
        row["iol_model"], ambiguous = iol_model(row["iol"])
        row["iol_eye_count"] = row["eye_count"] if row["iol_model"] and not ambiguous else 0
        row["review_codes"] = []
        for code, reason, condition in (
            ("unknown_side", "側別不是明確 OD／OS／OU；此排程不納入術式及水晶體眼數。", not row["eye_count"]),
            ("ambiguous_iol", "IOL 含側別或多型號；不猜測雙眼分配，型號眼數暫不計入。", ambiguous),
            ("missing_iol", "白內障排程尚未填寫 IOL 型號。", "CATA" in row["procedures"] and not row["iol_model"]),
            ("missing_procedure", "尚未填寫術式，無法分類。", not row["procedures"]),
            ("missing_report_sync_id", "缺少永久排程 ID；未依病歷號猜測去重。", not row["sync_id"]),
            ("legacy_archive_cancellation", "舊 GAS 封存未保存可靠取消狀態；請以私人備份核對，統計尚不完整。", source.get("_legacy_archive")),
        ):
            if condition:
                row["review_codes"].append(code)
                issues.append({"code": code, "position": row["position"], "month": month, "reason": reason})
        items.append(row)
    items.sort(key=lambda row: (row["date"], row["hospital"], row["position"]))
    present = available_months(book) | {item["date"][:7].replace("-", "") for item in candidates}
    monthly = []
    for month in _months(start, end, present):
        rows = [row for row in items if row["date"][:7].replace("-", "") == month]
        procedures = _counts(rows, "procedure")
        counts = {item["procedure"]: item["eye_count"] for item in procedures}
        available = month in present
        if not available:
            issues.append({"code": "missing_report_month", "month": month, "reason": "尚未取得此月份刀表或封存資料；不視為零筆。"})
        monthly.append({"month": month, "available": available, "total": len(rows),
                        "eye_count": sum(row["eye_count"] for row in rows),
                        "lensx_eye_count": counts.get("LenSx", 0), "cata_eye_count": counts.get("CATA", 0),
                        "excluded_cancelled": cancelled[month],
                        "excluded_unknown_side": sum(not row["eye_count"] for row in rows),
                        "procedures": procedures, "models": _counts(rows, "model"), "items": rows})
    return {"items": items, "total": len(items), "eye_count": sum(row["eye_count"] for row in items),
            "models": _counts(items, "model"), "procedures": _counts(items, "procedure"),
            "monthly": monthly, "excluded_cancelled": sum(cancelled.values()),
            "excluded_unknown_side": sum(not row["eye_count"] for row in items),
            "review_items": [row for row in items if row["review_codes"]], "statistics_issues": issues,
            "count_basis": "count／total 為排程列數；eye_count 為眼數，OD／OS 計 1、OU 計 2。術式可重疊，側別不明及取消列不納入眼數。"}
