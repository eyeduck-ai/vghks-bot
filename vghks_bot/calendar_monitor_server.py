"""Loopback-only control surface for the independent Calendar worker."""
from __future__ import annotations

import hmac
import threading
from urllib.parse import urlsplit

from .server import STATIC, Handler, LocalServer


class MonitorServer(LocalServer):
    def __init__(self, port, application):
        super().__init__(port, application)
        self.RequestHandlerClass = MonitorHandler


class MonitorHandler(Handler):
    def do_GET(self):
        if not self.local_request():
            return
        path = urlsplit(self.path).path
        files = {"/": ("calendar-monitor.html", "text/html; charset=utf-8"),
                 "/calendar-monitor.js": ("calendar-monitor.js", "text/javascript; charset=utf-8"),
                 "/calendar-monitor.css": ("calendar-monitor.css", "text/css; charset=utf-8")}
        if path in files:
            name, mime = files[path]
            self.reply(200, (STATIC / name).read_bytes(), mime=mime)
        elif path == "/api/instance":
            self.reply(200, {"instance": self.server.application.store.get("config", "settings")["worker_id"]})
        elif path == "/api/status" and self.authorized():
            self.reply(200, self.server.application.public())
        elif path != "/api/status":
            self.reply(404, {"error": "找不到此頁面。"})

    def dispatch_post(self, app, path, values):
        if path == "/api/session":
            token = values.get("token", "")
            if not isinstance(token, str) or not hmac.compare_digest(token, app.launch_token):
                self.reply(401, {"error": "請從監控 EXE 開啟的頁面進入。"})
                return
            self.reply(200, {"csrf": app.csrf_token}, cookie=f"opd_session={app.session_token}; HttpOnly; SameSite=Strict; Path=/")
        elif path == "/api/config":
            self.reply(200, app.configure(values))
        elif path == "/api/prepare":
            if type(values.get("takeover", False)) is not bool:
                raise ValueError("主機接替選項格式錯誤。")
            self.reply(200, app.prepare(values.get("takeover", False)))
        elif path == "/api/run":
            self.reply(202, app.run_background())
        elif path == "/api/pause":
            self.reply(200, app.pause())
        elif path == "/api/remove-key":
            self.reply(200, app.remove_key())
        elif path == "/api/close":
            self.reply(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self.reply(404, {"error": "找不到此操作。"})
