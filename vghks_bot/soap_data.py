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


def display_chronic_prescriptions(record: dict) -> None:
    """Enrich a legacy review response without changing stored SOAP or tag indices."""
    structure = record.get("soap_structure")
    if not isinstance(structure, dict) or "MEDICATIONS" not in structure.get("present_sections", []):
        return
    rows, blocks = structure.get("medications"), structure.get("blocks")
    if (not rows or any("prescription_type" in row for row in rows) or not blocks
            or not any("慢性" in block or "慢箋" in block for block in blocks)):
        return
    from vghks_sdk.parsing import parse_soap_medications

    medications, periods, issues = parse_soap_medications(blocks)
    chronic = [row for row in medications if row.prescription_type == "chronic"]
    if (not chronic
            or [row.raw_text for row in medications] != [row.get("raw_text") for row in rows]):
        # Some legacy layouts have lost their table labels. Never shift tags or
        # attribute a medication if the same printed rows cannot be aligned.
        return
    # Saved text no longer has HTML section labels. Only link legacy rows to a
    # notice printed in their own block, not a possibly unrelated earlier block.
    if any(row.chronic_period_index is None
           or row.raw_text not in blocks[periods[row.chronic_period_index].source_block_index].splitlines()
           for row in chronic):
        return
    reparsed_issues = {"SOAP_MEDICATION_ROW_UNRECOGNIZED", "SOAP_CHRONIC_PRESCRIPTION_PERIOD_UNRECOGNIZED"}
    remaining_issues = [issue for issue in structure.get("parsing_issues", []) if issue not in reparsed_issues]
    current_issues = list(dict.fromkeys([*remaining_issues, *issues]))
    record["soap_structure"] = {**structure, "medications": to_jsonable(medications),
                                "chronic_prescription_periods": to_jsonable(periods),
                                "parsing_issues": current_issues}
    if not any(issue.startswith("SOAP_MEDICATION_") for issue in current_issues) and "tag_scope_issues" in record:
        record["tag_scope_issues"] = [item for item in record["tag_scope_issues"] if item.get("scope") != "medications"]


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
