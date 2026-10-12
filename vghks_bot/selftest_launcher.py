"""Exercise reconnect discovery and database switching in source and frozen builds."""
from __future__ import annotations

import http.client
import json
import threading
from contextlib import closing
from pathlib import Path

from .app import connect_or_start
from .bot_server import BotServer
from .databases import DatabaseManager
from .launcher import INSTANCE_FILE, publish_instance, running_url
from .settings import Settings


def check_launcher(directory):
    home = Path(directory).resolve()
    with closing(DatabaseManager(home, Settings())) as manager:
        with BotServer(0, manager.current, manager) as server, publish_instance(home, server):
            thread = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": .01}, daemon=True)
            thread.start()

            def post(path, values, *, authenticated=False):
                headers = {"Content-Type": "application/json"}
                if authenticated:
                    headers.update(Cookie=f"opd_session={server.application.session_token}",
                                   **{"X-CSRF-Token": server.application.csrf_token})
                with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)) as connection:
                    connection.request("POST", path, json.dumps(values), headers)
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())

            try:
                original = manager.current
                before = manager.catalog_path.read_bytes()
                for _ in range(2):
                    owner, url = connect_or_start(home, Settings(), timeout=0)
                    assert owner is None
                    assert url == f"{server.origin}/#{original.launch_token}"
                    assert manager.current is original
                    assert manager.catalog_path.read_bytes() == before
                assert post("/api/session", {"token": original.launch_token})[0] == 200
                code, created = post("/api/databases/create", {"name": "重新開啟測試"}, authenticated=True)
                assert code == 200
                code, switched = post("/api/databases/open", {"id": created["entry"]["id"]}, authenticated=True)
                assert code == 200
                assert manager.current is not original
                assert running_url(home) == server.origin + switched["reopen"]
                assert post("/api/session", {"token": original.launch_token})[0] == 401
                assert post("/api/session", {"token": manager.current.launch_token})[0] == 200
            finally:
                server.shutdown()
                thread.join(3)
        assert not (home / INSTANCE_FILE).exists()
    # Once the previous owner exits, the same catalog starts normally again.
    manager, url = connect_or_start(home, Settings(), timeout=0)
    try:
        assert manager is not None and url is None
    finally:
        if manager:
            manager.close()
