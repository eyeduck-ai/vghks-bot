"""Portable binary checks for persisted numerical parsing and background scheduling."""
import time


def check_numeric_prefetch(workspace):
    check_order_compatibility()
    analysis = workspace.analysis
    cohort = analysis.save_cohort({"source": "manual", "account_id": workspace.account_id,
                                   "name": "合成背景數值驗證", "mrns": "CATA628A CATA628B"})
    queue = analysis.cataract_queue
    now = [100.0]
    queue.clock = lambda: now[0]
    queue.delay = lambda *_: 12

    def wait(predicate):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.01)
        raise AssertionError("synthetic prefetch did not finish")

    queue.handle({"cohort_id": cohort["id"], "action": "start", "mrn": "CATA628A"})
    wait(lambda: queue.snapshot()["state"] == "waiting")
    assert queue.snapshot()["pending"] == ["CATA628B"]
    queue.handle({"cohort_id": cohort["id"], "action": "prioritize", "mrn": "CATA628B"})
    wait(lambda: queue.snapshot()["state"] == "completed")
    assert workspace.idle.wait(2)
    payload = {"mrn": "CATA628A", "tables": [
        {"title": "驗光-散瞳前", "headers": ["日期", "OD", "OS"],
         "rows": [["2026-09-11", "-1.25 -1.00 X 170", "-2.25 -1.00 X 175"]]},
        {"title": "KM", "headers": ["日期", "OD"], "rows": [["2026-09-11",
         "K1 41.25 8.20 X 160 K2 42.50 7.96 X 70 CYL -1.25 X 160"]]},
    ]}
    analysis.store.save_step("CATA628A", "numeric-history", "numeric", payload, workspace.username)
    rows = analysis.store.numeric_rows(analysis.store.step("CATA628A", "numeric-history"))
    assert rows[0]["measurements"][0]["values"]["se"] == -1.75
    assert rows[1]["measurements"][0]["values"]["kavg"] == 41.875
    assert analysis.store.step("CATA628A", "numeric-structured:numeric-history")
    # The frozen executable shares the same bounded local snapshot/cache tokens.
    values = {"cohort_id": cohort["id"], "mrn": "CATA628A", "module": "cataract"}
    result = analysis.results(values)
    status = analysis.cataract_status(values)
    summary = next(row for row in status["members"] if row["mrn"] == "CATA628A")
    assert result["data_revision"] == summary["data_revision"]
    assert len(result["data_revision"]) == 64
    with analysis.store.library.batch() as connection:
        with analysis.store.library.connect() as nested:
            assert nested is connection
        assert analysis.store.step("CATA628A", "numeric-history")


def check_order_compatibility():
    import json

    from vghks_sdk.models import PdfAttachmentRef, to_jsonable

    from .analysis_fetch import model
    from .sdk_orders import parse_order_index

    path = r"\\hfs01_1A0.vghks.gov.tw\SECTORD\lightrep\TEST001_O_DOCUMENT1.pdf"
    page = """<script>
var orderStr = '<a href="/PRQWeb/QueryOrderDetail.do?hhisnum=TEST001&caseNo=EYE1&caseType=O&seqNo=1">光照治療</a>';
var mydate = '2026-09-01'; var rcpDt = '2026-09-02'; var qrcodeStr = '';
var filepath = PATH;
qrcodeStr += '<button class="attachBtn" path="'+filepath+'">光照治療紀錄</button>';
new KSCase('', orderStr, mydate, rcpDt, '', '已執行', qrcodeStr);
</script>""".replace("PATH", json.dumps(path))
    orders = parse_order_index(page, mrn="TEST001")
    assert len(orders) == 1 and orders[0].name == "光照治療"
    ref = orders[0].pdf_refs[0]
    assert model(PdfAttachmentRef, to_jsonable(ref)).file_path == path
