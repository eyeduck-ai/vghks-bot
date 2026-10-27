"""Decode clinical responses and scope pacing for report attachments."""
from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from urllib.parse import urlsplit

from vghks_sdk.core.transport import SafeSessionTransport

_report_attachment_scope = ContextVar("report_attachment_scope", default=False)


@contextmanager
def report_attachment_downloads():
    """Skip request jitter only for the current report's binary downloads."""
    token = _report_attachment_scope.set(True)
    try:
        yield
    finally:
        _report_attachment_scope.reset(token)


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
    def __init__(self, *, response_encoding="auto", attachment_base_url="", **kwargs):
        super().__init__(**kwargs)
        self.response_encoding = response_encoding
        base = urlsplit(attachment_base_url)
        self.attachment_origin = (base.scheme.lower(), base.netloc.lower()) if base.netloc else None
        self.attachment_paths = {base.path.rstrip("/") + "/Page/JSP/" + name
                                 for name in ("showPACSPic.jsp", "showPDF.jsp")} if base.netloc else set()
        self.request_dispatch_lock = threading.RLock()
        # Both transports own their request policy, but share the existing
        # authenticated session and TLS choices. Only this outer object closes it.
        self.attachment_transport = SafeSessionTransport(
            policy=replace(self.policy, min_delay_seconds=0, max_delay_seconds=0),
            verify=self.verify, session=self.session, sleeper=self.sleeper, rng=self.rng,
            diagnostics=self.diagnostics, raw_capture=self.raw_capture, connections=self.connections)
        self.closed = False

    def request(self, method, url, **kwargs):
        target = urlsplit(url)
        attachment = (_report_attachment_scope.get() and method.upper() == "GET"
                      and (target.scheme.lower(), target.netloc.lower()) == self.attachment_origin
                      and target.path in self.attachment_paths)
        with self.request_dispatch_lock:
            if attachment:
                child = self.attachment_transport
                # The gateway installs capture sinks after constructing the SDK.
                child.diagnostics, child.raw_capture = self.diagnostics, self.raw_capture
                child.verify, child.connections = self.verify, self.connections
                return child.request(method, url, **kwargs)
            return super().request(method, url, **kwargs)

    def close(self):
        with self.request_dispatch_lock:
            if not self.closed:
                self.closed = True
                super().close()

    def text(self, response):
        return decode_response(response.content or b"", response.headers.get("Content-Type", ""), self.response_encoding)
