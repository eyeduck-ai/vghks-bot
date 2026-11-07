"""Check synthetic wait watchdogs without depending on wall-clock speed."""
import contextlib
import io
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from async_helpers import drain_workspace, join_threads, wait_event, wait_until, wait_workspace_task

from tools.test_suite import Result, reports


class AsyncWaitTests(unittest.TestCase):
    def test_ready_event_and_truthy_predicate_return_without_delay(self):
        event = threading.Event()
        event.set()
        self.assertTrue(wait_event(event, label="already ready", timeout=0))
        value = {"status": "completed"}
        self.assertIs(wait_until(lambda: value, label="ready value", timeout=0), value)

    def test_timeout_identifies_wait_state_and_worker_locations_without_locals(self):
        entered, release = threading.Event(), threading.Event()

        def held_worker():
            local_secret = "synthetic-private-frame-value"
            entered.set()
            release.wait()
            return local_secret

        thread = threading.Thread(target=held_worker, name="synthetic-held-worker")
        thread.start()
        try:
            wait_event(entered, label="worker started")
            with self.assertRaises(AssertionError) as failure:
                wait_event(threading.Event(), label="response published", timeout=0,
                           state={"completed": 2, "password": "synthetic-private-state-value",
                                  "access_token": "synthetic-private-token-value"}, threads=(thread,))
            message = str(failure.exception)
            for expected in ("response published", '"completed": 2', "synthetic-held-worker", "held_worker", ":"):
                self.assertIn(expected, message)
            self.assertNotIn("synthetic-private-frame-value", message)
            self.assertNotIn("synthetic-private-state-value", message)
            self.assertNotIn("synthetic-private-token-value", message)
            self.assertNotIn("local_secret", message)
            self.assertFalse(release.is_set(), "a wait watchdog released the worker")
        finally:
            release.set()
            join_threads((thread,), label="diagnostic test cleanup", preserve_error=True)

    def test_state_callback_error_does_not_hide_the_wait_failure(self):
        def broken_state():
            raise ValueError("synthetic-private-exception-value")

        with self.assertRaises(AssertionError) as failure:
            wait_until(lambda: False, label="expected condition", timeout=0, state=broken_state)
        self.assertIn("expected condition", str(failure.exception))
        self.assertIn("state unavailable: ValueError", str(failure.exception))
        self.assertNotIn("synthetic-private-exception-value", str(failure.exception))

    def test_wait_uses_monotonic_deadline_even_when_wall_time_is_frozen(self):
        pauses = []
        sleeper = SimpleNamespace(wait=pauses.append)
        with (patch("async_helpers.monotonic", side_effect=(10, 10.25, 10.75, 11)),
              patch("async_helpers.threading.Event", return_value=sleeper),
              patch("time.time", return_value=-1000)):
            with self.assertRaisesRegex(AssertionError, "monotonic condition"):
                wait_until(lambda: False, label="monotonic condition", timeout=1, interval=.5)
        self.assertEqual(pauses, [.5, .25])

    def test_invalid_timeouts_and_poll_intervals_are_rejected(self):
        for timeout in (-1, float("inf"), float("nan"), True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                wait_event(threading.Event(), label="invalid timeout", timeout=timeout)
        for interval in (0, -1, float("inf"), float("nan")):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                wait_until(lambda: False, label="invalid interval", interval=interval)

    def test_join_workers_share_one_deadline_and_ignore_unstarted_threads(self):
        calls = []

        class Worker:
            def __init__(self, ident):
                self.ident = ident

            def join(self, timeout):
                calls.append((self.ident, timeout))

            def is_alive(self):
                return False

        unstarted = threading.Thread(target=lambda: None)
        with patch("async_helpers.monotonic", side_effect=(100, 102, 109)):
            join_threads((Worker(1), Worker(2), unstarted), label="all workers", timeout=10)
        self.assertEqual(calls, [(1, 8), (2, 1)])

    def test_cleanup_timeout_preserves_the_original_assertion(self):
        entered, release = threading.Event(), threading.Event()

        def held_worker():
            entered.set()
            release.wait()

        worker = threading.Thread(target=held_worker, name="cleanup-held-worker")
        worker.start()
        try:
            wait_event(entered, label="cleanup worker started")
            with self.assertRaisesRegex(AssertionError, "primary assertion") as failure:
                try:
                    raise AssertionError("primary assertion")
                finally:
                    join_threads((worker,), label="cleanup secondary", timeout=0, preserve_error=True)
            self.assertIn("cleanup secondary", "\n".join(failure.exception.__notes__))
            with self.assertRaisesRegex(AssertionError, "worker did not exit"):
                join_threads((worker,), label="normal join failure", timeout=0)
        finally:
            release.set()
            join_threads((worker,), label="cleanup watchdog test exit", preserve_error=True)

    def test_legacy_workspace_drain_does_not_join_its_persistent_worker(self):
        idle = threading.Event()
        idle.set()
        workspace = SimpleNamespace(idle=idle, worker=SimpleNamespace(join=lambda: self.fail("persistent worker joined")))
        drain_workspace(workspace, timeout=0)

    def test_task_wait_can_preserve_an_intentionally_active_sibling(self):
        idle = threading.Event()
        active = threading.Thread(target=lambda: None)
        task = {"status": "completed", "done": 2, "total": 2, "progress": {"percent": 100}}
        workspace = SimpleNamespace(idle=idle,
            review=SimpleNamespace(task=lambda key: task, lock=threading.RLock(), threads={"held": active}),
            task_manager=SimpleNamespace(active={"held"}, pending=[]))
        self.assertIs(wait_workspace_task(workspace, "finished", timeout=0, drain=False), task)
        with self.assertRaisesRegex(AssertionError, "workspace idle") as failure:
            wait_workspace_task(workspace, "finished", timeout=0)
        self.assertIn('"foreground": 1', str(failure.exception))
        workspace.review.threads.clear()
        workspace.task_manager.active.clear()
        idle.set()
        self.assertIs(wait_workspace_task(workspace, "finished", timeout=0), task)

    def test_runner_keeps_original_failures_and_diagnostics_in_logs_and_junit(self):
        entered, release = threading.Event(), threading.Event()

        def held_worker():
            local_secret = "synthetic-private-runner-frame-value"
            entered.set()
            release.wait()
            return local_secret

        class Probe(unittest.TestCase):
            def test_assertion(self):
                self.fail("primary assertion")

            def test_error(self):
                raise RuntimeError("primary runtime error")

            def test_subtest(self):
                with self.subTest(case="synthetic"):
                    self.fail("primary subtest assertion")

            def test_helper(self):
                wait_event(threading.Event(), label="already diagnosed helper", timeout=0)

        worker = threading.Thread(target=held_worker, name="runner-held-worker")
        worker.start()
        try:
            wait_event(entered, label="runner diagnostic worker ready")
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(Probe)
            ids = [test.id() for test in suite]
            log, progress = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(progress):
                result = unittest.TextTestRunner(stream=log, resultclass=Result).run(suite)
                with tempfile.TemporaryDirectory() as temporary:
                    reports(result, Path(temporary), ids)
                    junit = (Path(temporary) / "tests.xml").read_text(encoding="utf-8")
            self.assertEqual((result.testsRun, len(result.failures), len(result.errors)), (4, 3, 1))
            for message in ("primary assertion", "primary runtime error", "primary subtest assertion",
                            "Async diagnostics:", "runner-held-worker", "held_worker"):
                self.assertIn(message, log.getvalue())
                self.assertIn(message, junit)
            self.assertNotIn("synthetic-private-runner-frame-value", log.getvalue() + junit)
            helper_failure = next(message for test, message in result.failures if test.id().endswith("test_helper"))
            self.assertEqual(helper_failure.count("Async diagnostics:"), 1, "helper diagnostics were duplicated")
            self.assertEqual(set(result.timings), set(ids), "failure diagnostics changed completed test IDs")
            with (patch("async_helpers.format_diagnostics", side_effect=ValueError("synthetic-private-diagnostic-error")),
                  contextlib.redirect_stdout(io.StringIO())):
                fallback = unittest.TextTestRunner(stream=io.StringIO(), resultclass=Result).run(Probe("test_assertion"))
            self.assertIn("primary assertion", fallback.failures[0][1])
            self.assertIn("Async diagnostics unavailable: ValueError", fallback.failures[0][1])
            self.assertNotIn("synthetic-private-diagnostic-error", fallback.failures[0][1])
        finally:
            release.set()
            join_threads((worker,), label="runner diagnostic test cleanup", preserve_error=True)
