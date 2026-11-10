"""Decode clinical responses and scope pacing for report attachments."""
from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from urllib.parse import urlsplit

from vghks_sdk import (
    OutpatientPatient,
    PatientBasicInfo,
    PatientDemographics,
    RegistrationRecord,
    SoapRecord,
)
from vghks_sdk.core.response_text import (
    declared_charsets,
    decode_cp950,
    decode_response_text,
    private_use_characters,
)
from vghks_sdk.core.transport import SafeSessionTransport

_report_attachment_scope = ContextVar("report_attachment_scope", default=False)
_prq_decode_scope = ContextVar("prq_decode_scope", default=None)
_EVIDENCE_ENDPOINTS = {"opd": ("POST", "/QueryOPDPatList.do"), "soap": ("GET", "/QueryBillingSOAP.do")}
_CHARSET_LABELS = {
    "utf-8", "utf8", "big5", "big-5", "cp950", "ms950", "big5-hkscs", "big5hkscs",
    "utf-16", "utf-16le", "utf-16be", "iso-8859-1", "windows-1252", "us-ascii",
}
_HTML_REPLACEMENT = re.compile(r"&#(?:[xX]0*[fF]{3}[dD](?![0-9a-fA-F])|0*65533(?![0-9]));?")
_HTML_NULL = re.compile(r"&#(?:[xX]0|0);")
_JS_REPLACEMENT = re.compile(r"\\u(?:[fF]{3}[dD]|\{0*[fF]{3}[dD]\})")
_SAFE_MARKER_BYTES = {
    "utf8_literal": re.compile(rb"\xef\xbf\xbd"),
    "html_numeric_entity": re.compile(rb"&#(?:[xX][fF]{3}[dD]|65533);"),
    "html_null_entity": re.compile(rb"&#(?:[xX]0|0);"),
    "javascript_escape": re.compile(rb"\\u(?:[fF]{3}[dD]|\{[fF]{3}[dD]\})"),
}


@contextmanager
def report_attachment_downloads():
    """Skip request jitter only for the current report's binary downloads."""
    token = _report_attachment_scope.set(True)
    try:
        yield
    finally:
        _report_attachment_scope.reset(token)


@contextmanager
def observe_prq_decoding(operation):
    """Collect only safe diagnostics for one known PRQ read operation."""
    evidence = {}
    if operation is None:
        yield evidence
        return
    method, path = _EVIDENCE_ENDPOINTS[operation]
    active = _prq_decode_scope.get()
    if active is not None and active[:2] == (method, path):
        yield active[2]
        return
    token = _prq_decode_scope.set((method, path, evidence))
    try:
        yield evidence
    finally:
        _prq_decode_scope.reset(token)


def decode_response(content: bytes, content_type: str = "") -> str:
    return decode_response_text(content, content_type)[0]


def text_encoding_notice(value, evidence):
    """Describe only parsed clinical text fields and safe response metadata."""
    if isinstance(value, SoapRecord):
        fields = (value.full_text,)
    elif isinstance(value, (PatientBasicInfo, PatientDemographics)):
        fields = (value.name,)
    elif isinstance(value, (list, tuple)):
        fields = tuple(row.name for row in value if isinstance(row, (OutpatientPatient, RegistrationRecord)))
    else:
        return {}
    texts = tuple(text for text in fields if isinstance(text, str))
    samples = private_use_characters(*texts)
    replacement = any("\ufffd" in text for text in texts)
    if not samples and not replacement:
        return {}
    return {
        "private_use_characters": samples, "replacement_character": replacement,
        **{key: evidence[key] for key in ("http_charset", "meta_charset", "decoded_with") if key in evidence},
    }


def _masked_decode_errors(content, codec):
    if codec == "utf-8-sig":
        # The BOM codec reports error offsets relative to the body after the BOM.
        content = content[3:]
        codec = "utf-8"
    masks = []
    # Never retain the byte values, offsets, or neighboring text. A fixed
    # mask represents the width of each of at most three invalid sequences.
    for _ in range(3):
        try:
            if codec == "cp950":
                decode_cp950(content)
            else:
                content.decode(codec)
        except UnicodeDecodeError as error:
            masks.append("XX" * min(error.end - error.start, 4))
            content = content[error.end:]
        else:
            break
    return masks


def _decode_evidence(content, content_type, text, codec):
    http, meta = declared_charsets(content, content_type)
    masks = _masked_decode_errors(content, codec) if "\ufffd" in text else []
    markers = []
    # These markers concern the entire response. They cannot be attributed to
    # any individual field or used to recover its original value.
    if codec.startswith("utf-8") and "\ufffd" in text and (not masks or "\ufffd" in content.decode(codec, errors="ignore")):
        markers.append("utf8_literal")
    if _HTML_REPLACEMENT.search(text):
        markers.append("html_numeric_entity")
    if _HTML_NULL.search(text):
        markers.append("html_null_entity")
    if _JS_REPLACEMENT.search(text):
        markers.append("javascript_escape")
    # Only exact, public replacement marker byte sequences can leave memory.
    # Never include other invalid bytes or any context around these matches.
    source_samples = []
    for marker in markers:
        match = _SAFE_MARKER_BYTES[marker].search(content)
        if match:
            source_samples.append({"marker": marker, "hex": match.group().hex()})
    return {
        "http_charset": http if http is None or http in _CHARSET_LABELS else "other",
        "meta_charset": meta if meta is None or meta in _CHARSET_LABELS else "other",
        "decoded_with": codec,
        "decoder_replacement": bool(masks),
        "source_markers": markers,
        "source_byte_samples": source_samples,
        "masked_byte_samples": masks,
        "byte_sample_policy": "fixed_markers_only",
        "scope": "response",
    }


class ClinicalTransport(SafeSessionTransport):
    def __init__(self, *, attachment_base_url="", **kwargs):
        super().__init__(**kwargs)
        base = urlsplit(attachment_base_url)
        self.attachment_origin = (base.scheme.lower(), base.netloc.lower()) if base.netloc else None
        self.prq_path = base.path.rstrip("/")
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
        content = response.content or b""
        content_type = response.headers.get("Content-Type", "")
        text, codec = decode_response_text(content, content_type, encoding_hint=getattr(response, "encoding", None))
        scope = _prq_decode_scope.get()
        if scope is not None:
            method, path, evidence = scope
            if getattr(getattr(response, "request", None), "method", "") == method:
                target = urlsplit(getattr(response, "url", "") or "")
                if (target.scheme.lower(), target.netloc.lower()) == self.attachment_origin and target.path == self.prq_path + path:
                    evidence.update(_decode_evidence(content, content_type, text, codec))
        return text
