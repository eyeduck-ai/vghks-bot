"""Lossless SDK SOAP snapshots and explicit tag search sources."""
from __future__ import annotations

from vghks_sdk import SoapRecord
from vghks_sdk import __version__ as sdk_version
from vghks_sdk.models import to_jsonable

TEXT_FIELDS = {"s": ("subjective",), "o": ("objective",),
               "ap": ("assessment_plan", "assessment", "plan")}
SCOPE_LABELS = {"all": "全文", "s": "S", "o": "O", "ap": "A+P",
                "medications": "藥囑", "orders": "醫囑"}


def snapshot(soap: SoapRecord) -> dict:
    # The encounter is validated by the caller. Do not duplicate its transient
    # PRQ request parameters in the clinical payload.
    value = to_jsonable(soap)
    value.pop("case", None)
    return {"soap": soap.full_text, "soap_structure": {
        **value, "schema_version": 1, "sdk_version": sdk_version,
    }}


def scope_sources(record: dict, scope: str) -> list[dict]:
    if scope == "all":
        return [{"field": "soap", "index": None, "text": record.get("soap", "")}]
    structure = record.get("soap_structure") or {}
    if scope in TEXT_FIELDS:
        return [{"field": field, "index": None, "text": structure[field]}
                for field in TEXT_FIELDS[scope] if isinstance(structure.get(field), str)]
    # Search each printed row independently, preserving the SDK's original text.
    # A keyword must not span two unrelated prescriptions/orders.
    return [{"field": scope, "index": index, "text": row.get("raw_text", "")}
            for index, row in enumerate(structure.get(scope, []))]


def scope_status(record: dict, scope: str) -> str:
    if scope == "all":
        return "ready"
    structure = record.get("soap_structure")
    if not isinstance(structure, dict):
        return "unavailable"
    if scope in TEXT_FIELDS:
        return "ready" if any(isinstance(structure.get(field), str) for field in TEXT_FIELDS[scope]) else "unavailable"
    if scope.upper() not in structure.get("present_sections", []):
        return "unavailable"
    prefix = "SOAP_MEDICATION_" if scope == "medications" else "SOAP_ORDER_"
    if any(issue.startswith(prefix) for issue in structure.get("parsing_issues", [])):
        return "partial"
    return "ready"
