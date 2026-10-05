"""Phototherapy order compatibility alongside SDK 0.22.2; no JavaScript is executed.

PRQ's phototherapy button uses a literal ``filepath`` and a SECTORD PDF
whose patient identifier lives in the filename. Keep the SDK's request,
authentication and identity checks, extending only these recorded formats.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup
from vghks_sdk import ConfigurationError, ParseError
from vghks_sdk.adapters.prq import (
    _CASE_ORDERS,
    _CASE_ORDERS_PAGE,
    _CASE_ORDERS_SELECT,
    _ORDER_HISTORY,
    _ORDER_HISTORY_PAGE,
    _ORDER_HISTORY_SELECT,
    PrqAdapter,
)
from vghks_sdk.core.jsliteral import (
    evaluate_expression,
    evaluated_string_assignments,
    iter_active_constructor_calls,
)
from vghks_sdk.models import ClinicalOrder, OrderDetailRef, OrderReportRef, PdfAttachmentRef
from vghks_sdk.models._validation import _fully_unquote, _validate_navigation_ref
from vghks_sdk.parsing.clinical import (
    _ORDER_VARIABLES,
    _order_refs_from_source,
    _order_sort_key,
    _pacs_refs_from_source,
    _pdf_candidates,
)
from vghks_sdk.parsing.common import normalize_inline_text, strip_markup
from vghks_sdk.services.orders import OrdersService

_LIGHT_PDF = re.compile(
    r"^(?://|\\\\)hfs01_1A0(?:\.vghks\.gov\.tw)?[/\\]SECTORD[/\\]lightrep[/\\]"
    r"(?P<mrn>[A-Za-z0-9]+)_[OAE]_[A-Za-z0-9]+\.pdf$",
    re.IGNORECASE,
)


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


def parse_order_index(html_text, *, mrn, case=None):
    output, seen = [], set()
    for script in BeautifulSoup(html_text, "html.parser").find_all("script"):
        if script.get("src"):
            continue
        source = script.string if script.string is not None else script.get_text()
        if "new KSCase" not in source:
            continue
        variables = evaluated_string_assignments(source, _ORDER_VARIABLES | {"filepath"})
        refs = _order_refs_from_source(source, expected_mrn=mrn)
        detail = next((ref for ref in refs if isinstance(ref, OrderDetailRef)), None)
        report = next((ref for ref in refs if isinstance(ref, OrderReportRef)), None)
        navigation = detail or report
        pdfs = []
        for path in _pdf_candidates(source):
            try:
                ref = pdf_reference(mrn, path)
            except ConfigurationError as exc:
                raise ParseError("order contained an unsafe PDF reference", code=exc.info.code) from exc
            if ref not in pdfs:
                pdfs.append(ref)
        for call in iter_active_constructor_calls(source, "KSCase", variables):
            if len(call.arguments) < 6:
                continue
            values = tuple(evaluate_expression(argument, variables) for argument in call.arguments)
            if any(value is None for value in values[:6]):
                raise ParseError("order constructor contained an unsupported expression",
                                 code="PRQ_ORDER_EXPRESSION_UNSUPPORTED")
            order = ClinicalOrder(
                mrn=mrn,
                case_no=navigation.case_no if navigation else case.case_no if case else "",
                case_type=navigation.case_type if navigation else case.case_type if case else "O",
                name=strip_markup(values[1] or ""),
                order_date=normalize_inline_text(values[2]),
                execution_date=normalize_inline_text(values[3]),
                requester=strip_markup(values[4] or ""),
                status=strip_markup(values[5] or ""),
                attachment=strip_markup(values[6] or "") if len(values) > 6 else "",
                detail_ref=detail, report_ref=report,
                pacs_ref=next(iter(_pacs_refs_from_source(source, expected_mrn=mrn)), None),
                pdf_refs=tuple(pdfs),
            )
            if order.name and order.identity not in seen:
                seen.add(order.identity)
                output.append(order)
    return sorted(output, key=_order_sort_key, reverse=True)


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
            return parse_order_index(html, mrn=case.mrn, case=case)

        return self.runtime.execute(_CASE_ORDERS, operation, operation_name="get_case_orders")

    def get_order_history(self, mrn, history_filter):
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
            orders = parse_order_index(html, mrn=mrn)
            if history_filter.order_date is not None:
                orders = [order for order in orders if order.order_date.startswith(history_filter.order_date.isoformat())]
            return orders

        return self.runtime.execute(_ORDER_HISTORY, operation, operation_name="get_order_history")


def configure_orders(sdk):
    sdk.orders = OrdersService(CompatibleOrderAdapter(sdk._runtime))
    return sdk
