"""Deterministic waits for synthetic tests, not application timing policies.

Timeouts are failure watchdogs. A held worker must be released explicitly in a
``finally`` block; reaching its watchdog raises instead of silently proceeding.
Predicates and diagnostic state callbacks must be bounded and nonblocking. State
must contain only explicit synthetic status/counts, never clinical payloads.
Thread diagnostics contain code locations, not frame locals or source text.
"""
from __future__ import annotations

import json
import math
import sys
import threading
from collections.abc import Mapping
from time import monotonic

DEFAULT_TIMEOUT = 15
HOLD_TIMEOUT = 60
_ACTIVE = {"queued", "running", "cancelling"}
_PRIVATE_KEYS = {"password", "passwd", "secret", "token", "authorization", "cookie",
                 "credentials", "soap", "payload", "body", "raw", "mrn", "patient_name"}


def _deadline(timeout):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("A finite nonnegative watchdog timeout is required")
    return monotonic() + timeout


def _safe(value, depth=0):
    if depth > 3:
        return "<depth limit>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:256]
    if isinstance(value, Mapping):
        result = {}
        for key, item in list(value.items())[:24]:
            name = key if isinstance(key, str) else "<" + type(key).__name__ + ">"
            private = name.casefold() in _PRIVATE_KEYS or any(
                part in name.casefold() for part in ("password", "secret", "token", "credential", "cookie"))
            result[name] = "<redacted>" if private else _safe(item, depth + 1)
        return result
    if isinstance(value, (tuple, list)):
        return [_safe(item, depth + 1) for item in value[:24]]
    return "<" + type(value).__name__ + ">"


def _state(state):
    try:
        return json.dumps(_safe(state() if callable(state) else state), ensure_ascii=True)
    except Exception as error:
        return "<state unavailable: " + type(error).__name__ + ">"


def _thread_list(threads):
    try:
        return list(threads() if callable(threads) else threads) or threading.enumerate()
    except Exception:
        return threading.enumerate()


def format_diagnostics(label, state=None, threads=()):
    """Return bounded diagnostic text without replacing the original failure.

    This also serves the suite runner for assertions outside these helpers.
    Only code locations are shown; frame locals and source lines are excluded.
    """
    lines = ["Async diagnostics: " + (label if isinstance(label, str) else type(label).__name__),
             "state=" + _state(state)]
    try:
        frames = sys._current_frames()
        for thread in _thread_list(threads)[:16]:
            lines.append(f"thread={thread.name} ident={thread.ident} alive={thread.is_alive()}")
            frame = frames.get(thread.ident)
            locations = []
            while frame is not None and len(locations) < 16:
                locations.append(f"  {frame.f_code.co_filename}:{frame.f_lineno} in {frame.f_code.co_name}")
                frame = frame.f_back
            lines.extend(reversed(locations))
    except Exception as error:
        lines.append("<thread stacks unavailable: " + type(error).__name__ + ">")
    text = "\n".join(lines)
    return text if len(text) <= 24000 else text[:24000] + "\n... diagnostics truncated"


def _failure(label, state, threads, reason="watchdog expired"):
    raise AssertionError(f"Async wait failed: {label} ({reason})\n" + format_diagnostics(label, state, threads))


def _wait(predicate, *, deadline, label, state, threads, pause, interval):
    while True:
        value = predicate()
        if value:
            return value
        remaining = deadline - monotonic()
        if remaining <= 0:
            _failure(label, state, threads)
        pause(min(interval, remaining))


def wait_until(predicate, *, label, timeout=DEFAULT_TIMEOUT, state=None, threads=(), interval=.01):
    """Return the predicate's truthy value under one monotonic deadline."""
    if not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval <= 0:
        raise ValueError("A finite positive polling interval is required")
    return _wait(predicate, deadline=_deadline(timeout), label=label, state=state, threads=threads,
                 pause=threading.Event().wait, interval=interval)


def wait_event(event, *, label, timeout=DEFAULT_TIMEOUT, state=None, threads=()):
    """Wait for an explicit event; timeout is a failure, never a release."""
    if state is None:
        def state():
            return {"event_set": event.is_set()}
    return _wait(event.is_set, deadline=_deadline(timeout), label=label, state=state, threads=threads,
                 pause=event.wait, interval=max(timeout, .001))


def join_threads(threads, *, label, timeout=DEFAULT_TIMEOUT, state=None, preserve_error=False):
    """Check all started threads exited within one shared deadline.

    In a test's ``finally`` use ``preserve_error=True`` to attach a cleanup
    failure to an existing exception rather than replacing its original cause.
    Unstarted threads are ignored so cleanup can handle a failed earlier start.
    """
    deadline, active_error = _deadline(timeout), sys.exc_info()[1]
    workers = list(threads)
    if state is None:
        def state():
            return {"alive_threads": sum(worker.is_alive() for worker in workers)}
    try:
        for thread in workers:
            if thread is threading.current_thread():
                raise ValueError("Cannot join the current test thread")
            if thread.ident is not None:
                thread.join(max(0, deadline - monotonic()))
        if any(thread.is_alive() for thread in workers):
            _failure(label, state, workers, "worker did not exit")
    except AssertionError as error:
        if preserve_error and active_error is not None:
            active_error.add_note(str(error))
            return
        raise


def _workspace_threads(workspace):
    review = getattr(workspace, "review", None)
    if review is None:
        return []
    if not review.lock.acquire(blocking=False):
        return None
    try:
        return list(review.threads.values())
    finally:
        review.lock.release()


def _workspace_state(workspace):
    manager = getattr(workspace, "task_manager", None)
    return {"idle": workspace.idle.is_set(), "foreground": len(manager.active) if manager else 0,
            "pending": len(manager.pending) if manager else 0}


def drain_workspace(workspace, *, label="workspace idle", timeout=DEFAULT_TIMEOUT):
    """Wait for background idle, foreground admission, and known worker exits."""
    deadline, observed = _deadline(timeout), set()

    def drained():
        threads = _workspace_threads(workspace)
        if threads is None:
            return False
        observed.update(threads)
        state = _workspace_state(workspace)
        return state["idle"] and not state["foreground"] and not state["pending"] and not threads

    wait_until(drained, label=label, timeout=max(0, deadline - monotonic()),
               state=lambda: _workspace_state(workspace), threads=lambda: observed)
    join_threads(observed, label=label + " worker exit", timeout=max(0, deadline - monotonic()),
                 state=lambda: _workspace_state(workspace))


def wait_workspace_task(workspace, key, *, timeout=60, drain=True):
    """Return a terminal task and optionally drain its workspace before mocks end.

    Set ``drain=False`` when another task is deliberately held for a race test.
    Diagnostic state is limited to task status, counters, and progress percent.
    """
    deadline, latest = _deadline(timeout), {}

    def terminal():
        task = workspace.review.task(key)
        latest.update(status=task.get("status"), done=task.get("done"), total=task.get("total"),
                      percent=(task.get("progress") or {}).get("percent"))
        return task if task["status"] not in _ACTIVE else None

    task = wait_until(terminal, label="task terminal", timeout=max(0, deadline - monotonic()), state=latest)
    if drain:
        drain_workspace(workspace, timeout=max(0, deadline - monotonic()))
    return task
