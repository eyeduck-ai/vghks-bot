from __future__ import annotations

import argparse
import ctypes
import sys
import time
import webbrowser
from pathlib import Path

from .bot_server import BotServer
from .databases import DatabaseManager, ManagerInUseError
from .launcher import publish_instance, running_url
from .settings import load_defaults
from .storage import StorageError


def connect_or_start(home, settings, *, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return DatabaseManager(home, settings), None
        except ManagerInUseError as exc:
            url = running_url(home)
            if url:
                return None, url
            if time.monotonic() >= deadline:
                raise StorageError(
                    "程式已在執行，但目前無法重新開啟網頁介面。"
                    "若正在啟動或結束，請稍候再雙擊 EXE；若執行中的是舊版，請先結束舊版程式。"
                ) from exc
            # The first process may still be initializing or releasing its lock.
            time.sleep(.2)


def open_interface(url, *, no_browser=False):
    if sys.stdout:
        print(f"VGHKS-bot: {url}", flush=True)
    if no_browser:
        return
    try:
        opened = webbrowser.open(url)
    except (webbrowser.Error, OSError):
        opened = False
    if not opened and sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(0, f"請在瀏覽器開啟：\n{url}", "VGHKS-bot", 0)


def main():
    parser = argparse.ArgumentParser(description="VGHKS-bot")
    parser.add_argument("--port", type=int, default=0, help="本機埠號；預設自動選擇")
    parser.add_argument("--no-browser", action="store_true", help="不自動開啟瀏覽器")
    parser.add_argument("--data-dir", type=Path, help="資料庫管理目錄（含 databases.json 與 datasets）")
    parser.add_argument("--self-test-report", type=Path, help="使用虛構資料執行離線自我測試，寫入報告後離開")
    args = parser.parse_args()
    if args.self_test_report:
        from .selftest_bot import self_test

        return self_test(args.self_test_report)
    manager = None
    try:
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
        manager, url = connect_or_start(args.data_dir or base / "VGHKS-bot-data", load_defaults())
        if url:
            open_interface(url, no_browser=args.no_browser)
            return 0
        app = manager.current
        with BotServer(args.port, app, manager) as server, publish_instance(manager.home, server):
            url = f"{server.origin}/#{app.launch_token}"
            open_interface(url, no_browser=args.no_browser)
            server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        message = str(exc) if isinstance(exc, StorageError) else "無法啟動 VGHKS-bot。請確認設定檔、資料夾權限或埠號。"
        if sys.platform == "win32" and sys.stdout is None:
            ctypes.windll.user32.MessageBoxW(0, message, "VGHKS-bot", 0x10)
        elif sys.stderr:
            print(message, file=sys.stderr)
        return 1
    finally:
        if manager:
            manager.close()
    return 0
