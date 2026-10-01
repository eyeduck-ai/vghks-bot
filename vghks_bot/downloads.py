"""Safe display names for local attachment and portable report downloads."""
from __future__ import annotations

import re
from urllib.parse import quote

EXTENSIONS = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png",
              "image/gif": ".gif", "application/zip": ".zip", "text/plain": ".txt"}


def filename(value, mime, fallback="report"):
    name = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', "_", str(value or fallback)).strip(" .")
    extension = EXTENSIONS.get(mime, "")
    if extension and name.casefold().endswith(extension):
        name = name[:-len(extension)]
    name = name[:120].strip(" .") or fallback
    if re.fullmatch(r"(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", name):
        name = "_" + name
    return name + extension


def disposition(name, *, download=False):
    # ASCII fallback and RFC 5987 preserve Chinese names without header injection.
    ascii_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", name) or "report"
    mode = "attachment" if download else "inline"
    return f'{mode}; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name, safe="")}'
