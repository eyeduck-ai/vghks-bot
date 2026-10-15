"""Phototherapy order compatibility alongside the pinned SDK; no JavaScript is executed.

PRQ's phototherapy button uses a literal ``filepath`` and a SECTORD PDF
whose patient identifier lives in the filename. Keep the SDK's request,
authentication and identity checks, extending only these recorded formats.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from html import unescape
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from vghks_sdk import ConfigurationError, ParseError, assess_data
from vghks_sdk.adapters.prq import PrqAdapter
from vghks_sdk.core.errors import error_info
from vghks_sdk.core.jsliteral import (
    evaluate_expression,
    evaluated_string_assignments,
    iter_active_constructor_calls,
    strip_js_comments,
)
from vghks_sdk.core.operations import operation_spec
from vghks_sdk.models import ClinicalOrder, OrderDetailRef, OrderReportRef, PdfAttachmentRef
from vghks_sdk.models._validation import _fully_unquote, _validate_navigation_ref
from vghks_sdk.parsing.clinical import (
    _INTERNAL_PATH_RE,
    _ORDER_VARIABLES,
    _order_refs_from_source,
    _order_sort_key,
    _pacs_refs_from_source,
    _pdf_candidates,
)
from vghks_sdk.parsing.common import normalize_inline_text, strip_markup
from vghks_sdk.services.orders import OrdersService

_CASE_ORDERS = operation_spec("prq.case_orders")
_CASE_ORDERS_PAGE = operation_spec("prq.case_orders_page")
_CASE_ORDERS_SELECT = operation_spec("prq.case_orders_select")
_ORDER_HISTORY = operation_spec("prq.order_history")
_ORDER_HISTORY_PAGE = operation_spec("prq.order_history_page")
_ORDER_HISTORY_SELECT = operation_spec("prq.order_history_select")

_LIGHT_PDF = re.compile(
    r"^(?://|\\\\)hfs01_1A0(?:\.vghks\.gov\.tw)?[/\\]SECTORD[/\\]lightrep[/\\]"
    r"(?P<mrn>[A-Za-z0-9]+)_[OAE]_[A-Za-z0-9]+\.pdf$",
    re.IGNORECASE,
)
_PACS_MARKER = re.compile(r"(?<![a-z0-9_])reqno\s*=|adm_querypacs", re.IGNORECASE)
_ESCAPED_CHARACTER = re.compile(r"\\(?:x[0-9a-f]{2}|u[0-9a-f]{4})", re.IGNORECASE)


class PhototherapyPdfRef(PdfAttachmentRef):
    def __post_init__(self):
        _validate_navigation_ref(self.mrn)
        path = _fully_unquote(self.file_path)
        match = _LIGHT_PDF.fullmatch(path)
        if not match:
            raise ConfigurationError("unknown phototherapy PDF path", code="PDF_REF_PATH_UNKNOWN")
        if match["mrn"] != self.mrn:
            raise ConfigurationError("PDF attachment MRN did not match", code="PDF_REF_MRN_MISMATCH")
        object.__setattr__(self, "file_path", path)


def pdf_reference(mrn, file_path):
    if _LIGHT_PDF.fullmatch(_fully_unquote(file_path)):
        return PhototherapyPdfRef(mrn, file_path)
    return PdfAttachmentRef(mrn, file_path)


class OrderIndex(list):
    """Keep usable rows and parsing failures together until they are persisted."""

    def __init__(self, orders=(), *, issues=()):
        super().__init__(orders)
        self.issues = tuple(issues)


def assess_order_index(value):
    assessment = assess_data(value)
    if isinstance(value, OrderIndex) and value.issues:
        return replace(assessment, complete=False, issues=value.issues)
    return assessment


def order_index_complete(step):
    return bool(step is not None and step.get("assessment", {}).get("complete") is not False)


def _source_mrn(source, mrn, contexts):
    """Discover a source identifier, then let the SDK validate every reference."""
    identifiers = set()
    for match in _INTERNAL_PATH_RE.finditer(unescape(source.replace('\\"', '"').replace("\\'", "'"))):
        parsed = urlsplit(match.group(0))
        if parsed.path.casefold().endswith(("queryorderdetail.do", "queryreportbyorder.do")):
            query = parse_qs(parsed.query, keep_blank_values=True)
            identifiers.add(query.get("hhisnum", [""])[-1].strip())
    if not identifiers:
        return mrn
    if len(identifiers) != 1:
        raise ParseError("order contained inconsistent patient references", code="ORDER_REF_MRN_MISMATCH")
    source_mrn = identifiers.pop()
    if source_mrn != mrn and source_mrn not in {context[0] for context in contexts}:
        raise ParseError("order patient was not verified", code="ORDER_REF_MRN_MISMATCH")
    return source_mrn


def _parse_order_row(source, values, *, mrn, case, contexts):
    source_mrn = mrn
    try:
        refs = _order_refs_from_source(source, expected_mrn=mrn)
    except ParseError as exc:
        if exc.info.code != "ORDER_REF_MRN_MISMATCH":
            raise
        source_mrn = _source_mrn(source, mrn, contexts)
        refs = _order_refs_from_source(source, expected_mrn=source_mrn)
    if source_mrn != mrn and any((ref.mrn, ref.case_type, ref.case_no) not in contexts for ref in refs):
        raise ParseError("old patient order encounter was not verified", code="ORDER_REF_CASE_MISMATCH")
    if case and any((ref.mrn, ref.case_type, ref.case_no) != (case.mrn, case.case_type, case.case_no) for ref in refs):
        raise ParseError("order encounter did not match", code="ORDER_REF_CASE_MISMATCH")
    detail = next((ref for ref in refs if isinstance(ref, OrderDetailRef)), None)
    report = next((ref for ref in refs if isinstance(ref, OrderReportRef)), None)
    navigation = detail or report
    pdfs = []
    # Most rows have neither PDF nor PACS links. Avoid building two more HTML
    # trees for each of the hundreds of ordinary laboratory/procedure rows.
    decoded = unescape(source).casefold()
    encoded_marker = bool(_ESCAPED_CHARACTER.search(source))
    for path in _pdf_candidates(source) if ".pdf" in decoded or "showpdf" in decoded or encoded_marker else ():
        try:
            ref = pdf_reference(source_mrn, path)
        except ConfigurationError as exc:
            raise ParseError("order contained an unsafe PDF reference", code=exc.info.code) from exc
        if ref not in pdfs:
            pdfs.append(ref)
    pacs = next(iter(_pacs_refs_from_source(source, expected_mrn=source_mrn)), None) if _PACS_MARKER.search(decoded) or encoded_marker else None
    order = ClinicalOrder(
        mrn=source_mrn,
        case_no=navigation.case_no if navigation else case.case_no if case else "",
        case_type=navigation.case_type if navigation else case.case_type if case else "O",
        name=strip_markup(values[1] or ""),
        order_date=normalize_inline_text(values[2]),
        execution_date=normalize_inline_text(values[3]),
        requester=strip_markup(values[4] or ""),
        status=strip_markup(values[5] or ""),
        attachment=strip_markup(values[6] or "") if len(values) > 6 else "",
        detail_ref=detail, report_ref=report, pacs_ref=pacs, pdf_refs=tuple(pdfs),
    )
    if not order.name:
        raise ParseError("order name was missing", code="PRQ_ORDER_EXPRESSION_UNSUPPORTED")
    return order


def _parse_order_source(source, *, mrn, case, contexts, allow_partial):
    variables = evaluated_string_assignments(source, _ORDER_VARIABLES | {"filepath"})
    calls = list(iter_active_constructor_calls(source, "KSCase", variables))
    cleaned = strip_js_comments(source) if len(calls) > 1 else source
    output, issues = [], []
    for call in calls:
        try:
            if len(call.arguments) < 6:
                raise ParseError("order constructor was incomplete", code="PRQ_ORDER_EXPRESSION_UNSUPPORTED")
            environment = variables if len(calls) == 1 else evaluated_string_assignments(
                cleaned[:call.end], _ORDER_VARIABLES | {"filepath"})
            values = tuple(evaluate_expression(argument, environment) for argument in call.arguments)
            if any(value is None for value in values):
                raise ParseError("order constructor contained an unsupported expression",
                                 code="PRQ_ORDER_EXPRESSION_UNSUPPORTED")
            # On a multi-row script, references belong to this constructor's
            # evaluated fields, rather than the first URL anywhere in the script.
            row_source = source
            if len(calls) > 1:
                row_source = "\n".join(values).replace("\\", "\\\\") + "\n" + json.dumps(values[6] if len(values) > 6 else "")
            output.append(_parse_order_row(row_source, values, mrn=mrn, case=case, contexts=contexts))
        except (ParseError, ConfigurationError, ValueError) as exc:
            if not allow_partial:
                raise
            issues.append(error_info(exc))
    return OrderIndex(output, issues=issues)


def parse_order_index(html_text, *, mrn, case=None, verified_cases=(), allow_partial=False):
    contexts = {(item.mrn, item.case_type, item.case_no) for item in verified_cases if item.patient_mrn == mrn}
    output, seen, issues = [], set(), []
    for script in BeautifulSoup(html_text, "html.parser").find_all("script"):
        if script.get("src"):
            continue
        source = str(script.string) if script.string is not None else script.get_text()
        if "new KSCase" not in source:
            continue
        try:
            orders = _parse_order_source(source, mrn=mrn, case=case, contexts=contexts, allow_partial=allow_partial)
        except (ParseError, ConfigurationError, ValueError) as exc:
            if not allow_partial:
                raise
            issues.append(error_info(exc))
            continue
        issues.extend(orders.issues)
        for order in orders:
            if order.name and order.identity not in seen:
                seen.add(order.identity)
                output.append(order)
    return OrderIndex(sorted(output, key=_order_sort_key, reverse=True), issues=issues)


class CompatibleOrderAdapter(PrqAdapter):
    """Preserve SDK navigation/diagnostics; replace only order-index parsing."""

    def get_case_orders(self, case):
        self._validate_outpatient_case(case)

        def operation():
            self._get_case_detail_raw(case)
            self._prime_key_raw()
            base = self.runtime.settings.prq_base_url.rstrip("/")
            common = {"Use": "Case", "caseNo": case.case_no, "caseType": case.case_type,
                      "hhisnum": case.mrn, "hid": self.runtime.auth.hid_for("prq")}
            self.runtime.request_text(_CASE_ORDERS_PAGE, base + "/Page/JSP/Order.jsp",
                                      params={**common, "casenoO": case.case_no, "section": case.section_code})
            self.runtime.request_text(_CASE_ORDERS_SELECT, base + "/Page/JSP/Order_Select.jsp", params=common)
            html = self.runtime.request_text(_CASE_ORDERS, base + "/QueryOrderResult.do", data={
                **common, "caseNoO": case.case_no, "date": "0", "ordersubtype": "*",
                "ordertype": "*", "orstepc": "*", "section": case.section_code})
            self._assert_constructor_list(html, code="PRQ_CASE_ORDERS_STRUCTURE_MISSING")
            return parse_order_index(html, mrn=case.mrn, case=case, allow_partial=True)

        return self.runtime.execute(_CASE_ORDERS, operation, operation_name="get_case_orders")

    def get_order_history(self, mrn, history_filter, *, verified_cases=()):
        def operation():
            self._history_context_raw(mrn)
            base = self.runtime.settings.prq_base_url.rstrip("/")
            common = {"Use": "Dur", "hhisnum": mrn, "hid": self.runtime.auth.hid_for("prq")}
            self.runtime.request_text(_ORDER_HISTORY_PAGE, base + "/Page/JSP/OrderC.jsp", params=common)
            self.runtime.request_text(_ORDER_HISTORY_SELECT, base + "/Page/JSP/OrderC_Select.jsp", params=common)
            html = self.runtime.request_text(_ORDER_HISTORY, base + "/QueryOrderResult.do", data={
                **common, "date": str(history_filter.lookback_days), "ordersubtype": history_filter.subtype,
                "ordertype": history_filter.category, "orstepc": history_filter.status})
            self._assert_constructor_list(html, code="PRQ_ORDER_HISTORY_STRUCTURE_MISSING")
            orders = parse_order_index(html, mrn=mrn, verified_cases=verified_cases, allow_partial=True)
            if history_filter.order_date is not None:
                orders = OrderIndex((order for order in orders if order.order_date.startswith(history_filter.order_date.isoformat())), issues=orders.issues)
            return orders

        return self.runtime.execute(_ORDER_HISTORY, operation, operation_name="get_order_history")


class CompatibleOrdersService(OrdersService):
    def get_order_history(self, mrn, filter, *, verified_cases=()):
        return self._adapter.get_order_history(mrn, filter, verified_cases=verified_cases)


def configure_orders(sdk):
    sdk.orders = CompatibleOrdersService(CompatibleOrderAdapter(sdk._runtime))
    return sdk
