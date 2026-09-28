"""Root account API; all clinical routes require an explicit workspace prefix."""
from __future__ import annotations

import hmac
import re
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from .google_sheets import SheetError
from .jobs import BusyError
from .server import STATIC, Handler, LocalServer
from .storage import StorageError
from .surgery_schedule import EBOARD_URL


class BotServer(LocalServer):
    def __init__(self, port, application, database_manager=None):
        super().__init__(port, application)
        self.RequestHandlerClass = BotHandler
        self.database_manager = database_manager
        self.api_lock = threading.RLock()


class BotHandler(Handler):
    server_version = "VGHKSBot"

    def send_header(self, keyword, value):
        if keyword == "X-Frame-Options":
            value = "SAMEORIGIN"
        elif keyword == "Content-Security-Policy":
            value = value.replace("frame-ancestors 'none'", "frame-ancestors 'self'")
            if "default-src 'self'" in value:
                # Only the intranet board may join our own comparison frames.
                board = urlsplit(EBOARD_URL)
                value += f"; frame-src 'self' {board.scheme}://{board.netloc}"
        super().send_header(keyword, value)

    def scope(self, path):
        found = re.fullmatch(r"/api/accounts/([a-f0-9]{32})(/.*)", path)
        if not found:
            return None
        return self.server.application.workspace(found[1]), "/api" + found[2]

    def failure(self, exc):
        if isinstance(exc, (ValueError, BusyError)):
            self.reply(409 if isinstance(exc, BusyError) else 400, {"error": str(exc)})
        elif isinstance(exc, SheetError):
            self.reply(502, {"error": str(exc)})
        elif isinstance(exc, StorageError):
            self.reply(500, {"error": str(exc)})
        else:
            self.reply(500, {"error": "操作未完成，已保存資料保留。"})

    def do_GET(self):
        with self.server.api_lock:
            self._get()

    def _get(self):
        if not self.local_request():
            return
        url = urlsplit(self.path)
        assets = {"/": ("bot.html", "text/html; charset=utf-8"),
                  "/bot.js": ("bot.js", "text/javascript; charset=utf-8"),
                  "/review-history.js": ("review-history.js", "text/javascript; charset=utf-8"),
                  "/scan-browser.js": ("scan-browser.js", "text/javascript; charset=utf-8"),
                  "/file-compare.js": ("file-compare.js", "text/javascript; charset=utf-8"),
                  "/file-compare.css": ("file-compare.css", "text/css; charset=utf-8"),
                  "/adaptive-identifiers.js": ("adaptive-identifiers.js", "text/javascript; charset=utf-8"),
                  "/review-notes.js": ("review-notes.js", "text/javascript; charset=utf-8"),
                  "/soap-view.js": ("soap-view.js", "text/javascript; charset=utf-8"),
                  "/bot.css": ("bot.css", "text/css; charset=utf-8"),
                  "/choices.js": ("choices.js", "text/javascript; charset=utf-8"),
                  "/dialog-dismiss.js": ("dialog-dismiss.js", "text/javascript; charset=utf-8"),
                  "/choices.css": ("choices.css", "text/css; charset=utf-8"),
                  "/databases.js": ("databases.js", "text/javascript; charset=utf-8"),
                  "/approvals.js": ("approvals.js", "text/javascript; charset=utf-8"),
                  "/monitor-ui.js": ("monitor-ui.js", "text/javascript; charset=utf-8"),
                  "/tool-workspace.js": ("tool-workspace.js", "text/javascript; charset=utf-8"),
                  "/earnings.js": ("earnings.js", "text/javascript; charset=utf-8"),
                  "/surgery-system.js": ("surgery-system.js", "text/javascript; charset=utf-8"),
                  "/surgery-system.css": ("surgery-system.css", "text/css; charset=utf-8"),
                  "/progress.js": ("progress.js", "text/javascript; charset=utf-8"),
                  "/progress.css": ("progress.css", "text/css; charset=utf-8"),
                  "/tool-bridge.js": ("tool-bridge.js", "text/javascript; charset=utf-8"),
                  "/tools": ("index.html", "text/html; charset=utf-8")}
        assets.update({
            f"/review-{name}.svg": (f"review-{name}.svg", "image/svg+xml")
            for name in ("report-current", "report-history", "orders-current", "orders-history",
                         "visits-history", "registration-records", "tag-add", "note-add", "scan-current")
        })
        if url.path in assets:
            name, mime = assets[url.path]
            self.reply(200, (STATIC / name).read_bytes(), mime=mime)
            return
        if not url.path.startswith("/api/"):
            return super().do_GET()
        if not self.authorized():
            return
        try:
            if url.path == "/api/bootstrap":
                self.reply(200, self.server.application.bootstrap())
                return
            if url.path == "/api/databases":
                if not self.server.database_manager:
                    raise ValueError("資料庫管理未啟用。")
                self.reply(200, self.server.database_manager.listing())
                return
            scoped = self.scope(url.path)
            if scoped is None:
                self.reply(404, {"error": "請使用帳號工作區 API。"})
                return
            workspace, path = scoped
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
            if path == "/api/workbench":
                self.reply(200, {"sets": workspace.review.db.all("set"), "tasks": workspace.review.db.all("task"),
                    "sdk_sessions": workspace.review.db.sdk_sessions(),
                    "preferences": workspace.review.preferences(), "categories": workspace.settings.public()["categories"],
                    "approval_counts": workspace.approvals.tracker.overview()["counts"],
                    "approval_monitor_enabled": workspace.approvals.tracker.preferences().get("enabled", True),
                    "draft": workspace.review.db.get("draft", "current", required=False), "online": workspace.gateway.online,
                    "offline_mode": workspace.offline_mode, "recovery_count": workspace.gateway.recovery_count})
            elif path == "/api/tasks/detail":
                self.reply(200, workspace.review.task(params.get("id")))
            else:
                self.delegate(workspace, path + ("?" + url.query if url.query else ""), "GET")
        except Exception as exc:
            self.failure(exc)

    def delegate(self, workspace, path, method):
        original_server, original_path = self.server, self.path
        try:
            self.server = SimpleNamespace(application=workspace, origin=original_server.origin, shutdown=original_server.shutdown)
            self.path = path
            if method == "GET":
                super().do_GET()
            else:
                super().do_POST()
        finally:
            self.server, self.path = original_server, original_path

    def do_POST(self):
        with self.server.api_lock:
            self._post()

    def _post(self):
        if not self.local_request():
            return
        path = urlsplit(self.path).path
        root = self.server.application
        if path != "/api/session" and not self.authorized(mutation=True):
            return
        try:
            if path.startswith("/api/databases/"):
                manager = self.server.database_manager
                if manager is None:
                    raise ValueError("資料庫管理未啟用。")
                result = manager.handle(path.removeprefix("/api/databases/"), self.read_json())
                self.server.application = manager.current
                self.reply(200, result)
                return
            scoped = self.scope(path) if path.startswith("/api/accounts/") and re.match(r"/api/accounts/[a-f0-9]{32}/", path) else None
            if scoped:
                workspace, local = scoped
                if self.server.database_manager and self.headers.get("X-Database-Context") != root.context_token:
                    raise ValueError("資料庫已切換，請重新載入頁面。")
                readonly_routes = {"/api/lists", "/api/library/search", "/api/library/record", "/api/patient-tags/search",
                    "/api/reviews/results", "/api/reviews/history/read", "/api/reviews/notes/read", "/api/extensions/read", "/api/analysis/results", "/api/analysis/raw",
                    "/api/analysis/google", "/api/analysis/surgery/candidates", "/api/analysis/surgery/history",
                    "/api/approvals/overview", "/api/approvals/results", "/api/approvals/detail", "/api/approvals/tracking",
                    "/api/earnings/overview", "/api/earnings/detail", "/api/earnings/export",
                    "/api/approvals/cases", "/api/approvals/sync-history", "/api/approvals/history",
                    "/api/reviews/diagnostics", "/api/tools/state", "/api/surgery/overview"}
                if root.read_only and local not in readonly_routes:
                    raise ValueError("目前是唯讀檢閱；請建立可編輯副本後操作。")
                if local == "/api/surgery/overview":
                    self.reply(200, workspace.surgery_schedule.overview(self.read_json()))
                    return
                if local.startswith("/api/approvals/"):
                    routes = {"overview": lambda _: workspace.approvals.overview(), "results": workspace.approvals.results,
                              "detail": workspace.approvals.detail, "tracking": workspace.approvals.tracker.overview,
                              "tracking/preferences": workspace.approvals.tracker.preferences,
                              "tracking/change": workspace.approvals.tracker.change,
                              "cases": workspace.approvals.cases, "sync-history": workspace.approvals.sync_history,
                              "history": lambda v: {"observations": workspace.approvals.history(v.get("apply_seq"))}}
                    method = routes.get(local.removeprefix("/api/approvals/"))
                    if method is None:
                        raise ValueError("審查功能不存在。")
                    self.reply(200, method(self.read_json()))
                    return
                if local.startswith("/api/earnings/"):
                    routes = {"overview": workspace.earnings.overview, "detail": workspace.earnings.detail,
                              "credentials": workspace.earnings.save_credentials, "forget": workspace.earnings.forget,
                              "export": workspace.earnings.export, "delete": workspace.earnings.delete,
                              "monitor": workspace.earnings.monitor.preferences}
                    method = routes.get(local.removeprefix("/api/earnings/"))
                    if method is None:
                        raise ValueError("薪資業績功能不存在。")
                    self.reply(200, method(self.read_json()))
                    return
                if local in {"/api/start", "/api/lists/browse", "/api/fetch", "/api/analysis/start",
                             "/api/analysis/google/test", "/api/analysis/surgery/preview",
                             "/api/analysis/surgery/apply", "/api/analysis/surgery/check"} and not workspace.gateway.online:
                    raise ValueError("請先登入此帳號，再使用網路功能。")
                custom = {"/api/sets/save", "/api/sets/delete", "/api/sets/cohort", "/api/draft/save",
                          "/api/tasks/start", "/api/tasks/stop", "/api/reviews/results", "/api/reviews/history/read",
                          "/api/reviews/notes/read", "/api/reviews/notes/save", "/api/reviews/reclassify",
                          "/api/reviews/preferences", "/api/accounts/save", "/api/extensions/read"}
                custom |= {"/api/reviews/diagnostics", "/api/tools/state", "/api/tools/state/save"}
                if local in {"/api/accounts/delete", "/api/shutdown", "/api/session"}:
                    raise ValueError("此操作需使用帳號入口。")
                if local not in custom:
                    self.delegate(workspace, local, "POST")
                    return
                values = self.read_json()
                review = workspace.review
                if local == "/api/reviews/diagnostics":
                    result = workspace.diagnostics.query(values)
                elif local == "/api/tools/state":
                    result = review.db.get("draft", "tools", required=False) or {"modules": {}}
                elif local == "/api/tools/state/save":
                    kind = values.get("module")
                    if kind not in {"review", "retina", "cataract", "surgery"}:
                        raise ValueError("工具種類不正確。")
                    group = review.db.get("set", values.get("set_id")) if values.get("set_id") else None
                    previous = review.db.get("draft", "tools", required=False) or {"modules": {}}
                    result = review.db.save("draft", {"id": "tools", "modules": {**previous["modules"], kind: {"set_id": group["id"] if group else ""}}})
                elif local == "/api/sets/save":
                    result = review.save_set(values)
                elif local == "/api/sets/cohort":
                    result = review.cohort(values)
                elif local == "/api/sets/delete":
                    review.db.delete("set", values.get("id"))
                    result = {"ok": True}
                elif local == "/api/draft/save":
                    result = review.db.save("draft", {**values, "id": "current"})
                elif local == "/api/tasks/start":
                    result = review.start(values)
                elif local == "/api/tasks/stop":
                    result = review.stop(values.get("id"))
                elif local == "/api/reviews/results":
                    result = review.results(values)
                elif local == "/api/reviews/history/read":
                    result = review.history.read(values)
                elif local == "/api/reviews/notes/read":
                    result = review.read_notes(values)
                elif local == "/api/reviews/notes/save":
                    result = review.save_note(values)
                elif local == "/api/reviews/reclassify":
                    result = review.reclassify(values.get("id"))
                elif local == "/api/reviews/preferences":
                    result = review.preferences(values)
                elif local == "/api/extensions/read":
                    kind = values.get("kind")
                    if kind not in {"numeric", "registrations"}:
                        raise ValueError("資料類型不正確。")
                    key = values.get("record_id") if kind == "numeric" else values.get("mrn")
                    if kind == "numeric":
                        workspace.library_record(key)
                    result = {"data": review.db.get(kind, key, required=False)}
                else:
                    # The embedded comparison UI can save date preferences but
                    # cannot change workspace identity or bypass root login.
                    for item in values.get("accounts", []):
                        if item.get("id") != workspace.account_id or item.get("username") != workspace.username or item.get("password"):
                            raise ValueError("請從帳號選單更新登入資訊。")
                    result = workspace.save_accounts(values)
                self.reply(200, result)
                return
            values = self.read_json()
            if root.read_only and path not in {"/api/session", "/api/accounts/activate", "/api/accounts/offline", "/api/shutdown"}:
                raise ValueError("目前是唯讀檢閱；請建立可編輯副本後操作。")
            if path == "/api/session":
                token = values.get("token")
                if not isinstance(token, str) or not hmac.compare_digest(token, root.launch_token):
                    self.reply(401, {"error": "請由 EXE 開啟的頁面進入。"})
                    return
                self.reply(200, {"ok": True}, cookie=f"opd_session={root.session_token}; HttpOnly; SameSite=Strict; Path=/")
            elif path == "/api/accounts/login":
                self.reply(200, root.login(values))
            elif path == "/api/accounts/activate":
                self.reply(200, root.activate(values.get("id")))
            elif path == "/api/accounts/offline":
                self.reply(200, root.offline(values.get("id")))
            elif path == "/api/accounts/logout":
                self.reply(200, root.logout(values.get("id")))
            elif path == "/api/accounts/forget":
                self.reply(200, root.forget(values.get("id")))
            elif path == "/api/concurrency":
                self.reply(200, root.set_concurrency(values.get("value")))
            elif path == "/api/shutdown":
                root.request_close()
                self.reply(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.reply(404, {"error": "找不到此功能。"})
        except Exception as exc:
            self.failure(exc)
