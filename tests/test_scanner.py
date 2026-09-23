import unittest
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from vghks_sdk import AuthenticationError, OutpatientPatient, ParseError, SoapRecord, VisitCase

from opd_monitor.scanner import ScanState, run_scan
from opd_monitor.settings import Settings

DAY = date(2026, 9, 19)
CONFIG = Settings(username="DOC1", password="synthetic-password")


def patient(mrn="TEST001", **values):
    return replace(OutpatientPatient(mrn, "Synthetic Patient", DAY, "F", "60", "70", "01", "DOC1F", True), **values)


def visit(mrn="TEST001", **values):
    return replace(VisitCase(mrn, DAY, "O", "case1", "70", "眼科"), **values)


class FakeSDK:
    def __init__(self):
        self.auth = SimpleNamespace(check=Mock(return_value=SimpleNamespace(ok=True)))
        self.opd = SimpleNamespace(get_doctor_patients=Mock(return_value=[patient()]))
        self.records = SimpleNamespace(
            get_visit_cases=Mock(return_value=[visit()]),
            get_soap=Mock(side_effect=lambda row: SoapRecord(
                row, ("# APPLY Cataract OD",), assessment_plan="# APPLY Cataract OD", present_sections=("AP",))),
        )
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.sdk = FakeSDK()

    def run_scan(self, start=DAY, end=DAY, state=None):
        state = state or ScanState(start, end)
        run_scan(state, CONFIG, start, end, lambda _: self.sdk)
        return state.snapshot()

    def test_only_same_date_patient_section_outpatient_and_owned_list(self):
        self.sdk.opd.get_doctor_patients.return_value = [patient(), patient("SHARED", doctor_card=""), patient("OTHER", doctor_card="DOC2"), patient("PREFIX", doctor_card="DOC1F2")]
        self.sdk.records.get_visit_cases.return_value = [visit(), visit(visit_date=DAY - timedelta(days=1)), visit(section_code="71"), visit(case_type="A"), visit(case_type="E"), visit()]
        result = self.run_scan()
        self.assertEqual(result["status"], "completed")
        self.sdk.opd.get_doctor_patients.assert_called_once_with("DOC1", DAY)
        self.sdk.records.get_visit_cases.assert_called_once_with("TEST001")
        self.sdk.records.get_soap.assert_called_once_with(visit())
        self.assertEqual(result["counts"]["matched_patients"], 1)
        self.assertEqual(result["counts"]["shared"], 1)
        self.assertEqual(result["counts"]["excluded"], 2)
        self.assertTrue(self.sdk.closed)

    def test_old_or_other_section_records_do_not_establish_attendance(self):
        self.sdk.records.get_visit_cases.return_value = [visit(visit_date=DAY - timedelta(days=1)), visit(section_code="71")]
        result = self.run_scan()
        self.assertEqual(result["counts"]["without_visit"], 1)
        self.assertEqual(result["records"], [])
        self.sdk.records.get_soap.assert_not_called()

    def test_inclusive_range_and_repeated_patient_context(self):
        start = DAY - timedelta(days=1)
        self.sdk.opd.get_doctor_patients.side_effect = lambda account, day: [patient(visit_date=day), patient(visit_date=day, room="02")]
        self.sdk.records.get_visit_cases.return_value = [visit(), visit(visit_date=start, case_no="case2")]
        result = self.run_scan(start, DAY)
        self.assertEqual(self.sdk.opd.get_doctor_patients.call_count, 2)
        self.sdk.records.get_visit_cases.assert_called_once_with("TEST001")
        self.assertEqual(self.sdk.records.get_soap.call_count, 2)
        self.assertEqual(result["counts"]["matched_patients"], 1)
        self.assertEqual(result["counts"]["matched_visits"], 2)

    def test_no_patient_or_case_sample_limit(self):
        self.sdk.opd.get_doctor_patients.return_value = [patient(f"TEST{i:03}") for i in range(20)]
        current = []

        def visits(mrn):
            current[:] = [mrn]
            return [visit(mrn, case_no=f"case{i}") for i in range(8)]

        def soap(case):
            self.assertEqual(case.mrn, current[0])
            return SoapRecord(case, ("No special marker",))

        self.sdk.records.get_visit_cases.side_effect = visits
        self.sdk.records.get_soap.side_effect = soap
        result = self.run_scan()
        self.assertEqual(result["counts"]["soap_read"], 160)
        self.assertEqual(result["counts"]["patients_done"], 20)

    def test_query_error_is_unknown_and_other_patients_continue(self):
        self.sdk.opd.get_doctor_patients.return_value = [patient(), patient("TEST002")]
        self.sdk.records.get_visit_cases.side_effect = [ParseError("secret source"), [visit("TEST002")]]
        result = self.run_scan()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["counts"]["unknown"], 1)
        self.assertEqual(result["counts"]["without_visit"], 0)
        self.assertEqual(result["counts"]["matched_patients"], 1)
        self.assertNotIn("secret source", str(result))

    def test_wrong_soap_identity_is_not_displayed(self):
        self.sdk.records.get_soap.side_effect = None
        self.sdk.records.get_soap.return_value = SoapRecord(visit("OTHER"), ("# private",))
        result = self.run_scan()
        self.assertEqual(result["records"], [])
        self.assertEqual(result["counts"]["soap_read"], 0)
        self.assertEqual(result["issues"][0]["code"], "SOAP_CASE_MISMATCH")

    def test_foreign_visit_patient_is_unknown_not_absent(self):
        self.sdk.records.get_visit_cases.return_value = [visit("OTHER")]
        result = self.run_scan()
        self.assertEqual(result["counts"]["unknown"], 1)
        self.assertEqual(result["counts"]["without_visit"], 0)
        self.sdk.records.get_soap.assert_not_called()

    def test_missing_visit_date_or_section_is_unknown(self):
        self.sdk.records.get_visit_cases.return_value = [visit(visit_date=None), visit(section_code="")]
        result = self.run_scan()
        self.assertEqual(result["counts"]["unknown"], 1)
        self.assertEqual(result["counts"]["without_visit"], 0)
        self.assertEqual(result["status"], "partial")

    def test_empty_soap_is_not_no_match(self):
        self.sdk.records.get_soap.side_effect = lambda row: SoapRecord(row, ())
        result = self.run_scan()
        self.assertEqual(result["counts"]["soap_missing"], 1)
        self.assertEqual(result["counts"]["soap_read"], 0)
        self.assertEqual(result["status"], "partial")

    def test_failed_day_continues_and_missing_identity_is_not_queried(self):
        self.sdk.opd.get_doctor_patients.side_effect = [ParseError("broken"), [patient(mrn=""), patient(section_code=""), patient()]]
        result = self.run_scan(DAY - timedelta(days=1), DAY)
        self.assertEqual(result["counts"]["days_failed"], 1)
        self.assertEqual(result["counts"]["unknown"], 2)
        self.sdk.records.get_visit_cases.assert_called_once_with("TEST001")

    def test_authentication_failure_stops_without_repeated_login_attempts(self):
        self.sdk.records.get_visit_cases.side_effect = AuthenticationError("private password")
        result = self.run_scan()
        self.assertEqual(result["status"], "failed")
        self.sdk.records.get_visit_cases.assert_called_once()
        self.assertNotIn("private password", str(result))

    def test_cancel_preserves_partial_results_and_closes_sdk(self):
        state = ScanState(DAY, DAY)
        self.sdk.records.get_visit_cases.return_value = [visit(), visit(case_no="case2")]

        def soap(case):
            state.cancel.set()
            return SoapRecord(case, ("# APPLY IVI-E2",), assessment_plan="# APPLY IVI-E2", present_sections=("AP",))

        self.sdk.records.get_soap.side_effect = soap
        result = self.run_scan(state=state)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(len(result["records"]), 1)
        self.sdk.records.get_soap.assert_called_once()
        self.assertTrue(self.sdk.closed)

    def test_failed_readiness_never_queries_patients(self):
        self.sdk.auth.check.return_value = SimpleNamespace(ok=False, targets=[])
        result = self.run_scan()
        self.assertEqual(result["status"], "failed")
        self.sdk.opd.get_doctor_patients.assert_not_called()


if __name__ == "__main__":
    unittest.main()
