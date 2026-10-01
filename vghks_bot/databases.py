"""Local database catalog, consistent copies and non-mutating snapshot viewing."""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path

from .bot import BotApplication
from .database_format import FORMAT, MANIFEST, manifest
from .scanner import create_sdk
from .settings import timestamp
from .storage import StorageError, atomic_json, read_json


@contextmanager
def readonly_connection(path):
    # SQLite mode=ro can still create/update WAL shared-memory files. Inspect
    # an isolated file snapshot instead; include committed WAL transactions and
    # recovery journals, but let SQLite rebuild its own shared-memory index.
    paths = [path, path.with_name(path.name+"-wal"), path.with_name(path.name+"-journal")]
    def signature():
        return {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in paths if p.exists()}
    before = signature()
    with tempfile.TemporaryDirectory(prefix="vghks-sqlite-read-") as directory:
        target = Path(directory) / path.name
        for source in paths:
            if source.exists():
                shutil.copy2(source, Path(directory) / source.name)
        if signature() != before:
            raise StorageError("資料庫正在變動，請等待目前任務完成後重試。")
        with closing(sqlite3.connect(target, timeout=10)) as db:
            yield db


def safe_files(directory):
    for base, folders, files in os.walk(directory, followlinks=False):
        for name in [*folders, *files]:
            path = Path(base) / name
            if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 1024:
                raise StorageError("資料庫目錄不能包含捷徑、符號連結或連接點。")
        for name in files:
            yield Path(base) / name


def inspect_database(directory):
    directory = Path(directory).resolve()
    if not directory.is_dir():
        raise ValueError("找不到資料庫目錄，請選擇包含 database.json 的資料夾。")
    info = manifest(directory)
    files = list(safe_files(directory))
    total = sum(p.stat().st_size for p in files)
    registry = directory / "accounts.sqlite3"
    counts = {"accounts": 0, "records": 0, "tasks": 0, "assets": 0, "approvals": 0, "earnings": 0}
    if not registry.is_file():
        raise StorageError("缺少 accounts.sqlite3，資料庫不完整。")
    with readonly_connection(registry) as db:
        check_sqlite(db)
        accounts = db.execute("SELECT id FROM accounts").fetchall()
    counts["accounts"] = len(accounts)
    for (key,) in accounts:
        if not isinstance(key, str) or len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
            raise StorageError("帳號識別資訊不正確。")
        path = directory / "accounts" / key / "clinical.sqlite3"
        if not path.is_file():
            raise StorageError("帳號資料庫不完整，缺少 clinical.sqlite3。")
        with readonly_connection(path) as db:
            check_sqlite(db)
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "records" in tables:
                counts["records"] += db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            if "bot_documents" in tables:
                counts["tasks"] += db.execute("SELECT COUNT(*) FROM bot_documents WHERE kind='task'").fetchone()[0]
                counts["approvals"] += db.execute("SELECT COUNT(*) FROM bot_documents WHERE kind='approval_case'").fetchone()[0]
                counts["earnings"] += db.execute("SELECT COUNT(*) FROM bot_documents WHERE kind='earnings_report'").fetchone()[0]
            if "analysis_assets" in tables:
                counts["assets"] += db.execute("SELECT COUNT(*) FROM analysis_assets").fetchone()[0]
    return {**info, **counts, "path": str(directory), "size": total}


def check_sqlite(db):
    if db.execute("PRAGMA user_version").fetchone()[0] != FORMAT:
        raise StorageError("SQLite 格式不相容，本版不讀取或升級舊格式。")
    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise StorageError("資料庫完整性檢查未通過，來源不會被修改。")


