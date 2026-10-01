"""Exercise clinical cache clearing in both source and frozen offline runtimes."""
from vghks_sdk.models import BinaryAsset


def check_clinical_clearing(workspace, other):
    service, store = workspace.library_data, workspace.analysis.store
    asset = BinaryAsset(b"%PDF-1.4\nsynthetic shared cache", "application/pdf")
    saved = store.save_asset("CACHEONLY", "history-asset:download_pdf:1", asset, "TEST")
    store.save_asset("CACHEONLY", "scan-asset:1", asset, "TEST")
    store.save_step("CACHEONLY", "numeric-history", "numeric", [], "TEST")
    store.save_step("CACHEONLY", "numeric-history", "numeric", {"tables": []}, "TEST")
    row = service.read({"q": "CACHEONLY"})["patients"][0]
    assert row["attachment_bytes"] == len(asset.content)
    assert row["categories"]["numeric"]["versions"] == 2
    assert other.library_data.read({"q": "CACHEONLY"})["patients"] == []
    request = {"mrns": ["CACHEONLY"], "categories": ["numeric", "orders"]}
    preview = service.preview(request)
    assert preview["attachments"] == 0 and "analysis" in preview["cascaded"]
    service.delete({**request, "fingerprint": preview["fingerprint"]})
    assert store.asset(saved["digest"])[0].is_file()
    assert not store.step("CACHEONLY", "numeric-history")
    assert all(step["key"] != "numeric-history" for step in store.raw_data("CACHEONLY")["versions"])
    request["categories"] = ["scans"]
    preview = service.preview(request)
    assert preview["attachments"] == 1
    service.delete({**request, "fingerprint": preview["fingerprint"]})
    assert not (store.assets / saved["digest"]).exists()
