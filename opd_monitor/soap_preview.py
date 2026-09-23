"""Extract explicitly labelled A/P text without guessing from block position."""
from __future__ import annotations

import re
import unicodedata

_LABELS = {
    "s": "S", "subjective": "S", "主觀": "S", "主訴": "S",
    "o": "O", "objective": "O", "客觀": "O",
    "a": "A", "assessment": "A", "評估": "A", "診斷": "A",
    "p": "P", "plan": "P", "計畫": "P", "計劃": "P", "處置": "P",
    "a+p": "A+P", "a/p": "A+P", "a&p": "A+P",
    "assessmentandplan": "A+P", "assessment/plan": "A+P",
    "assessment&plan": "A+P", "assessment+plan": "A+P",
    "評估與計畫": "A+P", "診斷與計畫": "A+P",
}


def _label(value: str) -> str | None:
    value = unicodedata.normalize("NFKC", value).strip()
    value = re.sub(r"^#{1,6}\s+", "", value)
    value = value.strip(" *_[\u3010\u3011]()")
    return _LABELS.get(re.sub(r"\s+", "", value).casefold())


def ap_preview(value: str | dict) -> list[dict[str, str | bool]]:
    """Use explicit line headings only; unlabeled SDK blocks remain unknown.

    Prefer SDK 0.20's fields, distinguishing absent and explicitly empty values.
    The older plain-text fallback uses labels only, never block position.
    """
    if isinstance(value, dict) and isinstance(value.get("soap_structure"), dict):
        structure = value["soap_structure"]
        return [{"label": label, "text": structure[field], "empty": not structure[field].strip()}
                for field, label in (("assessment_plan", "A+P"), ("assessment", "A"), ("plan", "P"))
                if isinstance(structure.get(field), str)]
    text = value.get("soap", "") if isinstance(value, dict) else value
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        head, separator, body = re.split(r"([:\uff1a])", line, maxsplit=1) if re.search(r"[:\uff1a]", line) else (line, "", "")
        label = _label(head)
        if label:
            current = label
            if current in {"A", "P", "A+P"}:
                sections.setdefault(current, []).append(body.lstrip() if separator else "")
        elif current in {"A", "P", "A+P"}:
            sections[current].append(line)
    return [{"label": label, "text": value} for label, lines in sections.items()
            if (value := "\n".join(lines).strip())]
