"""Stream legacy traces; keep only incident-related summaries from new traces."""
from __future__ import annotations

import json


def selected_trace(path, operations, limit):
    output, size, truncated = [], 0, False
    with path.open("rb") as stream:
        compact = None
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if compact is None:
                compact = row.get("recording_policy", {}).get("mode") == "failure_detail"
            if compact and row.get("event") not in {"run_started", "run_finished"} and row.get("operation_id") not in operations:
                continue
            if size + len(line) > limit:
                truncated = True
                break
            output.append(line)
            size += len(line)
    return b"".join(output), truncated
