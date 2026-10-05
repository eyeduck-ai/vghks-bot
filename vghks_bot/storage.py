"""Local, append-only clinical journals and atomic JSON metadata."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

ACTIVE = {"queued", "running", "cancelling"}
_IO_LOCK = threading.RLock()


class StorageError(Exception):
    pass


def atomic_json(path: Path, data):
    with _IO_LOCK:
        _write_json(path, data)


def _write_json(path: Path, data):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(.02 * (attempt + 1))
    except OSError as exc:
        raise StorageError("無法保存資料，請確認資料夾權限與磁碟空間。") from exc
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    # Windows cannot atomically replace a file while another thread has it
    # open without delete sharing. Serialize our short metadata reads/writes.
    with _IO_LOCK:
        return json.loads(path.read_text(encoding="utf-8"))


def read_lines(path):
    if not path.exists():
        return [], False
    result, damaged = [], False
    with path.open("rb") as handle:
        for line in handle:
            try:
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError()
                result.append(item)
            except (ValueError, UnicodeError):
                damaged = True
    return result, damaged


class Journal:
    def __init__(self, directory: Path, library=None, metadata=None):
        self.directory = directory
        self.library = library
        self.metadata = metadata or {}

    def checkpoint(self, metadata):
        atomic_json(self.directory / "run.json", metadata)
        self.metadata = metadata

    def append(self, name: str, value: dict):
        if name not in {"records", "issues", "registrations", "visits"}:
            raise ValueError("無效的紀錄種類。")
        try:
            with (self.directory / f"{name}.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(value, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise StorageError("資料保存失敗，查詢已停止；先前紀錄仍保留。") from exc
        if name == "records" and self.library and not self.metadata.get("source_id"):
            self.library.save_record(value, self.metadata.get("account", ""), self.metadata["id"])


class Store:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.warnings = []
        self._lock_file = None
        try:
            (self.directory / "runs").mkdir(parents=True, exist_ok=True)
            self._lock_file = (self.directory / ".lock").open("a+b")
            self._lock_file.write(b"0")
            self._lock_file.flush()
            self._lock_file.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if self._lock_file:
                self._lock_file.close()
            raise StorageError("資料夾無法使用，或已有另一個程式正在使用此資料夾。") from exc
        try:
            self.recover()
            from .library import Library

            self.library = Library(self.directory / "clinical.sqlite3")
            pending = self.library.pending_deletions()
            if pending:
                self.delete_records(pending)
            for summary in reversed(self.summaries()):
                if not self.library.imported(summary):
                    self.library.import_run(summary, self.load(summary["id"])["records"])
        except Exception:
            self.close()
            raise

    def close(self):
        if self._lock_file:
            self._lock_file.close()
            self._lock_file = None

    def path(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("查詢紀錄代碼不正確。")
        return self.directory / "runs" / run_id

    def create(self, metadata):
        path = self.path(metadata["id"])
        try:
            path.mkdir()
        except OSError as exc:
            raise StorageError("無法建立查詢紀錄。") from exc
        journal = Journal(path, self.library, metadata)
        journal.checkpoint(metadata)
        return journal

    def load_config(self):
        path = self.directory / "settings.json"
        if not path.exists():
            return None
        try:
            value = read_json(path)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (OSError, ValueError) as exc:
            raise StorageError("settings.json 無法讀取；原檔已保留。") from exc

    def save_config(self, settings, accounts):
        atomic_json(self.directory / "settings.json", {
            "settings": settings.public(),
            "accounts": [account.persisted() for account in accounts],
        })

    def summaries(self):
        result = []
        cache = getattr(self, "_summary_cache", {})
        current = {}
        for path in (self.directory / "runs").glob("*/run.json"):
            try:
                stat = path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                saved = cache.get(path)
                value = saved[1] if saved and saved[0] == stamp else read_json(path)
                if self.path(value["id"]) != path.parent or not isinstance(value.get("counts"), dict):
                    raise ValueError()
                result.append(value)
                current[path] = (stamp, value)
            except (ValueError, KeyError, OSError):
                warning = "有紀錄檔無法讀取，原檔已保留。"
                if warning not in self.warnings:
                    self.warnings.append(warning)
        self._summary_cache = current
        return sorted(result, key=lambda row: (row.get("created_at", ""), row["id"]), reverse=True)

    def load(self, run_id):
        path = self.path(run_id)
        try:
            value = self.metadata(run_id)
            value["records"], records_damaged = read_lines(path / "records.jsonl")
            value["issues"], issues_damaged = read_lines(path / "issues.jsonl")
        except (OSError, ValueError) as exc:
            raise ValueError("無法讀取此查詢紀錄。") from exc
        if records_damaged or issues_damaged:
            value["issues"].append({"stage": "紀錄", "code": "JOURNAL_DAMAGED", "message": "有不完整的紀錄行；已載入其餘資料。", "mrn": "", "date": ""})
            value["status"] = "partial"
        matched = [r for r in value["records"] if r.get("matches")]
        value["counts"].update(
            soap_read=len(value["records"]), matched_visits=len(matched),
            matched_patients=len({r["mrn"] for r in matched}),
            markers=sum(len(r.get("matches", [])) for r in matched), errors=len(value["issues"]),
        )
        return value

    def metadata(self, run_id):
        try:
            value = read_json(self.path(run_id) / "run.json")
            if value["id"] != run_id:
                raise ValueError()
            return value
        except (OSError, ValueError, KeyError) as exc:
            raise ValueError("無法讀取此查詢紀錄。") from exc

    def recover(self):
        for summary in self.summaries():
            if summary["status"] in ACTIVE:
                recovered = self.load(summary["id"])
                recovered.update(status="interrupted", message="上次查詢中斷，已保留取得的資料。", revision=recovered["revision"] + 1)
                Journal(self.path(summary["id"])).checkpoint({k: v for k, v in recovered.items() if k not in {"records", "issues"}})

    def delete_records(self, ids):
        """Resume-safe deletion of SOAP versions and their JSON journal copies."""
        self.library.prepare_delete(ids)
        selected = set(ids)
        with _IO_LOCK:
            for summary in self.summaries():
                path = self.path(summary["id"]) / "records.jsonl"
                if not path.exists():
                    continue
                original = path.read_bytes().splitlines(keepends=True)
                kept = []
                for line in original:
                    try:
                        value = json.loads(line)
                    except (ValueError, UnicodeError):
                        kept.append(line)
                        continue
                    if not isinstance(value, dict) or value.get("id") not in selected:
                        kept.append(line)
                if len(kept) == len(original):
                    continue
                temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                try:
                    with temporary.open("xb") as handle:
                        handle.write(b"".join(kept))
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, path)
                except OSError as exc:
                    raise StorageError("刪除尚未完成；重新啟動後會繼續處理。") from exc
                finally:
                    temporary.unlink(missing_ok=True)
                updated = self.load(summary["id"])
                updated["revision"] += 1
                Journal(path.parent).checkpoint({k: v for k, v in updated.items() if k not in {"records", "issues"}})
        self.library.finish_delete(ids)
        return {"ok": True, "deleted": len(selected)}

    def purge_index_copies(self, mrns, lists):
        """Remove legacy raw index copies while keeping task/error metadata."""
        selected, days = set(mrns), {(row["account"], row["day"]) for row in lists}
        if not selected and not days:
            return
        with _IO_LOCK:
            for summary in self.summaries():
                for name in ("visits", "registrations"):
                    path = self.path(summary["id"]) / (name + ".jsonl")
                    if not path.is_file():
                        continue
                    original, kept = path.read_bytes().splitlines(keepends=True), []
                    for line in original:
                        try:
                            value = json.loads(line)
                        except (ValueError, UnicodeError):
                            kept.append(line)
                            continue
                        remove = isinstance(value, dict) and (
                            name == "visits" and (value.get("mrn") in selected or value.get("lookup_mrn") in selected)
                            or name == "registrations" and
                            (summary.get("account"), value.get("query_date") or value.get("visit_date")) in days)
                        if not remove:
                            kept.append(line)
                    if len(kept) == len(original):
                        continue
                    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                    try:
                        with temporary.open("xb") as handle:
                            handle.write(b"".join(kept))
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(temporary, path)
                    except OSError as exc:
                        raise StorageError("原始索引清除未完成，請重試；任務摘要與錯誤紀錄仍保留。") from exc
                    finally:
                        temporary.unlink(missing_ok=True)
