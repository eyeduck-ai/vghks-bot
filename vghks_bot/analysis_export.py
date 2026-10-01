"""Portable, offline cataract reports assembled only from account-owned saved data."""
from __future__ import annotations

import copy
import csv
import io
import json
import tempfile
from contextlib import contextmanager
from html import escape
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from .analysis_numeric import CATARACT_NUMERIC_EXAMS
from .analysis_store import digest
from .downloads import filename
from .settings import timestamp

STATIC = Path(__file__).with_name("static")


def _members(analysis, values):
    cohort = analysis.store.document("analysis_cohorts", values.get("cohort_id"))
    requested = values.get("mrns", [values["mrn"]] if values.get("mrn") else None)
    if requested is not None and (not isinstance(requested, list) or not requested
                                  or len(requested) > 500
                                  or any(not isinstance(mrn, str) for mrn in requested)):
        raise ValueError("請選擇一至 500 位已保存資料的病人。")
    account = getattr(analysis.app, "account_id", None)
    available = {member["mrn"]: member for member in cohort["members"]
                 if account is None or member.get("account_id") == account}
    if requested is not None and set(requested) - available.keys():
        raise ValueError("部分病人不在此清單或不屬於目前帳號。")
    return cohort, [member for member in cohort["members"] if member["mrn"] in available
                    and (requested is None or member["mrn"] in requested)]


def _snapshot(analysis, cohort, member):
    result = analysis.results({"cohort_id": cohort["id"], "mrn": member["mrn"],
                               "module": "cataract"})
    result["member"] = {"mrn": member["mrn"], "name": member.get("name", "")}
    for order in result["orders"]:
        read = analysis.app.review.history.read({"cohort_id": cohort["id"], "mrn": member["mrn"],
                                                 "resource": "order_report", "reference": order["id"]})
        saved = read["data"] or {}
        # Export only display data; SDK references, intranet paths and credentials stay local.
        order["texts"] = [{"text": row.get("text", ""), "fields": row.get("fields", {})}
                          for row in saved.get("texts", [])]
        order["details"] = [{"fields": row.get("fields", {})} for row in saved.get("details", [])]
        order["issues"] = [{"message": row.get("message", "")}
                           for row in saved.get("issues", [])]
    return copy.deepcopy(result)


def _has_data(result):
    return bool(result.get("latest_soap", {}).get("record") or result["numeric"]
                or any(order["report_loaded"] for order in result["orders"])
                or result.get("coverage")
                or result.get("latest_soap", {}).get("status") in {"missing", "no_visit"})


def listing(analysis, values):
    cohort, members = _members(analysis, {"cohort_id": values.get("cohort_id")})
    rows = []
    for member in members:
        result = _snapshot(analysis, cohort, member)
        rows.append({**result["member"], "available": _has_data(result),
                     "soap": bool(result["latest_soap"]["record"]),
                     "numeric_rows": len(result["numeric"]),
                     "orders": sum(order["report_loaded"] for order in result["orders"]),
                     "attachments": sum(asset["available"] for order in result["orders"]
                                        for asset in order["attachments"]),
                     "saved_at": result["updated_at"]})
    return {"members": rows}


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def _script(value):
    return "window.EXPORT_REPORT = " + _json(value).replace("<", "\\u003c").replace(
        ">", "\\u003e").replace("&", "\\u0026").replace("\u2028", "\\u2028").replace(
        "\u2029", "\\u2029") + ";\n"


def _text_report(order):
    parts = [" · ".join(str(order.get(key) or "") for key in ("date", "name", "case_no"))]
    for label, rows in (("文字報告", order["texts"]), ("醫囑明細", order["details"])):
        for row in rows:
            parts.append("\n" + label)
            if row.get("text"):
                parts.append(str(row["text"]))
            parts.extend(f"{key}：{value}" for key, value in row.get("fields", {}).items())
    return "\n".join(parts) + "\n"


def _csv(rows):
    content = io.StringIO(newline="")
    writer = csv.writer(content)
    parsed_fields = ("sph", "cyl", "axis", "se", "k1", "r1", "axis1", "k2", "r2", "axis2", "cyl_axis", "kavg")
    writer.writerow(["檢查", "日期", "眼別", "項目", "數值", "單位", *parsed_fields])
    for exam in CATARACT_NUMERIC_EXAMS:
        for row in sorted(rows, key=lambda item: item.get("date", ""), reverse=True):
            for cell in row.get("cells", []):
                if cell["exam"] == exam:
                    values = [exam, cell.get("date", ""), cell.get("side", ""),
                              cell.get("metric", ""), cell.get("raw", ""), cell.get("unit", "")]
                    # CSV is also intended for Excel; keep clinical strings as text.
                    parsed = cell.get("parsed", {}).get("exact", {})
                    writer.writerow(["'" + str(value) if str(value).startswith(("=", "+", "-", "@"))
                                     else value for value in values] + [parsed.get(key) for key in parsed_fields])
    return "\ufeff" + content.getvalue()


def _page(title, prefix="../_viewer/"):
    return f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><link rel="stylesheet" href="{prefix}report.css">
