"""Literal tags in the full SOAP or an explicitly identified SDK section."""
from __future__ import annotations

import re
from bisect import bisect_left

from .settings import Settings
from .soap_data import SCOPE_LABELS, scope_sources, scope_status
from .surgery import parse_surgery

SECTION = re.compile(r"(?im)^(?:[SOAP]|Subjective|Objective|Assessment|Plan|主訴|診斷|計畫)\s*[:：]")


def extract_tags(value: str | dict, settings: Settings) -> list[dict]:
    record = {"soap": value} if isinstance(value, str) else value
    full_text = record.get("soap", "")
    hits = {}
    for category in settings.categories:
        for source in scope_sources(record, category.scope):
            for keyword in category.keywords:
                for found in re.finditer(re.escape(keyword), source["text"], re.IGNORECASE):
                    key = (source["field"], source["index"], found.start(), category.id)
                    if key not in hits or found.end() > hits[key][0]:
                        hits[key] = (found.end(), category, keyword, source)
    headings = {}
    for (field, index, start, _), (_, _, _, source) in hits.items():
        if not source["text"][source["text"].rfind("\n", 0, start) + 1:start].strip():
            headings.setdefault((field, index), set()).add(start)
    headings = {key: sorted(positions) for key, positions in headings.items()}
    result = []
    for (field, index, start, _), (keyword_end, category, keyword, source) in hits.items():
        text = source["text"]
        positions = headings.get((field, index), [])
        next_index = bisect_left(positions, keyword_end)
        end = positions[next_index] if next_index < len(positions) else len(text)
        tail = text[keyword_end:end]
        breaks = [m.start() for m in (re.search(r"\n[ \t\r]*\n", tail), SECTION.search(tail)) if m]
        if breaks:
            end = keyword_end + min(breaks)
        excerpt = text[start:end].rstrip()
        surgery = parse_surgery(excerpt) if category.parser == "surgery" else None
        # Structured offsets are always exact in their own field. Only project
        # onto the full text if there is a unique source; repeated identical
        # text in S and A+P must never highlight the wrong section.
        base = 0 if field == "soap" else full_text.find(text) if text and full_text.count(text) == 1 else -1
        result.append({
            "category": category.id, "category_name": category.name, "keyword": keyword,
            "header": excerpt.splitlines()[0], "excerpt": excerpt,
            "start": base + start if base >= 0 else None,
            "end": base + start + len(excerpt) if base >= 0 else None,
            "keyword_end": base + keyword_end if base >= 0 else None,
            "scope": category.scope, "scope_name": SCOPE_LABELS[category.scope],
            "source_field": field, "source_index": index, "source_start": start,
            "source_end": start + len(excerpt), "source_keyword_end": keyword_end,
            "surgery": surgery,
        })
    return sorted(result, key=lambda hit: (hit["start"] if hit["start"] is not None else len(full_text),
                                          hit["source_field"], hit["source_index"] or 0,
                                          hit["source_start"], hit["category"]))


def classification(record: dict, settings: Settings) -> dict:
    issues = []
    for tag in settings.categories:
        if tag.keywords and (status := scope_status(record, tag.scope)) != "ready":
            issues.append({"category": tag.id, "name": tag.name, "scope": tag.scope,
                           "scope_name": SCOPE_LABELS[tag.scope], "status": status})
    return {"matches": extract_tags(record, settings), "tag_scope_issues": issues}
