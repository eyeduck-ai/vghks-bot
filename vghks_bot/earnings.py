"""Account-scoped archival of the currently available MIS earnings reports."""
import hmac
import json
import secrets
import threading

from vghks_sdk import AuthenticationError
from vghks_sdk.core.config import EarningsCredentials
from vghks_sdk.core.errors import error_info

from .analysis_store import digest
from .connection_state import should_pause
from .earnings_monitor import EarningsMonitor
from .earnings_parser import export_csv, month_value, normalize_document, report_content
from .pagination import slice_rows
from .portable_credentials import seal, unseal
from .scanner import Cancelled, safe_failure
from .settings import timestamp
from .storage import StorageError

KINDS = {"earnings_options", "earnings_capture"}
REPORTS = {"performance": "醫療績點明細", "payroll": "專勤工作獎金明細"}
OPEN_METHODS = {"performance": "open_performance", "payroll": "open_bonus"}


class Earnings:
    def __init__(self, app):
        self.app, self.db = app, app.review.db
        self.lock = threading.RLock()
        self.memory_credentials = None
        self.monitor = EarningsMonitor(self)
        with self.db.library.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS bot_earnings_credentials (id INTEGER PRIMARY KEY CHECK(id=1), secret BLOB)")
            db.execute("CREATE TABLE IF NOT EXISTS bot_earnings_identity (id INTEGER PRIMARY KEY CHECK(id=1), salt BLOB NOT NULL, scope TEXT NOT NULL)")
            if not db.execute("SELECT 1 FROM bot_earnings_identity").fetchone():
                salt = secrets.token_bytes(32)
                row = db.execute("SELECT secret FROM bot_earnings_credentials WHERE id=1").fetchone()
                try:
                    national_id = json.loads(unseal(db, row[0]))["national_id"] if row and row[0] else ""
                    scope = self.identity_key(salt, national_id) if isinstance(national_id, str) and national_id else ""
                except (ValueError, TypeError, KeyError):
                    scope = ""
                db.execute("INSERT INTO bot_earnings_identity VALUES(1,?,?)",
                           (salt, scope))
        self.index_versions()

    def index_versions(self):
        """Add a content index without changing any previously captured payload."""
        if self.db.get("preferences", "earnings_content_index_v1", required=False):
            return
        entries = []
        indexed = {}
        for version in sorted(self.db.all("earnings_version"), key=lambda v: v["fetched_at"]):
            key = digest([version["report_id"], report_content(version["payload"])])
            indexed.setdefault(key, version)
            entries.append(("earnings_fetch", {"id": "legacy:" + version["id"], "report_id": version["report_id"],
                "version": indexed[key]["id"], "fetched_at": version["fetched_at"], "source_version": version["id"]}))
        for key, version in indexed.items():
            entries.append(("earnings_content", {"id": key, "report_id": version["report_id"], "version": version["id"]}))
        for report in self.db.all("earnings_report"):
            latest = self.db.get("earnings_version", report["latest"])
            content_key = digest([report["id"], report_content(latest["payload"])])
            entries.append(("earnings_report", {**report, "latest": indexed[content_key]["id"],
                "version_count": sum(v["report_id"] == report["id"] for v in indexed.values())}))
        entries.append(("preferences", {"id": "earnings_content_index_v1", "done": True}))
        self.db.save_batch(entries)

    @staticmethod
    def identity_key(salt, national_id):
        return hmac.digest(salt, national_id.strip().upper().encode(), "sha256").hex()

    def scope(self):
        with self.db.library.connect() as db:
            return db.execute("SELECT scope FROM bot_earnings_identity WHERE id=1").fetchone()[0]

    def credentials(self):
        with self.lock:
            if self.memory_credentials is not None:
                return self.memory_credentials
            with self.db.library.connect() as db:
                row = db.execute("SELECT secret FROM bot_earnings_credentials WHERE id=1").fetchone()
                if row and row[0]:
                    return EarningsCredentials(**json.loads(unseal(db, row[0]))).validate()
        raise ValueError("請先設定此帳號的薪資系統登入資訊。")

    def public_credentials(self):
        with self.db.library.connect() as db:
            row = db.execute("SELECT secret IS NOT NULL FROM bot_earnings_credentials WHERE id=1").fetchone()
        return {"configured": bool(self.memory_credentials or row and row[0]), "remembered": bool(row and row[0])}

    def save_credentials(self, values):
        national_id, password = values.get("national_id", ""), values.get("password", "")
        if not isinstance(national_id, str) or not isinstance(password, str) or not 1 <= len(national_id.strip()) <= 32 or not 1 <= len(password) <= 256:
            raise ValueError("請輸入身分證字號及薪資系統密碼。")
        if type(values.get("remember", True)) is not bool:
            raise ValueError("保存登入資訊設定不正確。")
        value = {"national_id": national_id.strip().upper(), "password": password}
        credentials = EarningsCredentials(**value).validate()
        with self.app.root.lock, self.app.lock, self.lock:
            self.require_idle()
            with self.db.library.connect() as db:
                salt = db.execute("SELECT salt FROM bot_earnings_identity WHERE id=1").fetchone()[0]
                secret = seal(db, json.dumps(value).encode()) if values.get("remember", True) else None
                db.execute("INSERT OR REPLACE INTO bot_earnings_credentials VALUES(1,?)", (secret,))
                db.execute("UPDATE bot_earnings_identity SET scope=? WHERE id=1", (self.identity_key(salt, value["national_id"]),))
            self.memory_credentials = credentials
            return self.public_credentials()

    def forget(self, _=None):
        with self.app.root.lock, self.app.lock, self.lock:
            self.require_idle()
            with self.db.library.connect() as db:
                db.execute("DELETE FROM bot_earnings_credentials")
            self.memory_credentials = None
            return self.public_credentials()

    def prepare(self, task, values):
        self.credentials()  # Never put credentials or report contexts in a task.
        self.require_idle()
        scope = self.scope()
        if not scope:
            raise ValueError("請重新設定薪資系統登入資訊。")
        task = {**task, "earnings_scope": scope}
        if task["kind"] == "earnings_options":
            return {**task, "name": "查詢薪資／業績可用月份", "report_kinds": list(REPORTS)}
        reports = values.get("reports", [])
        all_available = values.get("all_available", False)
        if type(all_available) is not bool or not isinstance(reports, list) or len(reports) > 2400:
            raise ValueError("薪資報表選取不正確。")
        if not reports and not all_available:
            raise ValueError("請選取月份，或保存所有可取得月份。")
        selected = []
        for item in reports:
            if not isinstance(item, dict) or item.get("kind") not in REPORTS or not isinstance(item.get("period"), str):
                raise ValueError("報表類型或期別不正確。")
            kind, period = item["kind"], item["period"]
            catalog = self.db.get("earnings_catalog", digest([scope, kind]))
            if period not in {r["value"] for r in catalog["periods"]}:
                raise ValueError("請先取得院方可查詢月份，再選擇期別。")
            row = {"kind": kind, "period": period}
            if row not in selected:
                selected.append(row)
        return {**task, "name": "保存薪資／業績報表", "reports": [] if all_available else selected, "all_available": all_available,
                "automatic": values.get("automatic") is True, "force": values.get("force", all_available) is True,
                "report_kinds": list(REPORTS) if all_available else list(dict.fromkeys(r["kind"] for r in selected))}

    def require_idle(self):
        if any(t["kind"] in KINDS and t["status"] in {"queued", "running", "cancelling"} for t in self.db.task_summaries(kinds=('earnings_options','earnings_capture'))):
            raise ValueError("薪資查詢已在處理中，請等待完成或先暫停。")

    def check_task_identity(self, task):
        scope = task.get("earnings_scope")
        if not scope or self.scope() != scope:
            raise ValueError("薪資查詢的身分與目前設定不同或來源未標記，請以目前身分建立新查詢。")
        return scope

    def scoped_tasks(self, scope):
        if not scope:
            return []
        with self.db.library.connect() as db:
            keys = {row[0] for row in db.execute("SELECT id FROM bot_documents WHERE kind='task' AND "
                "json_extract(payload,'$.kind') IN ('earnings_options','earnings_capture') AND "
                "json_extract(payload,'$.earnings_scope')=?", (scope,))}
        return [task for task in self.db.task_summaries(kinds=KINDS) if task["id"] in keys]

    def catalog(self, sdk, scope, kind, credentials):
        context = getattr(sdk.earnings, OPEN_METHODS[kind])(credentials)
        if context.kind != kind:
            raise ValueError("薪資報表類型與請求不符。")
        choices = context.form.choices.get("BEGYM", ())
        periods, seen = [], set()
        for value, label in choices:
            if not value:
                continue
            if value in seen or len(str(value)) > 100 or len(str(label)) > 300:
                raise ValueError("院方月份選單格式不完整。")
            seen.add(value)
            periods.append({"value": value, "label": label, "month": month_value(value)})
        self.db.save("earnings_catalog", {"id": digest([scope, kind]), "scope": scope, "kind": kind,
                                          "periods": periods, "fetched_at": timestamp()})
        return context, periods

    def store_report(self, kind, period, label, document, *, scope=None):
        scope = self.scope() if scope is None else scope
        payload = normalize_document(document)
        key = digest([scope, kind, period] if scope else [kind, period])
        content_key = digest([key, report_content(payload)])
        content = self.db.get("earnings_content", content_key, required=False)
        version = content["version"] if content else content_key
        now = timestamp()
        previous = self.db.get("earnings_report", key, required=False)
        saved = self.db.get("earnings_version", version, required=False)
        row = {"id": key, **({"scope": scope} if scope else {}), "kind": kind, "period": period, "label": label,
               "month": month_value(period), "latest": version, "fetched_at": now,
               "first_saved": previous["first_saved"] if previous else now,
               "version_count": (previous["version_count"] if previous else 0) + int(not saved),
               "table_count": len(payload["tables"]), "notes": payload["notes"]}
        values = [("earnings_report", row)]
        if not saved:
            values.append(("earnings_version", {**row, "id": version, "report_id": key, "payload": payload}))
        values.extend([("earnings_content", {"id": content_key, "report_id": key, "version": version}),
                       ("earnings_fetch", {"report_id": key, "version": version, "fetched_at": now, "changed": bool(previous and previous["latest"] != version)})])
        self.db.save_batch(values)
        return row

    def execute(self, state, task):
        with self.lock:
            scope = self.check_task_identity(task)
            credentials = self.credentials()
        with self.app.sdk_factory(self.app.settings) as sdk:
            for kind in task["report_kinds"]:
                state.check_cancel()
                pending = [r for r in task.get("reports", []) if r["kind"] == kind]
                self.app.review.report(state, task, stage="開啟" + REPORTS[kind])
                try:
                    context, periods = self.catalog(sdk, scope, kind, credentials)
                except (Cancelled, StorageError):
                    raise
                except AuthenticationError:
                    raise  # Do not repeat a rejected secondary password for the next report.
                except Exception as exc:
                    if should_pause(exc):
                        raise
                    message, code = safe_failure(exc)
                    self.db.item(task["id"], "catalog:"+kind, {"status": "error", "kind": kind, "message": message, "code": code})
                    self.app.review.report(state, task, stage="月份未取得，可重試")
                    continue
                # A failed catalog step is replaced on successful retry.
                with self.db.library.connect() as db:
                    db.execute("DELETE FROM bot_task_items WHERE task_id=? AND key=?", (task["id"], "catalog:"+kind))
                if task["kind"] == "earnings_options":
                    self.db.item(task["id"], kind, {"status": "ready", "kind": kind, "periods": periods})
                    self.app.review.report(state, task, stage="已取得月份")
                    continue
                if task["all_available"]:
                    # Freeze discovered members once per report type, including zero months.
                    if kind not in task.get("discovered", []):
                        pending = [{"kind": kind, "period": p["value"]} for p in periods]
                        task["reports"].extend(pending)
                        task.setdefault("discovered", []).append(kind)
                        self.db.save("task", task)
                available = {p["value"]: p for p in periods}
                for choice in pending:
                    state.check_cancel()
                    period, key = choice["period"], digest([scope, kind, choice["period"]])
                    old = self.db.item(task["id"], key)
                    if old and old["status"] in {"ready", "deleted"}:
                        continue
                    tombstone = self.db.get("earnings_deleted", key, required=False)
                    cached = self.db.get("earnings_report", key, required=False)
                    if tombstone and (tombstone["deleted_at"] >= task["created_at"] or task.get("automatic") and not cached):
                        self.db.item(task["id"], key, {"status": "deleted", "kind": kind, "period": period})
                        self.app.review.report(state, task, stage="已略過手動刪除報表")
                        continue
                    self.app.review.report(state, task, stage=REPORTS[kind]+" · "+period)
                    try:
                        if cached and not task["force"]:
                            row = cached
                        else:
                            if period not in available:
                                raise ValueError("此期別已不在院方提供的月份中，既有資料保留。")
                            try:
                                report = sdk.earnings.get_report(context, period)
                            except Exception as exc:
                                if error_info(exc).code != "EARNINGS_CONTEXT_EXPIRED":
                                    raise
                                context, _ = self.catalog(sdk, scope, kind, credentials)
                                report = sdk.earnings.get_report(context, period)
                            row = self.store_report(kind, period, available[period]["label"], report, scope=scope)
                        self.db.item(task["id"], key, {"status": "ready", "report_id": key, "version": row["latest"],
                                                     "kind": kind, "period": period, "cached": bool(cached and not task["force"])})
                    except (Cancelled, StorageError, AuthenticationError):
                        raise
                    except Exception as exc:
                        if should_pause(exc):
                            raise
                        message, code = safe_failure(exc)
                        self.db.item(task["id"], key, {"status": "error", "kind": kind, "period": period,
                                                     "message": message, "code": code})
                    self.app.review.report(state, task, stage="報表已保存")

    def view_scope(self, values):
        scope = self.scope()
        view = values.get("view")
        with self.db.library.connect() as db:
            has_archive = db.execute("SELECT 1 FROM bot_documents WHERE kind='earnings_report' "
                                     "AND json_extract(payload,'$.scope') IS NULL LIMIT 1").fetchone() is not None
        if view is None:
            view = "archive" if not scope and has_archive else "active"
        if view not in ("active", "archive"):
            raise ValueError("薪資報表檢視範圍不正確。")
        return view, scope if view == "active" else None, has_archive

    @staticmethod
    def check_report_scope(report, scope):
        if report.get("scope") != scope:
            raise ValueError("報表不在目前的薪資身分或舊報表檢視範圍。")

    def overview(self, values=None):
        values = values or {}
        with self.db.library.read_snapshot():
            view, scope, has_archive = self.view_scope(values)
            reports = self.db.documents("earnings_report", filters={"scope": scope})
            catalogs = self.db.documents("earnings_catalog", filters={"scope": scope}) if scope else []
            tasks = self.scoped_tasks(scope)
        rows = [r for r in reports if (not values.get("kind") or r["kind"] == values["kind"])
                and (not values.get("month") or r["month"] == values["month"])]
        rows.sort(key=lambda r: (r["month"], r["period"], r["kind"]), reverse=True)
        known = {(r["kind"], r["period"]) for r in reports}
        catalogs = [{**public_report(c), "id": c["kind"], "periods": [{**p, "saved": (c["kind"], p["value"]) in known}
                     for p in c["periods"]]} for c in catalogs]
        return {"view": view, "has_archive": has_archive, "reports": [public_report(r) for r in slice_rows(rows, values)],
                "total": len(rows), "catalogs": catalogs, "credentials": self.public_credentials(),
                "monitor": self.monitor.overview() if view == "active" else {}, "kinds": REPORTS, "tasks": tasks,
                "months": sorted({r["month"] for r in reports if r["month"]}, reverse=True)}

    def detail(self, values):
        with self.db.library.read_snapshot():
            _, scope, _ = self.view_scope(values)
            report = self.db.get("earnings_report", values.get("id"))
            self.check_report_scope(report, scope)
            version = self.db.get("earnings_version", values.get("version") or report["latest"])
            if version["report_id"] != report["id"]:
                raise ValueError("報表與版本不符。")
            ids = {c["version"] for c in self.db.headers("earnings_content", ("version",), filters={"report_id": report["id"]})}
            versions = [v for v in self.db.headers("earnings_version", ("id", "fetched_at"), filters={"report_id": report["id"]}) if v["id"] in ids]
            return {"report": public_report(report), "version": public_report(version), "versions": versions,
                    "fetches": self.db.documents("earnings_fetch", filters={"report_id": report["id"]})}

    def export(self, values):
        ids = values.get("ids")
        if not isinstance(ids, list) or not ids or len(ids) > 2400:
            raise ValueError("請選取要匯出的報表。")
        if any(not isinstance(key, str) for key in ids):
            raise ValueError("請選取要匯出的報表。")
        with self.db.library.read_snapshot():
            _, scope, _ = self.view_scope(values)
            reports = {r["id"]: r for r in self.db.documents("earnings_report", keys=ids)}
            if set(ids) != set(reports):
                raise ValueError("資料不存在於此帳號工作區。")
            for report in reports.values():
                self.check_report_scope(report, scope)
            saved = {v["id"]: v for v in self.db.documents("earnings_version", keys=[r["latest"] for r in reports.values()])}
            versions = []
            for key in dict.fromkeys(ids):
                version = saved.get(reports[key]["latest"])
                if not version or version["report_id"] != key:
                    raise ValueError("報表與版本不符。")
                versions.append(public_report(version))
        kind = values.get("format", "json")
        if kind not in {"csv", "json"}:
            raise ValueError("匯出格式不正確。")
        content = export_csv(versions) if kind == "csv" else json.dumps({"exported_at": timestamp(), "reports": versions}, ensure_ascii=False, indent=2)
        return {"filename": "VGHKS-earnings."+kind, "mime": "text/csv;charset=utf-8" if kind == "csv" else "application/json", "content": content}

    def delete(self, values):
        with self.app.lock, self.app.review.lock:
            if not self.app.idle.is_set() or self.app.review.foreground:
                raise ValueError("請先暫停此帳號的任務再刪除。")
            ids = values.get("ids")
            if not isinstance(ids, list) or not 1 <= len(ids) <= 2400 or any(not isinstance(key, str) for key in ids):
                raise ValueError("請選取報表。")
            _, scope, _ = self.view_scope(values)
            for key in ids:
                self.check_report_scope(self.db.get("earnings_report", key), scope)
            with self.db.library.connect() as db:
                for key in ids:
                    tombstone = {"id": key, "deleted_at": timestamp()}
                    db.execute("INSERT OR REPLACE INTO bot_documents VALUES(?,?,?,?)", (
                        "earnings_deleted", key, json.dumps(tombstone), tombstone["deleted_at"]))
                    db.execute("DELETE FROM bot_documents WHERE kind='earnings_report' AND id=?", (key,))
                db.execute("DELETE FROM bot_documents WHERE kind IN ('earnings_version','earnings_content','earnings_fetch') "
                           "AND json_extract(payload,'$.report_id') IN (SELECT value FROM json_each(?))", (json.dumps(ids),))
        return {"ok": True}


def public_report(row):
    return {key: value for key, value in row.items() if key != "scope"}
