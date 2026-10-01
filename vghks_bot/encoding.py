"""Decode PRQ bytes before SDK parsers see them, preserving valid Big5 text."""
from __future__ import annotations

import re

from vghks_sdk.core.transport import SafeSessionTransport


def decode_response(content: bytes, content_type: str = "", encoding: str = "auto") -> str:
    if content.startswith(b"\xef\xbb\xbf"):
        return content.decode("utf-8-sig", errors="replace")
    if encoding != "auto":
        return content.decode("cp950" if encoding == "big5" else "utf-8", errors="replace")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        pass
    meta = re.search(rb'<meta\b[^>]*\bcharset\s*=\s*["\x27\s]*([a-zA-Z0-9_-]+)', content[:16384], re.I)
    declared = re.search(r"charset\s*=\s*[\"'\s]*([a-zA-Z0-9_-]+)", content_type, re.I)
    charset = (meta.group(1).decode("ascii") if meta else declared.group(1) if declared else "").lower()
    if charset in {"utf-8", "utf8"}:
        try:
            return content.decode("cp950")
        except UnicodeDecodeError:
            utf8 = content.decode("utf-8", errors="replace")
            big5 = content.decode("cp950", errors="replace")
            return min((utf8, big5), key=lambda text: text.count("\ufffd"))
    # PRQ legacy pages are Big5/CP950. One damaged byte elsewhere in a page
    # must not cause all names and demographics to be decoded as UTF-8.
    return content.decode("cp950", errors="replace")


class ClinicalTransport(SafeSessionTransport):
    def __init__(self, *, response_encoding="auto", **kwargs):
        super().__init__(**kwargs)
        self.response_encoding = response_encoding

    def text(self, response):
        return decode_response(response.content or b"", response.headers.get("Content-Type", ""), self.response_encoding)
