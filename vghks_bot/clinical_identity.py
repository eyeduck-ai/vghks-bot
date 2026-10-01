"""Local SDK compatibility rules; never relax patient identity checks."""
import re
from html import unescape
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from vghks_sdk import ParseError
from vghks_sdk.core.errors import error_info
from vghks_sdk.identifiers import normalize_mrn

from .diagnostics import failure_details
from .settings import timestamp


def doctor_identity(value):
    return re.sub(r"(?<=\d)[A-Z]+$", "", value.strip().upper())


def classify_opd_registration(patient, *, doctor_card):
    account, physician = doctor_identity(doctor_card), doctor_identity(patient.doctor_card)
    if account and physician == account:
        return "DEDICATED"
    return "SHARED" if patient.doctor_label_present and not physician else "UNCLASSIFIED"


def select_registration_visits(registrations, cases, *, doctor_card):
    keys = {(r.mrn, r.visit_date, r.section_code.strip()) for r in registrations
            if r.section_code.strip() and classify_opd_registration(r, doctor_card=doctor_card) == "DEDICATED"}
    unique = {c.identity: c for c in cases if c.case_type.strip().upper() == "O"
              and (c.patient_mrn, c.visit_date, c.section_code.strip()) in keys}
    return sorted(unique.values(), key=lambda c: c.identity)


def verified_visit_cases(sdk, mrn, diagnostic=None):
    """Run under AccountGateway.serial, including the single context-reset retry.

    Capture only identifiers and parsing stages, never HTML, cookies or URLs
    carrying PRQ authentication parameters. The pinned SDK supplies the parsers.
    """
    from vghks_sdk.adapters.prq import _PATIENT_IDENTITY, _VISIT_CASES
    from vghks_sdk.parsing.prq import (
        _visit_links,
        parse_patient_identity,
        parse_visit_cases,
    )

    mrn = normalize_mrn(mrn)
    runtime = getattr(sdk, "_runtime", None)
    attempts = []
    for attempt in (1, 2):
        event = {"attempt": attempt, "phase": "visit_index", "expected_mrn": mrn}
        try:
            if runtime is None:  # A test or a future SDK implementation.
                cases = sdk.records.get_visit_cases(mrn)
            else:
                def operation(event=event):
                    base = runtime.settings.prq_base_url.rstrip("/")
                    event["phase"] = "patient_context"
                    # Use the SDK's checked patient-context flow. It handles a
                    # conditional clinical access review before we inspect the
                    # patient header and visit index.
                    try:
                        sdk.records._adapter._establish_patient_context_raw(mrn, "", None)
                    finally:
                        event["access_review_attempted"] = runtime.operation_write_attempted
                    event["phase"] = "patient_header"
                    header = runtime.request_text(_PATIENT_IDENTITY, base + "/Page/JSP/KS_Patient.jsp")
                    event["header_mrn"] = parse_patient_identity(header)
                    if event["header_mrn"] != mrn:
                        raise ParseError("patient header mismatch", code="PRQ_CASE_PATIENT_MISMATCH")
                    event["phase"] = "visit_index"
                    html = runtime.request_text(_VISIT_CASES, base + "/QueryCaseList.do")
                    returned = {parse_qs(urlsplit(unescape(href)).query).get("hhisnum", [mrn])[-1]
                                for href, *_ in _visit_links(html)}
                    event["returned_mrns"] = sorted(returned)[:20]
                    cases = parse_visit_cases(html, mrn, allow_related_mrns=True)
                    if not cases and BeautifulSoup(html, "html.parser").find(id="typeO") is None:
                        raise ParseError("case list structure missing", code="PRQ_CASE_LIST_STRUCTURE_MISSING")
                    return cases
                cases = runtime.execute(_VISIT_CASES, operation, operation_name="verified_visit_cases")
            if not isinstance(cases, list) or any(c.patient_mrn != mrn for c in cases):
                event["returned_mrns"] = sorted({getattr(c, "mrn", "") for c in cases})[:20] if isinstance(cases, list) else []
                raise ParseError("case list patient mismatch", code="PRQ_CASE_PATIENT_MISMATCH")
            event.update(status="ready", count=len(cases))
            attempts.append(event)
            if attempt > 1 and diagnostic:
                diagnostic({"mrn": mrn, "recorded_at": timestamp(), "recovered": True, "attempts": attempts})
            return cases
        except Exception as exc:
            code = error_info(exc).code
            event.update(status="error", code=code, error=failure_details(exc))
            attempts.append(event.copy())
            if (code == "PRQ_CASE_PATIENT_MISMATCH" and attempt == 1
                    and not event.get("access_review_attempted")):
                continue
            if diagnostic:
                diagnostic({"mrn": mrn, "recorded_at": timestamp(), "recovered": False, "attempts": attempts})
            raise
