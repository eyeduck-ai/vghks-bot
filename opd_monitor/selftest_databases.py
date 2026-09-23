"""Synthetic database-management and review queries, including frozen builds."""
import hashlib
import http.client
import json
import threading
from types import SimpleNamespace

from .bot_server import BotServer
from .databases import DatabaseManager, inspect_database, safe_files
from .selftest_bot import BotSyntheticSDK, wait_task
from .selftest_portable import synthetic_google_key
from .settings import Settings


def contents(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in safe_files(path) if p.name != ".lock"}


def check_databases(path):
    manager = DatabaseManager(path, Settings(), BotSyntheticSDK)
    try:
        source = manager.current.directory
        original = manager.active_entry
        key = manager.current.login({"username": "TEST", "password": "portable-synthetic"})["account"]["id"]
        work = manager.current.workspace(key)
        query = work.review.start({"kind": "approval_search"})["task_id"]
        assert wait_task(work, query)["status"] == "completed"
        assert work.approvals.results({"id": query})["total"] == 3
        part = work.review.start({"kind": "approval_case", "apply_seq": "1001", "parts": ["detail", "orders", "attachments", "pacs"]})["task_id"]
        assert wait_task(work, part)["status"] == "completed"
        work.patient_tags_update({"mrns": "TEST001", "new_tag": "合成群組"})
        asset = work.analysis.store.save_asset("TEST001", "synthetic-pdf", SimpleNamespace(content=b"%PDF-1.4\nsynthetic"), "TEST")
        work.analysis.google.save({"spreadsheet_id": "synthetic-database-sheet-00000000", "key": synthetic_google_key()})
        resolve = work.review.start({"kind": "resolve", "identifiers": "TEST001"})["task_id"]
        assert wait_task(work, resolve)["status"] == "completed"
        group = work.review.save_set({"mrns": "TEST001"})
        review = work.review.start({"set_id": group["id"], "department_confirmed": True})["task_id"]
        assert wait_task(work, review)["status"] == "completed"
        tracking = work.review.start({"kind": "approval_refresh"})["task_id"]
        follow_up = wait_task(work, tracking)
        assert follow_up["status"] == "completed" and follow_up["progress"]["done"] == 2
        assert work.approvals.tracker.overview()["counts"]["pending"] == 2
        work.earnings.save_credentials({"national_id": "A123456789", "password": "synthetic-salary"})
        earnings = work.review.start({"kind": "earnings_capture", "all_available": True})["task_id"]
        saved_earnings = wait_task(work, earnings)
        assert saved_earnings["status"] == "completed" and saved_earnings["progress"]["percent"] == 100
        assert not work.earnings.monitor.overview()["due"]
        work.earnings.monitor.preferences({"enabled": False, "hours": 24})
        salary_rows = work.earnings.overview()["reports"]
        assert len(salary_rows) == 4
        report_id = salary_rows[0]["id"]
        # Ensure foreground cleanup has finished before whole-database operations.
        for thread in list(work.review.threads.values()):
            thread.join(5)
        assert work.idle.wait(5)
        backup = manager.handle("backup", {})
        assert backup["database"]["records"] == 2 and backup["database"]["approvals"] == 3
        assert backup["database"]["earnings"] == 4
        before = contents(source)
        manager.open_entry(original, readonly=True)
        assert manager.current.read_only and manager.current.directory != source
        manager.current.offline(key)
        work = manager.current.workspace(key)
        calls = list(BotSyntheticSDK.calls)
        assert work.approvals.results({"id": query})["total"] == 3
        assert work.review.results({"id": review})["total"] == 1
        assert work.earnings.detail({"id": report_id})["version"]["payload"]["tables"]
        assert work.analysis.store.asset(asset["digest"])[0].read_bytes().startswith(b"%PDF-")
        assert calls == BotSyntheticSDK.calls
        assert contents(source) == before, "read-only viewing modified source"
        with BotServer(0, manager.current, manager) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
            thread.start()
            def request(route, values=None, *, context=None, application=None):
                app = application or manager.current
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=8)
                headers = {"Content-Type": "application/json", "Cookie": "opd_session="+app.session_token,
                           "X-CSRF-Token": app.csrf_token, "X-Database-Context": context or app.context_token}
                connection.request("GET" if values is None else "POST", "/api/"+route,
                                   None if values is None else json.dumps(values), headers)
                response = connection.getresponse()
                result = response.status, json.loads(response.read())
                connection.close()
                return result
            try:
                assert request("databases")[0] == 200
                assert request(f"accounts/{key}/approvals/results", {"id": query})[1]["total"] == 3
                assert request(f"accounts/{key}/approvals/tracking", {})[1]["counts"]["pending"] == 2
                assert len(request(f"accounts/{key}/earnings/overview", {})[1]["reports"]) == 4
                assert request(f"accounts/{key}/earnings/detail", {"id": report_id})[0] == 200
                exported = request(f"accounts/{key}/earnings/export", {"ids": [report_id], "format": "csv"})
                assert exported[0] == 200 and "可解析數值" in exported[1]["content"]
                for route, values in [("tasks/start", {"kind": "approval_search"}), ("draft/save", {}),
                                      ("library/delete", {}), ("analysis/google/save", {}),
                                      ("earnings/credentials", {}), ("earnings/delete", {"ids": [report_id]}),
                                      ("earnings/forget", {}), ("approvals/tracking/preferences", {"automatic": False, "hours": 6}),
                                      ("earnings/monitor", {"enabled": True, "hours": 6}),
                                      ("approvals/tracking/change", {"ids": ["1001"], "action": "pause"})]:
                    assert request(f"accounts/{key}/"+route, values)[0] == 400, route
                assert request("accounts/login", {"id": key})[0] == 400
                assert contents(source) == before
                copy = manager.handle("copy", {"name": "可編輯副本"})
                old = manager.current
                assert request("databases/open", {"id": copy["entry"]["id"], "readonly": False})[0] == 200
                assert request("bootstrap", application=old)[0] == 401
                assert request("accounts/offline", {"id": key})[0] == 200
                assert request(f"accounts/{key}/draft/save", {}, context=old.context_token)[0] == 400
                assert request(f"accounts/{key}/draft/save", {"test": "copy"})[0] == 200
                assert manager.current.registry.password(key) == "portable-synthetic"
                assert manager.current.workspace(key).analysis.google.public()["configured"]
                copied_work = manager.current.workspace(key)
                assert copied_work.earnings.credentials().password == "synthetic-salary"
                assert len(copied_work.earnings.overview()["reports"]) == 4
                assert not copied_work.earnings.monitor.preferences()["enabled"]
                assert contents(source) == before
            finally:
                server.shutdown()
                thread.join(3)
        # A backup always opens read-only, even if editing was requested.
        manager.open_entry(backup["entry"]["id"], readonly=False)
        assert manager.current.read_only
        assert inspect_database(source)["records"] == 2
    finally:
        manager.close()
