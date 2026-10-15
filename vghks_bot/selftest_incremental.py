"""Exercise review ancestry and SDK report references inside the frozen EXE."""
import html
import json
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import urlencode

from vghks_sdk.models import (
    OrderHistoryFilter,
    OrderReportRef,
    PacsImageRef,
    PacsStudyRef,
    VisitCase,
    to_jsonable,
)
from vghks_sdk.parsing.clinical import parse_order_report, parse_pacs_study

from .analysis_fetch import model
from .bot import BotApplication
from .sdk_orders import CompatibleOrderAdapter, CompatibleOrdersService, assess_order_index
from .settings import Settings, today


def check_incremental_reviews(path):
    from .selftest_bot import BotSyntheticSDK, wait_task

    class CurrentSDK(BotSyntheticSDK):
        calls = []

        def visits(self, mrn):
            return [replace(case, visit_date=today()) if case.case_no in {"ONE", "TWO"} else case
                    for case in super().visits(mrn)]

    app = BotApplication(Settings(), path, CurrentSDK)
    try:
        account = app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        work = app.workspace(account)
        wait_task(work, work.review.start({"kind": "resolve", "identifiers": "TEST001 TEST002 TEST003"})["task_id"])
        group = work.review.save_set({"mrns": "TEST001 TEST002"})
        base = wait_task(work, work.review.start({"set_id": group["id"]})["task_id"])
        assert base["status"] == "completed"
        work.review.save_note({"task_id": base["id"], "mrn": "TEST001", "text": "synthetic copied note"})
        with work.store.library.connect() as db:
            db.execute("UPDATE records SET updated_at='2000-01-01T00:00:00+08:00'")
            db.execute("UPDATE bot_documents SET payload=json_set(payload,'$.updated_at',?),updated_at=? WHERE kind='visits'",
                       ("2000-01-01T00:00:00+08:00", "2000-01-01T00:00:00+08:00"))
        group = work.review.save_set({"mrns": "TEST001 TEST002 TEST003"})
        CurrentSDK.calls.clear()
        child = wait_task(work, work.review.start({"set_id": group["id"], "base_task_id": base["id"]})["task_id"])
        assert child["status"] == "completed" and child["reused_count"] == 2
        assert CurrentSDK.calls and all(call[2] == "TEST003" for call in CurrentSDK.calls)
        inherited = next(row for row in child["items"] if row["mrn"] == "TEST001")
        original = next(row for row in base["items"] if row["mrn"] == "TEST001")
        assert inherited["checked_at"] == original["checked_at"] and inherited["source_task_id"] == base["id"]
        work.review.save_note({"task_id": child["id"], "mrn": "TEST001", "text": "independent edit"})
        assert work.review.read_notes({"task_id": base["id"]})["patients"][0]["text"] == "synthetic copied note"
        CurrentSDK.calls.clear()
        single = wait_task(work, work.review.start({"base_task_id": child["id"], "refresh_mrn": "TEST002"})["task_id"])
        assert single["status"] == "completed" and single["cutoff"] == child["cutoff"]
        assert CurrentSDK.calls and all(call[2] == "TEST002" for call in CurrentSDK.calls)
        assert any(call[1] == "index" for call in CurrentSDK.calls) and any(call[1] == "soap" for call in CurrentSDK.calls)
        CurrentSDK.calls.clear()
        assert wait_task(work, work.review.start({"resume": single["id"]})["task_id"])["status"] == "completed"
        assert not CurrentSDK.calls
    finally:
        app.close()


def check_sdk_report_contract():
    image_url = ("http://192.168.20.137:800/xwado/PushWADO?requestType=WADO&"
                 "contentType=image%2Fjpeg&rows=512&objectUID=1.2.3")
    requests = []
    def binary(spec, url, **options):
        requests.append(options["params"])
        return b"\xff\xd8synthetic\xff\xd9", "image/jpeg"
    source = "/PRQWeb/Page/JSP/showPACSPic.jsp?" + urlencode({
        "hhisnum": "TEST001", "hid": "OLD", "reqno": "SYNTHETIC", "imgUrl": image_url})
    runtime = SimpleNamespace(settings=SimpleNamespace(prq_base_url="https://hospital.test/PRQWeb"),
        auth=SimpleNamespace(hid_for=lambda _: "CURRENT"),
        request_text=lambda *args, **kwargs: '<img src="' + html.escape(source) + '">',
        request_binary=binary, execute=lambda spec, operation, **kwargs: operation())
    adapter = CompatibleOrderAdapter(runtime)
    study = adapter.get_pacs_study(PacsStudyRef("TEST001", "SYNTHETIC"))
    reference = model(PacsImageRef, json.loads(json.dumps(to_jsonable(study.images[0]))))
    assert reference.img_url == image_url
    adapter.download_pacs_image(reference)
    adapter.download_pacs_image(PacsImageRef("TEST001", "SYNTHETIC", "SERIES", "STUDY", "1.2.3"))
    assert requests[0]["imgUrl"] == image_url and requests[1]["uid"] == "1.2.3"
    assert parse_pacs_study("查無資料!!", reference=study.reference).data_status == "EMPTY"
    script = r'resVal="First\nSecond"; while(resVal.indexOf("\n")>0){resVal=resVal.replace("\n","<br/>");} document.write(resVal);'
    report = parse_order_report('<table><tr><th>報告內容</th><td><pre><script>' + script + '</script></pre></td></tr></table>',
                                reference=OrderReportRef("TEST001", "SYNTHETIC", "O", "1"))
    assert report.report_text == "First\nSecond" and not report.text_extraction_notes
    def order_row(mrn, case_no):
        name = '<a href="/PRQWeb/QueryOrderDetail.do?hhisnum=' + mrn + '&caseType=O&caseNo=' + case_no + '&seqNo=1">Synthetic order</a>'
        return '<script>new KSCase("",' + json.dumps(name) + ',"2026-10-01","","","","");</script>'
    page = order_row("TEST001", "CURRENT") + order_row("OLD001", "LEGACY") + order_row("OTHER001", "FOREIGN")
    runtime.request_text = lambda *args, **kwargs: page
    adapter._history_context_raw = lambda _: None
    verified = VisitCase("OLD001", today(), "O", "LEGACY", "70", "眼科", lookup_mrn="TEST001")
    orders = CompatibleOrdersService(adapter).get_order_history("TEST001", OrderHistoryFilter(), verified_cases=[verified])
    assert {order.mrn for order in orders} == {"TEST001", "OLD001"}
    assert all(order.mrn == order.detail_ref.mrn for order in orders)
    assert len(orders.issues) == 1 and assess_order_index(orders).complete is False
