"""Local commit receipts and reclamation of confirmed, temporary cloud markers.

The receipt is persisted BEFORE deleting its marker. Uncertain operations and
markers owned by another local database are never reclaimed. No system tab or
clinical column is added for the operation journal.
"""
from __future__ import annotations

from .google_sheets import SheetError

OPD_KEY = "vghks.opd.update"
MANAGEMENT_KEY = "vghks.sheet.management"


def is_confirmed(record, token):
    return token in record.get("confirmed_markers", [])


def confirm(record, token):
    record["confirmed_markers"] = list(dict.fromkeys([*record.get("confirmed_markers", []), token]))


def reclaim(client, book, receipts):
    """receipts: (record, metadata_key, confirmed_tokens, persist_callback).

    Metadata is only a temporary commit witness. Permanent event/source identity
    stays in the existing hidden columns/baseline, independent of these receipts.
    """
    selected, requests_ = [], []
    metadata = book.get("developerMetadata", [])
    for record, key, tokens, save in receipts:
        pending = {token for token in tokens if is_confirmed(record, token)
                   or record.get("status") in {"copied", "applied", "applied_changed"}} - set(record.get("retired_markers", []))
        if not pending:
            continue
        for token in pending:
            confirm(record, token)
            # Include both key and value; never delete another operation's ID.
            if any(m.get("metadataKey") == key and m.get("metadataValue") == token for m in metadata):
                requests_.append({"deleteDeveloperMetadata": {"dataFilter": {"developerMetadataLookup": {
                    "metadataKey": key, "metadataValue": token, "visibility": "DOCUMENT"}}}})
        save(record)  # Durable proof must survive a crash after marker deletion.
        selected.append((record, pending, save))
    try:
        for start in range(0, len(requests_), 100):
            client.write(requests_[start:start + 100])
    except SheetError:
        # Cleanup failure cannot turn a confirmed clinical write into a failure.
        # A later operation will retry reclamation using the same local receipts.
        return False
    for record, tokens, save in selected:
        record["retired_markers"] = list(dict.fromkeys([*record.get("retired_markers", []), *sorted(tokens)]))
        save(record)
    return True
