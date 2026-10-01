"""Offline EXE regression: retain and replay a failing clinical response."""
import io
import json
import zipfile
from types import SimpleNamespace

from vghks_sdk import ParseError
from vghks_sdk.parsing.clinical import parse_clinical_orders

from .diagnostics import analysis_query_context, failure_details


def check_order_index_evidence(workspace):
    recorder = workspace.diagnostics.recorder()
    body = b'''<script>var orderStr=unknownExpression("synthetic clinical value");
    new KSCase("",orderStr,"2026-09-11","2026-09-11","","");</script>'''
    try:
        for category in ("*", "OR"):
            operation = recorder.start_operation(name="get_order_history", app_key="prq")
            request = recorder.record_http_request(method="POST",
                url="https://synthetic.invalid/PRQWeb/QueryOrderResult.do?hid=SECRET",
                attempt=1, max_attempts=2, throttle_delay_seconds=0, tls_verification_enabled=True,
                kwargs={"data": {"password": "SECRET", "ordertype": category}})
            response = SimpleNamespace(content=body, status_code=200, headers={"Content-Type": "text/html"},
                                       url="https://synthetic.invalid/PRQWeb/QueryOrderResult.do", history=[])
            recorder.record_http_response(request_id=request, response=response, elapsed_seconds=.01)
            try:
                parse_clinical_orders(body.decode(), mrn="TEST001")
            except ParseError as exc:
                recorder.finish_operation(operation_id=operation, name="get_order_history", status="ERROR", exc=exc)
                workspace.diagnostics.save({"task_id": "selftest-order-index", "mrn": "TEST001",
                    "phase": "orders.get_order_history", "sdk_run_id": recorder.run_id,
                    "query": analysis_query_context("orders-history:" + category, "order_index"),
                    "transport": recorder.failure_transport(exc), "error": failure_details(exc)})
            else:
                raise AssertionError("unsupported synthetic expression must fail closed")
        saved = workspace.diagnostics.query({"task_id": "selftest-order-index"})
        assert len(saved["failures"]) == 2
        assert {row["query"]["category"] for row in saved["failures"]} == {"*", "OR"}
        assert saved["contains_raw_response"] and saved["contains_medical_values"]
        assert "synthetic clinical value" in json.dumps(saved)
        assert "SECRET" not in json.dumps(saved)
        with zipfile.ZipFile(io.BytesIO(workspace.diagnostics.export({"task_id": "selftest-order-index"}))) as archive:
            sources = [archive.read(name).decode() for name in archive.namelist() if name.endswith(".html")]
            assert len(sources) == 2 and all(source == body.decode() for source in sources)
            for source in sources:
                try:
                    parse_clinical_orders(source, mrn="TEST001")
                except ParseError as exc:
                    assert exc.info.code == "JS_EXPRESSION_UNSUPPORTED"
                else:
                    raise AssertionError("saved response must reproduce the parse error")
    finally:
        recorder.finalize(command="selftest-order-index", status="ERROR", exit_code=0)
    assert json.loads(recorder.summary_path.read_text())["contains_clinical_response"]
