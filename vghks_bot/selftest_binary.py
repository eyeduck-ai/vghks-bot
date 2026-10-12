"""Real SDK attachment downloads through the platform transport, adapter and cache."""
from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import unquote

import requests
from vghks_sdk import PortalCredentials, RequestPolicy, SDKSettings, VghksSDK
from vghks_sdk.adapters.auth import AppSession
from vghks_sdk.models import ClinicalOrder, PdfAttachmentRef, VisitCase

from .analysis_fetch import clean, order_identity
from .encoding import ClinicalTransport
from .sdk_orders import configure_orders
from .settings import today

MRN = "TEST000"
PDF = b"%PDF-1.4\n1 0 obj\nstream\n<![synthetic-binary[\x00\xff\nendstream\nendobj\n%%EOF\n"
JPEG = b"\xff\xd8<![synthetic-binary[\x00\xff\xff\xd9"
PDF_REF = PdfAttachmentRef(MRN, f"//nfs01p/EMRU/{MRN}/report.pdf")


class BinaryScenario:
    def __init__(self, content=PDF, mime="application/pdf", *, status=200, final_url=""):
        self.content, self.mime, self.status, self.final_url = content, mime, status, final_url
        self.files, self.calls, self.requests, self.connections = {}, [], [], []

    def factory(self, settings):
        policy = RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, max_attempts=1)
        configuration = SDKSettings(portal_base_url="https://synthetic.invalid",
                                    prq_base_url="https://synthetic.invalid/PRQWeb", request_policy=policy)
        session = requests.Session()
        session.request = self.request
        transport = ClinicalTransport(policy=policy, session=session, response_encoding=settings.response_encoding)
        sdk = configure_orders(VghksSDK(settings=configuration, transport=transport,
                                       credentials=PortalCredentials("SYNTHETIC", "SYNTHETIC")))
        auth = sdk._runtime.auth
        auth._portal_authenticated, auth._generation = True, 1
        auth._apps["prq"] = AppSession("prq", "SYNTHETIC-HID", configuration.prq_base_url)
        sdk.auth.check = lambda only=None: SimpleNamespace(ok=True, reauthenticated=False, targets=())
        self.connections.append(sdk)
        return sdk

    def request(self, method, url, **kwargs):
        if not url.startswith("https://synthetic.invalid/"):
            raise AssertionError("binary fixture cannot leave its fake origin")
        filename = unquote(str(kwargs.get("params", {}).get("fileName", ""))).replace("\\", "/").rsplit("/", 1)[-1]
        call = {"method": method, "url": url, "filename": filename}
        self.requests.append(call)
        mime = "text/html; charset=utf-8"
        if url.endswith("QueryPatientRecord.do"):
            content = b'<frameset><frame src="/Page/JSP/KS_Patient.jsp"><frame src="/QueryCaseList.do"></frameset>'
        elif url.endswith("KS_Patient.jsp"):
            content = f'<span id="pHistno">{MRN}</span>'.encode()
        elif url.endswith("QueryCaseList.do"):
            content = b'<div id="typeO"></div>'
        else:
            self.calls.append(call)
            content = self.files.get(filename, self.content)
            mime = self.mime
        response = requests.Response()
        response.status_code, response.url = self.status, self.final_url or url
        response._content = content
        response._content_consumed = True
        response.headers.update({"Content-Type": mime, "Content-Length": str(len(content))})
        return response


def seed_cataract(workspace, refs=(PDF_REF,)):
    case = VisitCase(MRN, today(), "O", "EYE1", "70", "眼科")
    order = clean(ClinicalOrder(mrn=MRN, case_no=case.case_no, case_type="O", name="DBR, free charge",
                               order_date=today().isoformat(), execution_date=today().isoformat(),
                               status="已完成", pdf_refs=tuple(refs)))
    store = workspace.analysis.store
    store.save_step(MRN, "visits", "visits", [clean(case)], workspace.username)
    store.save_step(MRN, "orders-history:*", "order_index", [order], workspace.username)
    return {"id": "selftest-binary-report", "mrn": MRN, "resource": "order_report",
            "reference": order_identity(order), "cohort_id": "synthetic-cataract"}


def check_sdk_binary(directory):
    from .bot import BotApplication
    from .settings import Settings

    scenario = BinaryScenario()
    app = BotApplication(Settings(), directory, scenario.factory)
    try:
        account = app.login({"username": "SYNTHETIC", "password": "SYNTHETIC"})["account"]["id"]
        work = app.workspace(account)
        task = seed_cataract(work)
        with work.gateway.task_context(task["id"], "history"):
            result = work.review.history.execute(task)
        report, _ = work.review.history._stored(MRN, "order_report", task["reference"], cataract=True)
        assert result["status"] == "ready" and len(report["assets"]) == 1
        asset = report["assets"][0]
        path, mime = work.analysis.store.asset(asset["digest"])
        assert path.read_bytes() == PDF and mime == "application/pdf"
        rows = work.analysis.cataract_orders(MRN)
        assert len(rows) == 1 and rows[0]["report_complete"] and rows[0]["asset_count"] == 1
        assert work.review.history.execute(task)["cached"]
        assert len(scenario.calls) == 1 and len(scenario.connections) == 1
    finally:
        app.close()
    # The comparison view remains usable after restarting, without downloading again.
    app = BotApplication(Settings(), directory, scenario.factory)
    try:
        app.offline(account)
        rows = app.workspace(account).analysis.cataract_orders(MRN)
        assert rows[0]["report_complete"] and rows[0]["attachments"][0]["available"]
        assert len(scenario.calls) == 1
    finally:
        app.close()
