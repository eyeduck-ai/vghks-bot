"""Extract template fields, leaving absent or ambiguous values unfilled."""
from __future__ import annotations

import re
import unicodedata
from datetime import date


def parse_surgery(excerpt: str) -> dict:
    text = unicodedata.normalize("NFKC", excerpt).replace("−", "-")
    header = text.splitlines()[0]
    result = dict.fromkeys(("procedure", "laterality", "iol", "target", "scheduled_date", "date_iso", "tel"), "")
    procedures = re.search(r"\b(LENSX\s*\+\s*CATA|VT\s*\+\s*MP|CATA|LMR|VT)\b", header, re.I)
    if procedures:
        result["procedure"] = re.sub(r"\s+", "", procedures.group()).upper()
    side = re.search(r"\b(OD|OS|OU)\b(?!\s*[/、?])", header, re.I)
    if side and not re.search(r"[/、]\s*$", header[:side.start()]):
        result["laterality"] = side.group().upper()
    target = re.search(r"\b(?:Target|T)\s*[:=]?\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+))(?![\d.?])", header, re.I)
    if target:
        result["target"] = target.group(1)
    parenthesis = re.search(r"\(([^()]*)\)", header)
    if parenthesis:
        lens = re.split(r"\b(?:Target|T)\s*[:=?+-]?\s*(?=[\d.?+-]|$)", parenthesis.group(1), maxsplit=1, flags=re.I)[0].strip(" ,;/")
        lens = re.sub(r"^IOL\s*[:=]?\s*", "", lens, flags=re.I).strip()
        if lens and "?" not in lens and lens.lower() not in {"iol", "sharkskin+twin", "sharkskin + twin"}:
            if result["procedure"] in {"CATA", "LENSX+CATA"} or re.search(r"\bIOL\b", parenthesis.group(1), re.I):
                result["iol"] = lens
    if not result["iol"]:
        labelled = re.search(r"\bIOL\s*[:=]\s*([^\n()]+?)(?=\s+\b(?:T|Target|on|TEL)\b|$)", header, re.I)
        if labelled and "?" not in labelled.group(1):
            result["iol"] = labelled.group(1).strip()
    scheduled = re.search(r"\bon\s+([^\n()]+?)(?=\s+TEL\b|$)", header, re.I)
    if scheduled:
        raw = scheduled.group(1).strip(" .;,")
        if raw and "?" not in raw and "{" not in raw:
            result["scheduled_date"] = raw
            parts = re.fullmatch(r"(\d{3,4})[-/.](\d{1,2})[-/.](\d{1,2})", raw)
            compact = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", raw)
            if parts or compact:
                year, month, day = map(int, (parts or compact).groups())
                if year < 1911:
                    year += 1911
                try:
                    result["date_iso"] = date(year, month, day).isoformat()
                except ValueError:
                    pass
    phone = re.search(r"(?im)(?:^\s*[-•]?\s*|\s+)TEL\s*[:：][ \t]*([^\n]*)", text)
    if phone:
        raw = phone.group(1).strip()
        if raw and "?" not in raw:
            result["tel"] = raw
    return result