<script defer src="{prefix}soap-view.js"></script>
<script defer src="{prefix}cataract-numeric.js"></script>
<script defer src="data.js"></script><script defer src="{prefix}report.js"></script>
</head><body><main id="report"><p>正在開啟已保存報告…</p></main>
<noscript>請啟用 JavaScript，或查看同目錄的 report.json、SOAP.txt、numeric.csv 與 orders 資料夾。</noscript>
</body></html>'''


@contextmanager
def archive(analysis, values):
    cohort, members = _members(analysis, values)
    if values.get("mrns") is None and not values.get("mrn"):
        raise ValueError("請指定要匯出的病人。")
    results = [_snapshot(analysis, cohort, member) for member in members]
    if any(not _has_data(result) for result in results):
        raise ValueError("部分病人尚無已保存的 SOAP、數值或醫囑報告，請重新選取。")
    exported_at = timestamp()
    label = results[0]["member"]["mrn"] if len(results) == 1 else f'{len(results)}位病人'
    name = filename(f"白內障術前報告-{label}-{exported_at[:10]}", "application/zip")
    with tempfile.TemporaryDirectory(prefix="vghks-report-") as folder:
        path = Path(folder) / "report.zip"
        manifest = {"format": 1, "exported_at": exported_at, "patients": []}
        with ZipFile(path, "w", ZIP_DEFLATED) as output:
            for asset_name in ("soap-view.js", "cataract-numeric.js", "report.js", "report.css"):
                source = "export-" + asset_name if asset_name.startswith("report.") else asset_name
                output.write(STATIC / source, "_viewer/" + asset_name)
            for result in results:
                member = result["member"]
                # Directories do not contain unchecked identifiers or patient-supplied paths.
                directory = "patient-" + digest(member["mrn"])[:12]
                result["exported_at"] = exported_at
                result["files"] = []
                result["missing"] = []
                exported = {}
                for order in result["orders"]:
                    order_dir = "orders/" + order["id"][:20]
                    text_path = order_dir + "/report.txt"
                    order["text_file"] = text_path
                    output.writestr(directory + "/" + text_path, _text_report(order).encode("utf-8-sig"))
                    if not order["report_loaded"] or not order["report_complete"]:
                        result["missing"].append(f'{order["date"]} · {order["name"]}：報告或附件尚未完整保存')
                    for index, asset in enumerate(order["attachments"]):
                        sha = asset["digest"]
                        asset["file"] = None
                        if sha in exported:
                            asset["file"] = exported[sha]
                            continue
                        try:
                            with analysis.store.library.connect() as db:
                                owned = db.execute("SELECT 1 FROM analysis_asset_refs WHERE mrn=? AND digest=?",
                                                   (member["mrn"], sha)).fetchone()
                            if not owned:
                                raise ValueError("附件不屬於此病人。")
                            source, mime = analysis.store.asset(sha)
                            target = "attachments/" + filename(
                                f'{order["date"][:10]} {order["name"]} {index + 1}-{sha[:12]}', mime)
                            output.write(source, directory + "/" + target)
                        except (ValueError, FileNotFoundError):
                            result["missing"].append(f'{order["name"]}：檔案 {index + 1} 未保存或已刪除')
                            continue
                        asset["file"] = exported[sha] = target
                        result["files"].append({"file": target, "mime": mime, "sha256": sha,
                                                "size": source.stat().st_size})
                soap = result["latest_soap"].get("record")
                if not soap:
                    result["missing"].append("尚無已保存的眼科 SOAP")
                if analysis.store.step(member["mrn"], "numeric-history") is None:
                    result["missing"].append("歷年數值尚未保存")
                if any(analysis.store.step(member["mrn"], "orders-history:" + kind) is None
                       for kind in ("*", "OR")):
                    result["missing"].append("歷年醫囑索引尚未完整保存")
                output.writestr(directory + "/SOAP.txt", (soap.get("soap", "") if soap else
                                 "尚無已保存的眼科 SOAP").encode("utf-8-sig"))
                output.writestr(directory + "/numeric.csv", _csv(result["numeric"]).encode("utf-8"))
                output.writestr(directory + "/report.json", _json(result).encode("utf-8"))
                output.writestr(directory + "/data.js", _script(result).encode("utf-8"))
                output.writestr(directory + "/index.html", _page(member["name"] + " " + member["mrn"]))
                manifest["patients"].append({**member, "index": directory + "/index.html",
                                             "missing": result["missing"], "files": result["files"]})
            links = "".join(f'<li><a href="{row["index"]}">{escape(row["name"])} '
                            f'{escape(row["mrn"])}</a> · {len(row["files"])} 份附件'
                            f'{" · 有未保存資料" if row["missing"] else ""}</li>'
                            for row in manifest["patients"])
            output.writestr("index.html", '<!doctype html><html lang="zh-Hant"><meta charset="utf-8">'
                           '<meta name="viewport" content="width=device-width,initial-scale=1">'
                           '<title>白內障術前報告</title><link rel="stylesheet" href="_viewer/report.css">'
                           f'<main><h1>白內障術前報告</h1><p>匯出時間 {escape(exported_at)}</p>'
                           f'<p>解壓縮後可離線開啟；附件保留原始格式。此為已保存資料快照。</p><ul>{links}</ul></main></html>')
            output.writestr("manifest.json", _json(manifest).encode("utf-8"))
            output.writestr("README.txt", ("先解壓縮整個 ZIP，再開啟 index.html。\n"
                             "報告可離線檢閱；SOAP.txt、numeric.csv、report.json 與醫囑 report.txt 可另行使用。\n"
                             "attachments 內含已保存的原始 PDF／JPG／PNG／GIF，匯出不會啟動院內爬蟲。\n"
                             "本檔為匯出時的本機快照；缺漏詳見各病人報告及 manifest.json。\n").encode("utf-8-sig"))
        yield path, name
