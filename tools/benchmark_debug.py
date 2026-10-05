"""Compare successful recording costs with synthetic HTML, never hospital data."""
from __future__ import annotations

import json
import statistics
import sys
import tempfile
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vghks_sdk.core.diagnostics import DiagnosticRecorder  # noqa: E402

from vghks_bot.debug_trace import TraceRecorder  # noqa: E402


def sample(recorder_type, root):
    recorder = recorder_type(root)
    body = b"<html><table><tr><td>" + b"synthetic " * 26214 + b"</td></tr></table></html>"
    response = SimpleNamespace(content=body, status_code=200, history=[],
        headers={"Content-Type": "text/html; charset=utf-8"},
        url="https://synthetic.invalid/PRQWeb/QueryCaseList.do")
    started = perf_counter()
    for _ in range(100):
        operation = recorder.start_operation(name="get_soap", app_key="prq")
        request = recorder.record_http_request(method="GET", url=response.url, attempt=1, max_attempts=1,
            throttle_delay_seconds=0, tls_verification_enabled=True, kwargs={})
        recorder.record_http_response(request_id=request, response=response, elapsed_seconds=.01)
        recorder.finish_operation(operation_id=operation, name="get_soap", status="OK")
        if isinstance(recorder, TraceRecorder):
            recorder.release_success()
    recorder.finalize(command="synthetic-benchmark", status="OK", exit_code=0)
    return {"operations": 100, "response_bytes_each": len(body),
            "elapsed_ms": round((perf_counter() - started) * 1000, 2),
            "written_bytes": sum(path.stat().st_size for path in recorder.directory.iterdir() if path.is_file()),
            "raw_response_files": len(list(recorder.directory.glob("response-*")))}


def main():
    report = {}
    with tempfile.TemporaryDirectory() as temporary:
        for name, recorder_type in (("sdk_per_response", DiagnosticRecorder), ("failure_detail", TraceRecorder)):
            samples = [sample(recorder_type, Path(temporary) / f"{name}-{index}") for index in range(3)]
            report[name] = {**samples[0], "elapsed_ms": statistics.median(row["elapsed_ms"] for row in samples),
                            "written_bytes": statistics.median(row["written_bytes"] for row in samples)}
    output = ROOT / ".build/debug-benchmark.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