def stamp(directory):
    return {str(p.relative_to(directory)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in safe_files(directory) if p.name != ".lock"}


@contextmanager
def source_lock(directory, owned=False):
    if owned:
        yield
        return
    path = directory / ".lock"
    handle = None
    try:
        if path.exists():
            handle = path.open("r+b")
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        if handle:
            handle.close()
        raise StorageError("來源資料庫正在使用或無法鎖定，請先結束使用它的程式。") from exc
    try:
        yield
    finally:
        if handle:
            handle.close()


def copy_database(source, destination, *, name, snapshot=False, owned=False, preserve_id=False):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("副本目錄不能與來源相同、互相包含。")
    if destination.exists():
        raise ValueError("目的地已存在，請選擇新的資料夾；不會覆寫既有資料。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".vghks-copy-", dir=destination.parent)).resolve()
    try:
        with source_lock(source, owned):
            info = inspect_database(source)
            before = stamp(source)
            for path in list(safe_files(source)):
                if path.name in {".lock", MANIFEST} or path.name.endswith(("-wal", "-shm", "-journal")):
                    continue
                target = staging / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                if path.suffix == ".sqlite3":
                    with readonly_connection(path) as src, closing(sqlite3.connect(target)) as dst:
                        check_sqlite(src)
                        src.backup(dst)
                        check_sqlite(dst)
                else:
                    shutil.copy2(path, target)
            if stamp(source) != before:
                raise StorageError("來源在複製期間有變動，請停止來源任務後重試。")
            meta = manifest(source)
            meta.update(name=name, source_id=info["id"], copied_at=timestamp(), snapshot=snapshot)
            if not preserve_id:
                meta.update(id=uuid.uuid4().hex, created_at=timestamp())
            atomic_json(staging / MANIFEST, meta)
            (staging / ".lock").touch()
            inspect_database(staging)
            staging.rename(destination)
    finally:
        if staging.exists():
            # Only remove the internally generated staging directory we own.
            if staging.parent != destination.parent.resolve() or not staging.name.startswith(".vghks-copy-"):
                raise StorageError("暫存路徑不正確。")
            shutil.rmtree(staging)
    return inspect_database(destination)


class DatabaseManager:
    def __init__(self, home, settings, sdk_factory=create_sdk):
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self._catalog_lock = (self.home / ".manager.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._catalog_lock.seek(0)
                msvcrt.locking(self._catalog_lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self._catalog_lock.close()
            raise StorageError("此資料庫管理目錄已有程式使用，請先結束另一個 EXE。") from exc
        try:
            self.initialize(settings, sdk_factory)
        except Exception:
            self.close()
            raise

    def initialize(self, settings, sdk_factory):
        self.catalog_path = self.home / "databases.json"
        self.settings, self.sdk_factory = settings, sdk_factory
        self.current = None
        self.temporary = None
        self.active_entry = ""
        self.previews = {}
        self.notice = ""
        self.catalog = read_json(self.catalog_path) if self.catalog_path.exists() else {"entries": []}
        if not self.catalog["entries"]:
            path = self.home / "datasets" / uuid.uuid4().hex
            app = BotApplication(settings, path, sdk_factory)
            app.close()
            entry = self.register(path)
            self.catalog["last"] = entry["id"]
        entry = next((e for e in self.catalog["entries"] if e["id"] == self.catalog.get("last")), self.catalog["entries"][0])
        try:
            self.open_entry(entry["id"], readonly=self.catalog.get("last_readonly", False))
        except (StorageError, ValueError, OSError, sqlite3.Error):
            # Keep the manager accessible without modifying an unavailable or
            # unsupported dataset, or silently creating another persistent one.
            self.notice = "上次的資料庫無法開啟。請在資料庫管理選擇可用資料庫，或建立新資料庫。"
            self.temporary = tempfile.TemporaryDirectory(prefix="vghks-database-entry-")
            directory = Path(self.temporary.name) / "database"
            directory.mkdir()
            manifest(directory, create=True, name="尚未開啟資料庫")
            self.current = BotApplication(settings, directory, sdk_factory, read_only=True, source_directory=self.home)
            self.current.database_notice = self.notice

    def path_for(self, entry):
        path = Path(entry["path"])
        return (self.home / path).resolve() if not path.is_absolute() else path.resolve()

    def persist(self):
        atomic_json(self.catalog_path, self.catalog)

    def register(self, path):
        path = Path(path).resolve()
        entry = next((e for e in self.catalog["entries"] if self.path_for(e) == path), None)
        if not entry:
            entry = {"id": uuid.uuid4().hex, "path": str(path.relative_to(self.home)) if path.is_relative_to(self.home) else str(path)}
            self.catalog["entries"].append(entry)
        self.persist()
        return entry

    def ensure_idle(self):
        if not self.current:
            return
        for work in self.current.workspaces.values():
            if not work.idle.is_set() or work.review.foreground or work.analysis.sheet_busy:
                raise ValueError("請先暫停所有帳號的任務，並等待目前查詢完成，再操作資料庫。")

    def entry(self, key):
        found = next((e for e in self.catalog["entries"] if e["id"] == key), None)
        if not found:
            raise ValueError("資料庫不存在於清單中。")
        return found

    def listing(self):
        result = []
        for entry in self.catalog["entries"]:
            try:
                info = inspect_database(self.path_for(entry))
            except Exception as exc:
                info = {"name": self.path_for(entry).name, "path": str(self.path_for(entry)), "error": str(exc)}
            result.append({**info, "entry_id": entry["id"], "active": entry["id"] == self.active_entry})
        return {"databases": result, "read_only": self.current.read_only, "format": FORMAT, "notice": self.notice,
                "active": self.active_entry, "home": str(self.home)}

    def open_entry(self, key, *, readonly=False):
        with self.current.lock if self.current else nullcontext():
            return self._open_entry(key, readonly=readonly)

    def _open_entry(self, key, *, readonly=False):
        self.ensure_idle()
        entry = self.entry(key)
        source = self.path_for(entry)
        info = inspect_database(source)
        readonly = bool(readonly or info.get("snapshot"))
        if self.current and self.active_entry == key and self.current.read_only == readonly:
            return
        temp = None
        old, old_temp = self.current, self.temporary
        try:
            if readonly:
                temp = tempfile.TemporaryDirectory(prefix="vghks-readonly-")
                directory = Path(temp.name) / "database"
                copy_database(source, directory, name=info["name"], snapshot=True, preserve_id=True,
                              owned=bool(old and old.directory == source))
                current = BotApplication(self.settings, directory, self.sdk_factory, read_only=True, source_directory=source)
            else:
                if old and old.directory == source:
                    old.close()
                current = BotApplication(self.settings, source, self.sdk_factory)
        except Exception:
            if temp:
                temp.cleanup()
            raise
        if old:
            old.close()
        if old_temp:
            old_temp.cleanup()
        self.current, self.temporary, self.active_entry = current, temp, key
        self.notice = ""
        self.catalog.update(last=key, last_readonly=readonly)
        self.persist()

    @staticmethod
    def name(values, fallback):
        name = str(values.get("name", fallback)).strip()
        if not 1 <= len(name) <= 100:
            raise ValueError("資料庫名稱需為 1–100 字。")
        return name

    def handle(self, action, values):
        with self.current.lock if self.current else nullcontext():
            return self._handle(action, values)

    def _handle(self, action, values):
        if action == "inspect":
            return inspect_database(Path(values.get("path", "")))
        if action == "open":
            if values.get("path"):
                path = Path(values["path"]).resolve()
                inspect_database(path)
                entry = self.register(path)
            else:
                entry = self.entry(values.get("id"))
            self.open_entry(entry["id"], readonly=values.get("readonly", True))
            return {"reopen": "/#" + self.current.launch_token}
        if action == "create":
            path = Path(values["path"]).resolve() if values.get("path") else self.home / "datasets" / uuid.uuid4().hex
            if path.exists():
                raise ValueError("目的地已存在，請指定新的資料夾。")
            path.mkdir(parents=True)
            manifest(path, create=True, name=self.name(values, "新資料庫"))
            app = BotApplication(self.settings, path, self.sdk_factory)
            app.close()
            entry = self.register(path)
            return {"entry": entry, "database": inspect_database(path)}
        if action == "import-preview":
            path = Path(values.get("path", "")).resolve()
            info = inspect_database(path)
            key = uuid.uuid4().hex
            self.previews[key] = {"source": str(path), "stamp": stamp(path), "id": info["id"]}
            return {"preview_id": key, "database": info, "mode": "independent"}
        if action in {"backup", "copy", "import"}:
            self.ensure_idle()
            if action == "import":
                preview = self.previews.get(values.get("preview_id"))
                if not preview:
                    raise ValueError("請先預覽匯入資料。")
                source = Path(preview["source"])
                if stamp(source) != preview["stamp"]:
                    raise ValueError("來源已變更，請重新預覽。")
            else:
                source = self.path_for(self.entry(values.get("id", self.active_entry)))
            info = inspect_database(source)
            path = Path(values["path"]).resolve() if values.get("path") else self.home / ("backups" if action == "backup" else "datasets") / uuid.uuid4().hex
            result = copy_database(source, path, name=self.name(values, info["name"] + (" · 備份" if action == "backup" else " · 副本")),
                snapshot=action == "backup", owned=self.current.directory == source)
            if action == "import":
                self.previews.pop(values["preview_id"], None)
            return {"database": result, "entry": self.register(path)}
        if action == "rename":
            self.ensure_idle()
            entry = self.entry(values.get("id"))
            path = self.path_for(entry)
            if self.current.read_only and self.active_entry == entry["id"]:
                raise ValueError("唯讀檢閱不修改來源；請建立副本再命名。")
            info = manifest(path)
            info["name"] = self.name(values, info["name"])
            with source_lock(path, owned=self.current.directory == path):
                atomic_json(path / MANIFEST, info)
            if self.active_entry == entry["id"]:
                self.current.database = info
            return {"ok": True}
        if action == "forget":
            entry = self.entry(values.get("id"))
            if entry["id"] == self.active_entry:
                raise ValueError("請先切換至其他資料庫。")
            self.catalog["entries"].remove(entry)
            self.persist()
            return {"ok": True}
        raise ValueError("資料庫操作不存在。")

    def close(self):
        try:
            if getattr(self, "current", None):
                self.current.close()
            if getattr(self, "temporary", None):
                self.temporary.cleanup()
        finally:
            self._catalog_lock.close()
