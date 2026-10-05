"""Select order provenance from verified visits, including legacy patient IDs."""
from .analysis_store import digest
from .settings import today

SCOPE_KEY = "orders-cataract-scope"


def is_eye_visit(raw):
    name = raw.get("section_name", "").strip()
    return ("眼科" in name or name.casefold().startswith("ophthalmology")
            or not name and raw.get("section_code", "").strip() == "70")


def patient_visits(visits, mrn):
    last_day = today().isoformat()
    return [raw for raw in visits or [] if isinstance(raw, dict)
            and (raw.get("lookup_mrn") or raw.get("mrn")) == mrn
            and raw.get("case_no") and (not raw.get("visit_date") or raw["visit_date"] <= last_day)]


def eye_order_contexts(visits, mrn):
    # A history row may use the lookup MRN for an encounter stored under an
    # older MRN. Accept that mapping only when its department is unambiguous.
    flags = {}
    for raw in patient_visits(visits, mrn):
        key = (raw.get("case_type"), raw["case_no"])
        for source in {raw["mrn"], mrn}:
            flags.setdefault((source, *key), set()).add(is_eye_visit(raw))
    return {key for key, values in flags.items() if values == {True}}


def order_in_eye_context(order, contexts):
    if not isinstance(order, dict):
        return False
    key = (order.get("mrn"), order.get("case_type"), order.get("case_no"))
    return key in contexts and all(
        not order.get(name) or (isinstance(order[name], dict)
                               and (order[name].get("mrn"), order[name].get("case_type"), order[name].get("case_no")) == key)
        for name in ("detail_ref", "report_ref"))


def eye_visit_signature(visits, mrn):
    return digest(sorted((raw["mrn"], raw.get("case_type", ""), raw["case_no"],
                          raw.get("visit_date") or "", raw.get("section_code", ""), raw.get("section_name", ""))
                         for raw in patient_visits(visits, mrn) if is_eye_visit(raw)))
