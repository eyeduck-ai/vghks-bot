"""Saved report export checks usable by source tests and the portable EXE."""
from __future__ import annotations

import json
from zipfile import ZipFile

from vghks_sdk.models import BinaryAsset, ClinicalOrder

from .analysis_export import archive, listing
from .analysis_fetch import clean
from .analysis_store import digest


def seed_report(workspace, mrn="EXPORT01"):
    store = workspace.analysis.store
    record = {"id": digest([mrn, "export-soap"])[:24], "mrn": mrn, "name": "合成病人",
              "date": "2026-09-11", "case_no": "EX-1", "section_code": "70", "section": "眼科",
              "soap": "A+P: 合成術前摘要\nO: </script><img src=x onerror=alert(1)>",
              "soap_structure": {"assessment_plan": "合成術前摘要", "objective": "合成檢查",
                                 "subjective": "合成主訴", "present_sections": ["AP", "O", "S", "DIAGNOSES"],
                                 "diagnoses": [{"code": "H25", "name": "合成診斷"}]}, "matches": []}
    store.library.save_record(record, workspace.username, "synthetic-export")
    store.save_step(mrn, "cataract-soap", "soap_marker", {"status": "ready", "record_id": record["id"]}, workspace.username)
    store.save_step(mrn, "numeric-history", "numeric", {"mrn": mrn, "tables": [
        {"title": "Va", "headers": ["日期", "OD", "OS"], "rows": [["2026-09-11", "0.6", "0.3"]]},
        {"title": "  vacc  ", "headers": ["日期", "OD", "OS"], "rows": [["2026-09-11", "0.8", "0.9"]]},
        {"title": "CCT", "headers": ["日期", "OD", "OS"], "rows": [["2026-09-11", "550", "560"]]},
    ]}, workspace.username)
    assets = [store.save_asset(mrn, "history-asset:download_pdf:synthetic", BinaryAsset(
        b"%PDF-1.4\nsynthetic-export-only", "application/pdf"), workspace.username),
        store.save_asset(mrn, "history-asset:download_image:synthetic", BinaryAsset(
        b"\xff\xd8\xffsynthetic-jpeg-export-only", "image/jpeg"), workspace.username)]
    orders = [ClinicalOrder(mrn, "EX-1", "O", "DBR, free charge", "2026-09-11", "2026-09-11"),
              ClinicalOrder(mrn, "EX-2", "O", "Keratomery", "2026-08-14", "2026-08-14"),
              ClinicalOrder(mrn, "EX-3", "O", "其他非術前醫囑", "2026-08-13")]
    # Repeated history sources and shared attachment bytes must appear once.
    for kind in ("*", "OR"):
        store.save_step(mrn, "orders-history:" + kind, "order_index", clean(orders), workspace.username)
    for row in workspace.review.history._public_orders(workspace.review.history._groups(mrn, clean(orders))):
        store.save_step(mrn, "history-order:" + row["id"], "history_order", {
            "order": row, "assets": assets, "texts": [{"text": "合成文字報告", "fields": {"結果": "合成正常"}}],
            "details": [{"fields": {"檢查": "合成眼科檢查"}}], "issues": [], "status": "complete"}, workspace.username)
    return workspace.analysis.save_cohort({"source": "manual", "account_id": workspace.account_id,
                                           "name": "合成匯出清單", "mrns": mrn}), assets


def check_saved_export(workspace, other):
    cohort, assets = seed_report(workspace)
    request = {"cohort_id": cohort["id"], "mrns": ["EXPORT01"]}
    assert listing(workspace.analysis, request)["members"][0]["available"]
    with archive(workspace.analysis, request) as (path, name):
        assert name.endswith(".zip")
        with ZipFile(path) as saved:
            manifest = json.loads(saved.read("manifest.json"))
            patient = manifest["patients"][0]
            base = patient["index"].rsplit("/", 1)[0]
            report = json.loads(saved.read(base + "/report.json"))
            assert report["latest_soap"]["record"]["mrn"] == "EXPORT01"
            assert len(report["orders"]) == 2 and len(patient["files"]) == 2
            assert {cell["exam"] for row in report["numeric"] for cell in row["cells"]} == {"Va", "VAcC", "CCT"}
            for asset in assets:
                file = next(row for row in patient["files"] if row["sha256"] == asset["digest"])
                assert saved.read(base + "/" + file["file"]) == workspace.analysis.store.asset(asset["digest"])[0].read_bytes()
            assert b"</script>" not in saved.read(base + "/data.js")
            for file in ("_viewer/soap-view.js", "_viewer/cataract-numeric.js", "_viewer/report.js", "_viewer/report.css"):
                assert saved.read(file)
    assert not path.exists()
    try:
        listing(other.analysis, request)
    except ValueError:
        pass
    else:
        raise AssertionError("another account could read the cohort")
