from __future__ import annotations

import hmac
import json
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .downloads import disposition
from .downloads import filename as download_filename
from .google_sheets import SheetError
from .jobs import Application, BusyError
from .storage import StorageError

STATIC = Path(__file__).with_name("static")


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, application: Application):
        self.application = application
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    def handle_error(self, request, client_address):
        # Do not print request URLs, credentials, SOAP, or clinical data to logs.
        pass


class Handler(BaseHTTPRequestHandler):
    server: LocalServer
    server_version = "OPDMonitor"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_):
        pass

    def reply(self, status, payload, *, mime="application/json; charset=utf-8", cookie=None, filename=None):
        if self.command == "POST" and not getattr(self, "_body_consumed", False):
            # Closing a Windows socket with an unread POST body can reset the
            # response before the browser receives a 401/403. Discard bounded
            # rejected input without parsing it or performing any action.
            self._body_consumed = True
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if 0 < size <= 1048576 and not self.headers.get("Transfer-Encoding"):
                    self.connection.settimeout(2)
                    self.rfile.read(size)
            except (OSError, ValueError):
                pass
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        if filename:
            self.send_header("Content-Disposition", disposition(filename, download=True))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def local_request(self):
        if self.headers.get("Host") != self.server.origin.removeprefix("http://"):
            self.reply(403, {"error": "不允許的本機主機名稱。"})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.reply(403, {"error": "不允許跨網站存取。"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            self.reply(403, {"error": "不允許跨網站存取。"})
            return False
        return True

    def authorized(self, *, mutation=False):
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            token = cookies["opd_session"].value if "opd_session" in cookies else ""
            valid = hmac.compare_digest(token, self.server.application.session_token)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            self.reply(401, {"error": "本機工作階段已失效，請由 EXE 開啟的頁面進入。"})
            return False
        if mutation and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), self.server.application.csrf_token):
            self.reply(403, {"error": "請重新載入頁面後再操作。"})
            return False
        return True

    def do_GET(self):
        if not self.local_request():
            return
        path = urlsplit(self.path).path
        files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/workspace.js": ("workspace.js", "text/javascript; charset=utf-8"),
            "/dialog-dismiss.js": ("dialog-dismiss.js", "text/javascript; charset=utf-8"),
            "/analysis.js": ("analysis.js", "text/javascript; charset=utf-8"),
            "/cataract.js": ("cataract.js", "text/javascript; charset=utf-8"),
            "/cataract-export.js": ("cataract-export.js", "text/javascript; charset=utf-8"),
            "/cataract-numeric.js": ("cataract-numeric.js", "text/javascript; charset=utf-8"),
            "/scan-browser.js": ("scan-browser.js", "text/javascript; charset=utf-8"),
            "/soap-view.js": ("soap-view.js", "text/javascript; charset=utf-8"),
            "/file-compare.js": ("file-compare.js", "text/javascript; charset=utf-8"),
            "/clinical-ui.js": ("clinical-ui.js", "text/javascript; charset=utf-8"),
            "/clinical-ui.css": ("clinical-ui.css", "text/css; charset=utf-8"),
            "/library-data.js": ("library-data.js", "text/javascript; charset=utf-8"),
            "/file-compare.css": ("file-compare.css", "text/css; charset=utf-8"),
            "/review-scan-current.svg": ("review-scan-current.svg", "image/svg+xml"),
            "/patient-tags.js": ("patient-tags.js", "text/javascript; charset=utf-8"),
            "/analysis.css": ("analysis.css", "text/css; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
            "/favicon.svg": ("favicon.svg", "image/svg+xml"),
        }
        if path in files:
            filename, mime = files[path]
            self.reply(200, (STATIC / filename).read_bytes(), mime=mime)
        elif path == "/api/analysis/asset":
            if self.authorized():
                try:
                    params = parse_qs(urlsplit(self.path).query)
                    file, mime = self.server.application.analysis.store.asset(params.get("id", [""])[0])
                    self.asset_reply(file, mime, filename=download_filename(params.get("name", ["附件"])[0], mime),
                                     download=params.get("download", [""])[0] == "1")
                except (ValueError, OSError):
                    self.reply(404, {"error": "附件不存在或已刪除。"})
        elif path in {"/api/bootstrap", "/api/history", "/api/run"}:
            if self.authorized():
                app = self.server.application
                params = parse_qs(urlsplit(self.path).query)
                try:
                    value = app.bootstrap() if path == "/api/bootstrap" else app.history() if path == "/api/history" else app.snapshot(params.get("id", [""])[0], params.get("revision", [""])[0])
                    self.reply(200, value)
                except (ValueError, StorageError):
                    self.reply(400, {"error": "無法讀取此紀錄，原檔已保留。"})
        else:
            self.reply(404, {"error": "找不到此頁面。"})

    def read_json(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("請使用 JSON 格式。")
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("不支援此傳輸格式。")
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 1048576:
            raise ValueError("請求內容大小不正確。")
        self._body_consumed = True
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("請求內容必須是物件。")
        return value

    def asset_reply(self, path, mime, *, filename=None, download=False):
        import re

        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        requested = self.headers.get("Range") if self.command == "GET" else None
        if requested:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested)
            if not match:
                self.reply(416, {"error": "附件範圍不正確。"})
                return
            start, end = int(match[1]), min(int(match[2]), size - 1) if match[2] else size - 1
            if start > end:
                self.reply(416, {"error": "附件範圍不正確。"})
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", mime)
        if filename:
            self.send_header("Content-Disposition", disposition(filename, download=download))
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'self'")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = handle.read(min(remaining, 65536))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if not self.local_request():
            return
        path = urlsplit(self.path).path
        app = self.server.application
        if path != "/api/session" and not self.authorized(mutation=True):
            return
        try:
            values = self.read_json()
            if path == "/api/session":
                token = values.get("token")
                if not isinstance(token, str) or not hmac.compare_digest(token, app.launch_token):
                    self.reply(401, {"error": "啟動連結已失效，請重新執行 EXE。"})
                    return
                self.reply(200, {"ok": True}, cookie=f"opd_session={app.session_token}; HttpOnly; SameSite=Strict; Path=/")
            elif path == "/api/settings":
                self.reply(200, app.save_settings(values))
            elif path == "/api/accounts/save":
                self.reply(200, app.save_accounts(values))
            elif path == "/api/accounts/delete":
                self.reply(200, app.delete_account(values.get("id")))
            elif path == "/api/start":
                self.reply(202, app.start(values))
            elif path == "/api/lists/browse":
                self.reply(202, app.browse(values))
            elif path == "/api/lists":
                self.reply(200, app.patient_list(values))
            elif path == "/api/lists/delete":
                self.reply(200, app.delete_list(values))
            elif path == "/api/fetch":
                self.reply(202, app.fetch_selected(values))
            elif path == "/api/library/data/read":
                self.reply(200, app.library_data.read(values))
            elif path == "/api/library/data/preview-delete":
                self.reply(200, app.library_data.preview(values))
            elif path == "/api/library/data/delete":
                self.reply(200, app.library_data.delete(values))
            elif path == "/api/library/search":
                self.reply(200, app.library_search(values))
            elif path == "/api/library/record":
                self.reply(200, app.library_record(values.get("id")))
            elif path == "/api/library/delete":
                self.reply(200, app.delete_records(values))
            elif path == "/api/patient-tags/search":
                self.reply(200, app.tag_patients(values))
            elif path == "/api/patient-tags/update":
                self.reply(200, app.patient_tags_update(values))
            elif path == "/api/analysis/export/read":
                from .analysis_export import listing
                self.reply(200, listing(app.analysis, values))
            elif path == "/api/analysis/export":
                from .analysis_export import archive
                with archive(app.analysis, values) as (file, name):
                    self.asset_reply(file, "application/zip", filename=name, download=True)
            elif path.startswith("/api/analysis/"):
                self.reply(200, app.analysis.handle(path.removeprefix("/api/analysis/"), values))
            elif path == "/api/reclassify":
                self.reply(202, app.reclassify(values.get("id")))
            elif path == "/api/stop":
                self.reply(200, app.stop(values.get("id")))
            elif path == "/api/shutdown":
                app.request_close()
                self.reply(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.reply(404, {"error": "找不到此功能。"})
        except BusyError as exc:
            self.reply(409, {"error": str(exc)})
        except SheetError as exc:
            self.reply(502, {"error": str(exc)})
        except StorageError as exc:
            self.reply(500, {"error": str(exc)})
        except (ValueError, TypeError, UnicodeError) as exc:
            message = str(exc) if type(exc) is ValueError else "請求格式不正確。"
            self.reply(400, {"error": message})
        except Exception:
            self.reply(500, {"error": "本機程式發生錯誤，請重新啟動。"})
