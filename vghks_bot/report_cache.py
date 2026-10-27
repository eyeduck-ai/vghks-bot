"""Shared completeness rules for saved report branches and compositions."""
from __future__ import annotations


def branch_complete(payload, assessment=None):
    if (assessment or {}).get("complete") is False:
        return False
    if not isinstance(payload, dict):
        return True
    return (not payload.get("text_extraction_notes") and
            payload.get("report_data_status") not in {"PARTIAL", "UNPARSED", "TEXT_UNPARSED"})


def result_complete(payload, store, *, asset_available=None):
    if (payload.get("processing") or any(item.get("status") != "ready"
                                        for item in payload.get("attachment_items", [])) or
            payload.get("status") not in {
        "ready", "complete", "missing", "not_executed", "no_data", "no_links"
    } or payload.get("issues") or
            any(branch.get("status") != "ok" for branch in payload.get("branches", [])) or
            any(not branch_complete({"report_data_status": text.get("status"),
                                     "text_extraction_notes": text.get("text_extraction_notes")})
                for text in payload.get("texts", []))):
        return False
    # Older compositions omitted extraction notes. Recompose ambiguous legacy
    # reports once; their successful raw branches and binaries remain reusable.
    if not payload.get("report_cache_revision") and any(
            text.get("status") in {"METADATA_ONLY", "ATTACHMENT_ONLY"} and not text.get("text")
            for text in payload.get("texts", [])):
        return False
    try:
        for asset in payload.get("assets", []):
            if asset_available is not None:
                if not asset_available(asset["digest"]):
                    return False
            else:
                store.asset(asset["digest"])
    except (KeyError, TypeError, ValueError):
        return False
    return True
