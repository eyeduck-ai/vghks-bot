"""Preserve report structure and values without guessing payroll semantics."""
import csv
import io
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup
from vghks_sdk.core.errors import ParseError
from vghks_sdk.core.jsliteral import static_document_writes


def month_value(value):
    text = unicodedata.normalize("NFKC", value).strip()
    match = re.fullmatch(r"(\d{3,4})[-/]?(\d{2})", text)
    if not match:
        return ""
    year, month = int(match[1]), int(match[2])
    year += 1911 if len(match[1]) == 3 else 0
    return f"{year:04}-{month:02}" if 1912 <= year <= 2200 and 1 <= month <= 12 else ""


def numeric_cell(raw):
    text = unicodedata.normalize("NFKC", raw).strip()
    unit = ""
    for token in ("NT$", "NTD", "$"):
        if text.startswith(token):
            text, unit = text[len(token):].strip(), token
            break
    if text.endswith("%"):
        text, unit = text[:-1].strip(), "%"
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    if not re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", text):
        return {"number": None, "unit": ""}
    # Leading zero identifiers, dates and mixed-unit text remain text.
    compact = text.replace(",", "")
    if re.match(r"[+-]?0\d", compact):
        return {"number": None, "unit": unit}
    try:
        number = Decimal(compact) * (-1 if negative else 1)
        return {"number": str(number), "unit": unit}
    except InvalidOperation:
        return {"number": None, "unit": unit}


def normalize_document(document):
    soup = BeautifulSoup(document.html, "html.parser")
    tables, notes = [], list(document.rendering_notes)
    # Use the same static literal reader as the SDK: many MIS reports write
    # cells with JavaScript. Expand literals without ever executing scripts.
    for script in list(soup.find_all("script")):
        if not script.get("src"):
            try:
                markup = static_document_writes(script.string or script.get_text(), allow_unknown_branches=True)
                script.insert_before(BeautifulSoup(markup, "html.parser"))
            except ParseError as exc:
                notes.append(exc.info.code)
        script.decompose()
    for table in soup.find_all("table"):
        rows = [r for r in table.find_all("tr") if r.find_parent("table") is table]
        cells, occupied, grid = [], {}, []
        for r, row in enumerate(rows):
            column = 0
            for cell in row.find_all(["td", "th"], recursive=False):
                while (r, column) in occupied:
                    column += 1
                own = BeautifulSoup(str(cell), "html.parser")
                for nested in own.find_all(["table", "script", "style"]):
                    nested.decompose()
                raw = own.get_text(" ", strip=True)
                spans = []
                for attr in ("rowspan", "colspan"):
                    try:
                        span = int(cell.get(attr, 1))
                    except (ValueError, TypeError):
                        span = 1
                        notes.append("無法解析合併儲存格")
                    if not 1 <= span <= 200:
                        span = 1
                        notes.append("合併儲存格超出支援範圍")
                    spans.append(span)
                item = {"row": r, "column": column, "rowspan": spans[0], "colspan": spans[1],
                        "header": cell.name == "th", "raw": raw, **numeric_cell(raw)}
                if any((rr, cc) in occupied for rr in range(r, r+spans[0]) for cc in range(column, column+spans[1])):
                    notes.append("儲存格位置重疊，請核對原始文字")
                cells.append(item)
                for rr in range(r, r+spans[0]):
                    for cc in range(column, column+spans[1]):
                        occupied[(rr, cc)] = raw
                column += spans[1]
        if not any(c["raw"] for c in cells):
            continue
        width = max((column for _, column in occupied), default=-1) + 1
        height = max((row for row, _ in occupied), default=-1) + 1
        if width * height > 500000:
            raise ValueError("報表表格過大，請縮小院方提供的查詢範圍。")
        grid = [[occupied.get((r, c), "") for c in range(width)] for r in range(height)]
        tables.append({"id": str(table.get("id", "")), "index": len(tables), "cells": cells, "grid": grid})
    # Some SDK fixtures/reports expose extracted tables without HTML tables.
    if not tables:
        for source in document.tables:
            cells = [{"row": r, "column": c, "rowspan": 1, "colspan": 1, "header": False,
                      "raw": value, **numeric_cell(value)} for r, row in enumerate(source.rows) for c, value in enumerate(row)]
            tables.append({"id": source.identifier, "index": len(tables), "cells": cells,
                           "grid": [list(row) for row in source.rows]})
    if not tables:
        raise ValueError("薪資報表沒有可保存的表格。")
    return {"tables": tables, "text": document.text, "html": document.html,
            "sdk_tables": [{"id": t.identifier, "rows": [list(r) for r in t.rows]} for t in document.tables],
            "notes": list(dict.fromkeys(notes))}


def report_content(payload):
    """Comparable report facts; generated HTML IDs/scripts are not revisions."""
    return {"text": re.sub(r"\s+", " ", payload.get("text", "")).strip(),
            "tables": [[{k: cell.get(k) for k in ("row", "column", "rowspan", "colspan", "header", "raw")}
                        for cell in table["cells"]] for table in payload["tables"]]}


def csv_cell(value):
    value = "" if value is None else str(value)
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        # Keep original strings in JSON; CSV is safe to open as a spreadsheet.
        return "'" + value
    return value


def export_csv(reports):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["報表類型", "院內期別", "月份", "取得時間", "版本", "表格", "列", "欄",
                     "合併列數", "合併欄數", "表頭", "原文", "可解析數值", "單位"])
    for report in reports:
        for table in report["payload"]["tables"]:
            for cell in table["cells"]:
                values = [report["kind"], report["period"], report["month"], report["fetched_at"], report["id"],
                          table["index"]+1, cell["row"]+1, cell["column"]+1, cell["rowspan"], cell["colspan"],
                          cell["header"], cell["raw"], cell["number"], cell["unit"]]
                writer.writerow([csv_cell(v) if i != 12 else (v if v is not None else "") for i, v in enumerate(values)])
    return "\ufeff" + stream.getvalue()
