"""Normalize local patient demographics without fetching or changing source data."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

from .settings import today


def birthday_iso(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?", text):
        try:
            return datetime.fromisoformat(text).date().isoformat()
        except ValueError:
            return ""
    era = "roc" if text.startswith("民國") else "ad" if text.startswith("西元") else ""
    text = re.sub(r"^(民國|西元)\s*", "", text)
    text = text.replace("年", "-").replace("月", "-").removesuffix("日")
    parts = re.fullmatch(r"(\d{1,4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})", text)
    if parts:
        year_text, month, day = parts.groups()
    elif re.fullmatch(r"\d{6,8}", text):
        year_text, month, day = text[:-4], text[-4:-2], text[-2:]
    else:
        return ""
    year = int(year_text)
    if era == "roc" or not era and len(year_text) <= 3:
        if year < 1:
            return ""
        year += 1911
    try:
        return date(year, int(month), int(day)).isoformat()
    except ValueError:
        return ""


def merge_demographics(patient, *preferred, on_date=None):
    """Use verified local profiles first, then snapshots/registrations/records.

    All sources must refer to the same MRN. A missing value never erases a
    known value, and calculated age uses today's Taipei date, not the SOAP date.
    """
    sources = [*preferred, patient, *patient.get("registrations", []),
               *patient.get("source_registrations", []), *patient.get("records", [])]
    sources = [source for source in sources if isinstance(source, dict)
               and source.get("mrn", patient.get("mrn")) == patient.get("mrn")]

    def first(key):
        return next((source[key] for source in sources
                     if source.get(key) is not None and str(source[key]).strip()), "")

    result = {**patient, **{key: first(key) for key in ("name", "sex", "age")}}
    birthday_source = next((source for source in sources if birthday_iso(source.get("birthday"))), None)
    if birthday_source is not None:
        birthday = birthday_iso(birthday_source["birthday"])
        result["birthday"] = birthday
        result["birthday_raw"] = birthday_source.get("birthday_raw") or str(birthday_source["birthday"])
        born, current = date.fromisoformat(birthday), on_date or today()
        age = current.year - born.year - ((current.month, current.day) < (born.month, born.day))
        result["age"] = str(age) if born <= current and 0 <= age <= 130 else ""
    else:
        result["birthday"] = first("birthday")
        result["birthday_raw"] = first("birthday_raw") or result["birthday"]
    return result
