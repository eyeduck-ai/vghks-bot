"""Bounded, credential-redacted response evidence and safe navigation metadata."""
from __future__ import annotations

import hashlib
import json
import re
from http.cookies import SimpleCookie
from types import SimpleNamespace
from urllib.parse import parse_qsl, unquote, urlsplit

from vghks_sdk.core.diagnostics import _response_structure, _safe_field_name, _safe_path
from vghks_sdk.core.operations import OPERATIONS

from .encoding import decode_response

RESPONSE_LIMIT = 4 * 1024 * 1024
BUFFER_LIMIT = 8 * 1024 * 1024
EVIDENCE_LIMIT = 16 * 1024 * 1024
TRACE_LIMIT = 2 * 1024 * 1024
EVENT_LIMIT = 512
_STATIC_SEGMENTS = {segment for spec in OPERATIONS for segment in spec.path.split("/") if segment}
_STATIC_SEGMENTS.update({"common", "comm", "error", "errors", "exception", "auth", "session", "login", "jsp"})
_ERROR_ENDPOINT = re.compile(r"(?i)^(?=[a-z_-]*(?:error|exception|login|session|timeout|expired|denied))[a-z_-]{1,80}\.(?:jsp|do|html?)$")
_CREDENTIAL = re.compile(r"(?i)^(?:.*token|.*password|.*passwd|.*pwd|pswd|pass|hid|sid|session(?:id)?|jsessionid|user(?:name|id|key)|login(?:name|id)|authorization|cookie|client_secret|credential)$")
_ATTRIBUTE = re.compile(r"(?i)\b([\w:-]+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")
_INPUT = re.compile(r"(?is)<(?:input|meta)\b[^>]*>")
_NAMED_VALUE = re.compile(r"(?i)(?<![\w.-])([\"']?([a-z_][a-z0-9_.-]{0,119})[\"']?\s*[:=]\s*)(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')")
_QUERY_VALUE = re.compile(r"([?&])([\w.-]+)=([^&\s\"'<>#]+)")
_SESSION_PATH = re.compile(r"(?i)(;jsessionid=)[^/?#\s\"'<>]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
REDACTED = "[REDACTED]"


def safe_path(url):
    """Keep registered routes and recognizable error pages, never URL values."""
    path = unquote(urlsplit(str(url)).path or "/")
    path = _SESSION_PATH.sub(r"\1[REDACTED]", path)
    parts = []
    for part in path.split("/"):
        if part:
            parts.append(part if part in _STATIC_SEGMENTS or _ERROR_ENDPOINT.fullmatch(part)
                         else "<redacted-segment>")
    return "/" + "/".join(parts) if parts else _safe_path(url)


def navigation(url):
    path = safe_path(url)
    actual = unquote(urlsplit(str(url)).path or "/")
    return {"path": path, "path_redacted": path != actual,
            "query_keys": sorted({_safe_field_name(key) for key, _ in parse_qsl(urlsplit(str(url)).query)}),
            "scheme": urlsplit(str(url)).scheme if urlsplit(str(url)).scheme in {"http", "https"} else ""}


def cookie_names(headers):
    values = SimpleCookie()
    try:
        values.load(str(headers.get("Set-Cookie", "")))
    except Exception:
        return []
    return sorted({_safe_field_name(name) for name in values})


class Redactor:
    """Keep a small memory-only set of credential values observed in requests."""

    def __init__(self):
        self.secrets = set()

    def remember(self, value):
        if isinstance(value, str) and 1 <= len(value) <= 8192 and len(self.secrets) < 128:
            self.secrets.add(value)

    def request(self, url, kwargs):
        pairs = list(parse_qsl(urlsplit(str(url)).query))
        for field in ("params", "data", "json"):
            mapping = kwargs.get(field)
            if isinstance(mapping, dict):
                pairs.extend(mapping.items())
            elif isinstance(mapping, (tuple, list)):
                pairs.extend(item for item in mapping if isinstance(item, (tuple, list)) and len(item) == 2)
        for key, value in pairs:
            if _CREDENTIAL.fullmatch(str(key)):
                self.remember(value)
        for key, value in (kwargs.get("headers") or {}).items():
            if str(key).lower() in {"authorization", "proxy-authorization"}:
                self.remember(value)
                self.remember(str(value).split(" ")[-1])
            if str(key).lower() == "cookie":
                for part in str(value).split(";"):
                    self.remember(part.partition("=")[2].strip())

    def response(self, headers):
        values = SimpleCookie()
        try:
            values.load(str(headers.get("Set-Cookie", "")))
        except Exception:
            return
        for value in values.values():
            self.remember(value.value)

    def text(self, source):
        def input_tag(match):
            tag = match.group()
            attributes = {m.group(1).lower(): m.group(2).strip("\"'") for m in _ATTRIBUTE.finditer(tag)}
            if not any(_CREDENTIAL.fullmatch(attributes.get(key, "")) for key in ("name", "id")):
                return tag
            return _ATTRIBUTE.sub(lambda attr: f'{attr.group(1)}="{REDACTED}"'
                                  if attr.group(1).lower() in {"value", "content"} else attr.group(), tag)

        def named_value(match):
            return match.group(1) + '"' + REDACTED + '"' if _CREDENTIAL.fullmatch(match.group(2)) else match.group()

        source = _INPUT.sub(input_tag, source)
        source = _NAMED_VALUE.sub(named_value, source)
        source = _QUERY_VALUE.sub(lambda match: match.group(1) + match.group(2) + "=" + REDACTED
                                 if _CREDENTIAL.fullmatch(match.group(2)) else match.group(), source)
        source = _SESSION_PATH.sub(r"\1[REDACTED]", source)
        source = _JWT.sub(REDACTED, source)
        for secret in sorted(self.secrets, key=len, reverse=True):
            source = (source.replace(secret, REDACTED) if len(secret) >= 4 else
                      re.sub(r"(?<!\w)" + re.escape(secret) + r"(?!\w)", REDACTED, source))
        return source


def response_snapshot(request_id, response, elapsed_seconds, limit=RESPONSE_LIMIT):
    raw = response.content or b""
    headers = response.headers or {}
    return {"request_id": request_id, "http_status": int(response.status_code),
            "response_bytes": len(raw), "elapsed_ms": round(max(0, elapsed_seconds) * 1000, 1),
            "navigation": navigation(response.url),
            "redirects": [{"http_status": int(prior.status_code), **navigation(prior.url),
                           "location": navigation(prior.headers.get("Location", ""))}
                          for prior in (getattr(response, "history", ()) or ())[:20]],
            "headers": {key.lower(): str(headers[key])[:256] for key in
                        ("Content-Type", "Content-Length", "Content-Encoding", "Retry-After", "Cache-Control") if key in headers},
            "set_cookie_names": cookie_names(headers), "_raw": raw[:limit],
            "truncated": len(raw) > limit}


def response_shape(snapshot, encoding, redactor=None):
    source = decode_response(snapshot.get("_raw", b""), snapshot.get("headers", {}).get("content-type", ""), encoding)
    if redactor:
        source = redactor.text(source)
    sample = SimpleNamespace(content=source.encode("utf-8"), headers={"Content-Type": "text/html; charset=utf-8"}
                             if "html" in snapshot.get("headers", {}).get("content-type", "")
                             else {"Content-Type": "text/plain; charset=utf-8"})
    result = dict(_response_structure(sample))
    result.update(byte_length=snapshot["response_bytes"], shape_truncated=snapshot["truncated"],
                  mime_type=snapshot.get("headers", {}).get("content-type", "").partition(";")[0])
    return result


def file_reference(path):
    content = path.read_bytes()
    return {"name": path.name, "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}


def save_response(directory, operation_id, snapshot, redactor, encoding, remaining):
    raw = snapshot.get("_raw", b"")
    source = decode_response(raw, snapshot.get("headers", {}).get("content-type", ""), encoding)
    sanitized = redactor.text(source)
    changed = sanitized != source
    content = sanitized.encode("utf-8")
    binary = content if changed else raw
    files, written = [], 0
    truncated = snapshot["truncated"]
    for suffix, value in (("bin", binary), ("html", content)):
        allowed = min(RESPONSE_LIMIT, remaining - written)
        if allowed <= 0:
            truncated = True
            break
        if len(value) > allowed:
            truncated = True
            value = value[:allowed]
            if suffix == "html" or changed:
                value = value.decode("utf-8", errors="ignore").encode("utf-8")
        path = directory / f"response-{operation_id}-{snapshot['request_id']}.{suffix}"
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(value)
        temporary.replace(path)
        files.append(file_reference(path))
        written += len(value)
    return {"request_id": snapshot["request_id"], "original_bytes": snapshot["response_bytes"],
            "redacted": changed, "truncated": truncated, "binary_encoding": "utf-8" if changed else "original",
            "source_encoding": "utf-8", "files": files}, written


def json_line(row):
    return json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
