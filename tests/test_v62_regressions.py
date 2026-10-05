"""Regression coverage for observations from the hospital acceptance run."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vghks_sdk import LoginRejectedError, ParseError, RequestError
from vghks_sdk.adapters.prq import PrqAdapter
from vghks_sdk.core.errors import error_info
from vghks_sdk.parsing.documents import parse_document
from vghks_sdk.services.records import RecordsService

from vghks_bot.analysis_fetch import clean
from vghks_bot.bot import BotApplication
from vghks_bot.bot_gateway import AccountGateway
from vghks_bot.clinical_identity import doctor_identity, verified_visit_cases
from vghks_bot.monitoring import interval
from vghks_bot.selftest_bot import BotSyntheticSDK, wait_task
from vghks_bot.settings import Settings


class ContextTests(unittest.TestCase):
    def test_doctor_suffix_keeps_prefix_and_complete_number(self):
        for original, expected in [("AB42B", "AB42"), ("C123F", "C123"), ("AB420", "AB420"),
                                   (" ab42bc ", "AB42"), ("AAB42", "AAB42"), ("ABC", "ABC")]:
            self.assertEqual(doctor_identity(original), expected)
        self.assertNotEqual(doctor_identity("AB42B"), doctor_identity("AB420"))

    def runtime(self, headers, indices, context_html="", review_response=None):
        calls = []
        headers, indices = iter(headers), iter(indices)
        def request(spec, url, **kwargs):
            calls.append(url.rsplit("/", 1)[-1])
            if url.endswith("QueryPatientRecord.do"):
                return context_html or '<frameset><frame src="/Page/JSP/KS_Patient.jsp"><frame src="/QueryCaseList.do"></frameset>'
            if url.endswith("EMRProcess.do"):
                runtime.operation_write_attempted = True
                runtime.review_payload = kwargs["data"]
                return (review_response if review_response is not None else
                    '<frameset><frame src="/Page/JSP/KS_Patient.jsp"><frame src="/QueryCaseList.do"></frameset>')
            if url.endswith("KS_Patient.jsp"):
                return '<span id="pHistno">' + next(headers) + '</span>'
            return next(indices)
        runtime = SimpleNamespace(settings=SimpleNamespace(prq_base_url="https://synthetic.invalid/prq"),
            auth=SimpleNamespace(hid_for=lambda _: "SECRET_AUTH"), request_text=request,
            execute=lambda spec, operation, **kw: operation(), operation_write_attempted=False)
        return SimpleNamespace(_runtime=runtime, records=RecordsService(PrqAdapter(runtime))), calls

    def review_form(self):
        hidden = {"reqCode": "saveAccessCause", "value(status)": "01",
            "value(smr_hid)": "SECRET_AUTH", "value(smr_hhisnum)": "TEST001",
            "value(smr_Flg)": "Case1", "value(inCaseFlg)": "N",
            "value(bgnDt)": "2026-01-01", "value(endDt)": "2026-12-31",
            "value(causeOther1)": ""}
        return ('<form id="addForm" method="post" action="../../../EMRProcess.do">'
            + ''.join(f'<input type="hidden" name="{key}" value="{value}">' for key, value in hidden.items())
            + '<input type="checkbox" name="valueA(cause)" value="1A">了解病情</form>')

    def index(self, mrn):
        return '<div id="typeO"></div><script>var url="QueryCaseDetail.do?hhisnum=' + mrn + '&amp;caseDT=2026-09-21&amp;caseType=O&amp;caseNo=1&amp;caseSec=70";</script>'

    def test_index_mismatch_resets_header_and_retries_once(self):
        sdk, calls = self.runtime(["TEST001", "TEST001"], [self.index("TEST002"), self.index("TEST001")])
        records = []
        result = verified_visit_cases(sdk, "TEST001", records.append)
        self.assertEqual(result[0].mrn, "TEST001")
        self.assertEqual(calls.count("QueryPatientRecord.do"), 2)
        self.assertTrue(records[0]["recovered"])
        self.assertEqual(records[0]["attempts"][0]["phase"], "visit_index")
        self.assertEqual(records[0]["attempts"][0]["returned_mrns"], ["TEST002"])
        self.assertNotIn("SECRET_AUTH", json.dumps(records))

    def test_visit_context_uses_sdk_review_once_before_patient_check(self):
        sdk, calls = self.runtime(["TEST001"], [self.index("TEST001")], self.review_form())
        cases = verified_visit_cases(sdk, "TEST001")
        self.assertEqual(len(cases), 1)
        self.assertEqual(calls, ["QueryPatientRecord.do", "EMRProcess.do", "KS_Patient.jsp", "QueryCaseList.do"])
        self.assertEqual(dict(sdk._runtime.review_payload)["valueA(cause)"], "1A")

    def test_visit_patient_mismatch_after_review_never_submits_again(self):
        sdk, calls = self.runtime(["TEST002"], [], self.review_form())
        with self.assertRaises(ParseError):
            verified_visit_cases(sdk, "TEST001")
        self.assertEqual(calls.count("QueryPatientRecord.do"), 1)
        self.assertEqual(calls.count("EMRProcess.do"), 1)
        self.assertNotIn("QueryCaseList.do", calls)

    def test_visit_review_without_medical_reason_stops_before_submission(self):
        form = self.review_form().replace('value="1A"', 'value="2B"')
        sdk, calls = self.runtime([], [], form)
        with self.assertRaises(Exception) as error:
            verified_visit_cases(sdk, "TEST001")
        self.assertEqual(error_info(error.exception).code, "PRQ_ACCESS_REVIEW_REQUIRED")
        self.assertEqual(calls, ["QueryPatientRecord.do"])

    def test_visit_review_uncertain_response_never_submits_twice(self):
        sdk, calls = self.runtime([], [], self.review_form(), review_response="<html>unknown</html>")
        with self.assertRaises(Exception) as error:
            verified_visit_cases(sdk, "TEST001")
        self.assertEqual(error_info(error.exception).code, "PRQ_ACCESS_REVIEW_NOT_ACCEPTED")
        self.assertEqual(calls, ["QueryPatientRecord.do", "EMRProcess.do"])

    def test_gateway_does_not_replay_prq_calls_with_conditional_review(self):
        failure = RequestError("synthetic connection loss")
        self.assertFalse(AccountGateway._can_recover("records", "get_visit_cases", failure,
                                                      sdk_managed_prq=True))
        self.assertFalse(AccountGateway._can_recover("orders", "get_order_history", failure,
                                                      sdk_managed_prq=True))
        self.assertFalse(AccountGateway._can_recover("records", "get_visit_cases", failure))
        self.assertFalse(AccountGateway._can_recover("patients", "get_registration_history", failure))

    def test_wrong_header_never_reads_index_and_stops_after_two_attempts(self):
        sdk, calls = self.runtime(["TEST002", "TEST002"], [])
        diagnostics = []
        with self.assertRaises(ParseError) as error:
            verified_visit_cases(sdk, "TEST001", diagnostics.append)
        self.assertEqual(error_info(error.exception).code, "PRQ_CASE_PATIENT_MISMATCH")
        self.assertNotIn("QueryCaseList.do", calls)
        self.assertEqual(len(diagnostics[0]["attempts"]), 2)
        self.assertFalse(diagnostics[0]["recovered"])

    def test_other_parse_error_does_not_repeat_requests(self):
        sdk, calls = self.runtime(["TEST001"], ["unrecognizable"])
        with self.assertRaises(ParseError):
            verified_visit_cases(sdk, "TEST001")
        self.assertEqual(calls.count("QueryPatientRecord.do"), 1)

    def test_sdk_active_branch_does_not_mix_in_another_patient(self):
        def row(mrn):
            url = f"QueryCaseDetail.do?hhisnum={mrn}&caseDT=2026-09-21&caseType=O&caseNo=1&caseSec=70"
            return f'new KSCase("{url}", "2026-09-21", type, section, "測試醫師");'
        page = '<div id="typeO"></div><script>if (true) {' + row("TEST001") + '} else {' + row("TEST002") + '}</script>'
        sdk, calls = self.runtime(["TEST001"], [page])
        diagnostics = []
        cases = verified_visit_cases(sdk, "TEST001", diagnostics.append)
        self.assertEqual([c.mrn for c in cases], ["TEST001"])
        self.assertEqual(cases[0].doctor_name, "測試醫師")
        self.assertEqual(calls.count("QueryPatientRecord.do"), 1)
        self.assertFalse(diagnostics)

    def test_verified_patient_header_accepts_active_linked_old_mrn(self):
        page = ('<div id="typeO"></div><script>new KSCase('
            '"QueryCaseDetail.do?hhisnum=OLD001&caseDT=2026-09-21&caseType=O&caseNo=1&caseSec=70",'
            '"2026-09-21", type, section, "測試醫師");</script>')
        sdk, calls = self.runtime(["TEST001"], [page])
        cases = verified_visit_cases(sdk, "TEST001")
        self.assertEqual([(c.mrn, c.patient_mrn) for c in cases], [("OLD001", "TEST001")])
        self.assertEqual(calls.count("QueryPatientRecord.do"), 1)

    def test_sdk_unknown_branch_remains_an_error_not_an_empty_history(self):
        page = ('<div id="typeO"></div><script>if (runtimeFlag) {new KSCase('
            '"QueryCaseDetail.do?hhisnum=TEST001&caseDT=2026-09-21&caseType=O&caseNo=1&caseSec=70",'
            '"2026-09-21", type, section, "測試醫師");}</script>')
        sdk, calls = self.runtime(["TEST001"], [page])
        diagnostics = []
        with self.assertRaises(ParseError) as error:
            verified_visit_cases(sdk, "TEST001", diagnostics.append)
        self.assertEqual(error_info(error.exception).code, "JS_BRANCH_UNSUPPORTED")
        self.assertEqual(calls.count("QueryPatientRecord.do"), 1)
        self.assertEqual(diagnostics[0]["attempts"][0]["phase"], "visit_index")

    def test_monitor_interval_is_an_integer_from_one_hour_to_thirty_days(self):
        for value, unit, hours in [(1, "hours", 1), (720, "hours", 720), (1, "days", 24), (30, "days", 720)]:
            self.assertEqual(interval({"interval_value": value, "interval_unit": unit}, {})["hours"], hours)
        for value, unit in [(0, "days"), (31, "days"), (721, "hours"), (1.5, "hours"), (True, "days"), (1, "weeks")]:
            with self.assertRaises(ValueError):
                interval({"interval_value": value, "interval_unit": unit}, {})


class WorkspaceRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = BotApplication(Settings(), Path(self.temp.name), BotSyntheticSDK)
        key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(key)
        self.db = self.work.review.db
        BotSyntheticSDK.calls = []

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def task(self, **values):
        return wait_task(self.work, self.work.review.start(values)["task_id"])

    def test_sdk_login_rejection_pauses_account_without_repeating_query(self):
        records = self.work.gateway.connection.records
        with patch.object(records, "get_visit_cases", side_effect=LoginRejectedError(
                "synthetic rejection", code="AUTH_LOGIN_REJECTED")) as query:
            with self.assertRaises(LoginRejectedError) as error:
                self.work.gateway.invoke("records", "get_visit_cases", "TEST001")
        self.assertEqual(query.call_count, 1)
        self.assertEqual(error_info(error.exception).code, "AUTH_LOGIN_REJECTED")
        self.assertFalse(self.work.gateway.online)
        self.assertIsNone(self.work.gateway.context)

    def test_sync_ignores_filters_and_does_not_fetch_individual_cases(self):
        real = self.work.gateway.connection.reviews.get_cases
        with patch.object(self.work.gateway.connection.reviews, "get_cases", wraps=real) as get_cases:
            task = self.task(kind="approval_sync", filters={"mrn": "NONE", "verify_code": "2", "start_date": "2099-01-01"})
        self.assertEqual(task["status"], "completed")
        self.assertTrue(task["finished_at"])
        requested = get_cases.call_args.args[0]
        self.assertEqual(requested.doctor_card, "TEST")
        self.assertFalse(requested.mrn or requested.start_date or requested.verify_code)
        self.assertEqual(self.work.approvals.cases()["total"], 3)
        self.assertFalse(any(c[1] == "review-detail" for c in BotSyntheticSDK.calls))
        list_items = [i for i in task["items"] if "version" in i]
        self.assertEqual(len(list_items), 3)
        self.assertTrue(all("case" not in i and i["version"] for i in list_items))
        self.assertEqual(len([i for i in task["items"] if "order_names" in i]), 3)
        before = list(BotSyntheticSDK.calls)
        self.assertEqual(self.work.approvals.cases({"decision": "1"})["total"], 1)
        self.assertEqual(BotSyntheticSDK.calls, before)
        self.task(kind="approval_sync")
        run = self.work.approvals.sync_history()["runs"][0]
        self.assertEqual((run["new"], run["changed"], run["unchanged"]), (0, 0, 3))
        options = self.task(kind="approval_options", force=True)
        self.assertIn(options["id"], {t["id"] for t in self.work.approvals.cases()["tasks"]})

    def test_sync_missing_and_failed_list_retains_cases(self):
        self.task(kind="approval_sync")
        before = self.db.all("approval_case")
        with patch.object(self.work.gateway.connection.reviews, "get_cases", side_effect=RuntimeError("SECRET")):
            task = self.task(kind="approval_sync")
        self.assertEqual(task["status"], "failed")
        self.assertEqual(before, self.db.all("approval_case"))
        self.assertNotIn("SECRET", json.dumps(self.work.approvals.sync_history()))
        with patch.object(self.work.gateway.connection.reviews, "get_cases", return_value=[]):
            self.task(resume=task["id"])
        self.assertEqual(before, self.db.all("approval_case"))

    def test_business_versions_ignore_generated_ids_and_detail_shape(self):
        tracker = self.work.approvals.tracker
        case = clean(self.work.gateway.connection._review_rows()[1])
        case["fields"]["UniqueID"] = "first"
        tracker.observe(case)
        altered = copy.deepcopy(case)
        altered["fields"]["UniqueID"] = "another"
        self.assertEqual(tracker.observe(altered)["change"], "unchanged")
        altered["fields"]["DetailOnly"] = "diagnosis"
        self.assertEqual(tracker.observe(altered, source_kind="detail")["change"], "unchanged")
        self.assertEqual(tracker.observe(case)["change"], "unchanged")
        self.assertEqual(len(self.work.approvals.history("1002")), 1)
        # A closed case can still receive a real correction without a new decision.
        amended = copy.deepcopy(case)
        amended["fields"]["ReviewComment"] = "corrected"
        self.assertEqual(tracker.observe(amended)["change"], "changed")
        self.assertTrue(self.db.get("approval_tracking", "1002")["unread"])
        self.assertEqual(len(self.work.approvals.history("1002")), 2)
        self.assertEqual(self.db.get("approval_case", "1002")["case"]["fields"]["DetailOnly"], "diagnosis")

    def test_schedule_discovers_new_cases_with_no_pending_case(self):
        with patch.object(self.work.review, "start") as start:
            self.work.approvals.tracker.schedule()
            start.assert_not_called()
            self.work.approvals.tracker.enter()
        start.assert_called_once_with({"kind": "approval_sync", "automatic": True})
        self.task(kind="approval_sync")
        with patch.object(self.work.review, "start") as start:
            self.work.approvals.tracker.schedule()
        start.assert_not_called()

    def test_existing_observations_are_indexed_without_rewriting_history(self):
        approvals = self.work.approvals
        case = clean(self.work.gateway.connection._review_rows()[0])
        self.db.save("approval_case", {"id": "1001", "case": case, "fetched_at": "2026-09-01T00:00:00+08:00"})
        self.db.save("approval_observation", {"apply_seq": "1001", "case": case,
            "source": "legacy-list", "checked_at": "2026-09-01T00:00:00+08:00"})
        raw = self.db.all("approval_observation")
        self.db.delete("preferences", "approval_content_index_v1")
        approvals.index_versions()
        self.assertEqual(raw, self.db.all("approval_observation"))
        self.assertEqual(len(self.db.all("approval_version")), 1)
        amended = copy.deepcopy(case)
        amended["fields"]["ReviewComment"] = "actual correction"
        self.assertEqual(approvals.tracker.observe(amended)["change"], "changed")
        self.assertTrue(approvals.history("1001")[0]["changes"])

    def test_unverified_national_id_never_creates_patient_membership(self):
        with patch.object(self.work.gateway, "resolve_identity", side_effect=ParseError(
                "mismatch", code="PRQ_PATIENT_ID_MISMATCH")):
            task = self.task(kind="resolve", identifier_kind="national_id", identifiers="A123456789")
        self.assertEqual(task["status"], "partial")
        self.assertFalse(any(i["status"] == "resolved" for i in task["items"]))
        self.assertFalse(self.db.all("set"))

    def test_wrong_patient_never_saves_soap_and_single_failure_can_be_retried(self):
        self.task(kind="resolve", identifiers="TEST001,TEST002")
        group = self.work.review.save_set({"mrns": "TEST001,TEST002"})
        records = self.work.gateway.connection.records
        original = records.get_visit_cases
        def wrong(mrn):
            if mrn == "TEST001":
                return original("TEST002")
            return original(mrn)
        with patch.object(records, "get_visit_cases", wrong):
            failed = self.task(set_id=group["id"])
        self.assertEqual(failed["status"], "partial")
        item = next(i for i in failed["items"] if i["mrn"] == "TEST001")
        self.assertEqual(item["code"], "PRQ_CASE_PATIENT_MISMATCH")
        self.assertFalse(item["records"])
        self.assertEqual(len(self.db.all("patient_diagnostic")), 1)
        count = len(BotSyntheticSDK.calls)
        done = self.task(resume=failed["id"], retry_only=["TEST001"])
        self.assertEqual(done["status"], "completed")
        self.assertFalse(any("TEST002" in c[2:] for c in BotSyntheticSDK.calls[count:]))

    def test_salary_html_changes_are_fetches_but_business_changes_are_versions(self):
        earnings = self.work.earnings
        a = parse_document('<table id="a"><tr><th>績點</th><td>1200</td></tr></table>')
        b = parse_document('<table id="b" class="new"><tr><th>績點</th><td>1200</td></tr></table>')
        earnings.store_report("performance", "202610", "2026/10", a)
        earnings.store_report("performance", "202610", "2026/10", b)
        report = self.db.all("earnings_report")[0]
        self.assertEqual(report["version_count"], 1)
        self.assertEqual(report["month"], "2026-10")
        detail = earnings.detail({"id": report["id"]})
        self.assertEqual(len(detail["fetches"]), 2)
        changed = parse_document('<table><tr><th>績點</th><td>1201</td></tr></table>')
        earnings.store_report("performance", "202610", "2026/10", changed)
        self.assertEqual(earnings.detail({"id": report["id"]})["report"]["version_count"], 2)
        self.db.save("earnings_catalog", {"id": "performance", "periods": []})
        self.assertEqual(earnings.detail({"id": report["id"]})["version"]["payload"]["tables"][0]["grid"][0][1], "1201")


if __name__ == "__main__":
    unittest.main()
