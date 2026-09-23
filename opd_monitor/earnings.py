"""Account-scoped archival of the currently available MIS earnings reports."""
import json
import threading

from vghks_sdk import AuthenticationError
from vghks_sdk.core.config import EarningsCredentials
from vghks_sdk.core.errors import error_info

from .analysis_store import digest
from .earnings_monitor import EarningsMonitor
from .earnings_parser import export_csv, month_value, normalize_document, report_content
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

    def credentials(self, values=None):
        with self.lock:
            if values is not None:
                national_id, password = values.get("national_id", ""), values.get("password", "")
                if not isinstance(national_id, str) or not isinstance(password, str) or not 1 <= len(national_id.strip()) <= 32 or not 1 <= len(password) <= 256:
                    raise ValueError("請輸入身分證字號及薪資系統密碼。")
                if type(values.get("remember", True)) is not bool:
                    raise ValueError("保存登入資訊設定不正確。")
                value = {"national_id": national_id.strip().upper(), "password": password}
                self.memory_credentials = EarningsCredentials(**value).validate()
                with self.db.library.connect() as db:
                    secret = seal(db, json.dumps(value).encode()) if values.get("remember", True) else None
                    db.execute("INSERT OR REPLACE INTO bot_earnings_credentials VALUES(1,?)", (secret,))
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
        self.credentials(values)
        return self.public_credentials()

    def forget(self, _=None):
        if any(t["kind"] in KINDS and t["status"] in {"queued", "running", "cancelling"} for t in self.db.all("task")):
            raise ValueError("請先暫停薪資查詢任務。")
        with self.lock, self.db.library.connect() as db:
            db.execute("DELETE FROM bot_earnings_credentials")
            self.memory_credentials = None
        return self.public_credentials()

    def prepare(self, task, values):
        self.credentials()  # Never put credentials or report contexts in a task.
        self.require_idle()
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
            catalog = self.db.get("earnings_catalog", kind)
            if period not in {r["value"] for r in catalog["periods"]}:
                raise ValueError("請先取得院方可查詢月份，再選擇期別。")
            row = {"kind": kind, "period": period}
            if row not in selected:
                selected.append(row)
        return {**task, "name": "保存薪資／業績報表", "reports": [] if all_available else selected, "all_available": all_available,
                "automatic": values.get("automatic") is True, "force": values.get("force", all_available) is True,
                "report_kinds": list(REPORTS) if all_available else list(dict.fromkeys(r["kind"] for r in selected))}

    def require_idle(self):
        if any(t["kind"] in KINDS and t["status"] in {"queued", "running", "cancelling"} for t in self.db.all("task")):
            raise ValueError("薪資查詢已在處理中，請等待完成或先暫停。")

    def catalog(self, sdk, kind, credentials):
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
        self.db.save("earnings_catalog", {"id": kind, "periods": periods, "fetched_at": timestamp()})
        return context, periods

    def store_report(self, kind, period, label, document):
        payload = normalize_document(document)
        key = digest([kind, period])
        content_key = digest([key, report_content(payload)])
        content = self.db.get("earnings_content", content_key, required=False)
        version = content["version"] if content else content_key
        now = timestamp()
        previous = self.db.get("earnings_report", key, required=False)
        saved = self.db.get("earnings_version", version, required=False)
        row = {"id": key, "kind": kind, "period": period, "label": label,
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
        credentials = self.credentials()
        with self.app.sdk_factory(self.app.settings) as sdk:
            for kind in task["report_kinds"]:
                state.check_cancel()
                pending = [r for r in task.get("reports", []) if r["kind"] == kind]
                self.app.review.report(state, task, stage="開啟" + REPORTS[kind])
                try:
                    context, periods = self.catalog(sdk, kind, credentials)
                except (Cancelled, StorageError):
                    raise
                except AuthenticationError:
                    raise  # Do not repeat a rejected secondary password for the next report.
                except Exception as exc:
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
                    period, key = choice["period"], digest([kind, choice["period"]])
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
                                context, _ = self.catalog(sdk, kind, credentials)
                                report = sdk.earnings.get_report(context, period)
                            row = self.store_report(kind, period, available[period]["label"], report)
                        self.db.item(task["id"], key, {"status": "ready", "report_id": key, "version": row["latest"],
                                                     "kind": kind, "period": period, "cached": bool(cached and not task["force"])})
                    except (Cancelled, StorageError, AuthenticationError):
                        raise
                    except Exception as exc:
                        message, code = safe_failure(exc)
                        self.db.item(task["id"], key, {"status": "error", "kind": kind, "period": period,
                                                     "message": message, "code": code})
                    self.app.review.report(state, task, stage="報表已保存")

    def overview(self, values=None):
        values = values or {}
        reports = self.db.all("earnings_report")
        rows = [r for r in reports if (not values.get("kind") or r["kind"] == values["kind"])
                and (not values.get("month") or r["month"] == values["month"])]
        rows.sort(key=lambda r: (r["month"], r["period"], r["kind"]), reverse=True)
        known = {(r["kind"], r["period"]) for r in reports}
        catalogs = [{**c, "periods": [{**p, "saved": (c["id"], p["value"]) in known} for p in c["periods"]]}
                    for c in self.db.all("earnings_catalog")]
        return {"reports": rows, "catalogs": catalogs, "credentials": self.public_credentials(), "monitor": self.monitor.overview(),
                "kinds": REPORTS, "tasks": [t for t in self.db.all("task") if t["kind"] in KINDS],
                "months": sorted({r["month"] for r in reports if r["month"]}, reverse=True)}

    def detail(self, values):
        report = self.db.get("earnings_report", values.get("id"))
        version = self.db.get("earnings_version", values.get("version") or report["latest"])
        if version["report_id"] != report["id"]:
            raise ValueError("報表與版本不符。")
        ids = {c["version"] for c in self.db.all("earnings_content") if c["report_id"] == report["id"]}
        versions = [{k: v[k] for k in ("id", "fetched_at")} for v in self.db.all("earnings_version") if v["id"] in ids]
        return {"report": report, "version": version, "versions": versions,
                "fetches": [f for f in self.db.all("earnings_fetch") if f["report_id"] == report["id"]]}

    def export(self, values):
        ids = values.get("ids")
        if not isinstance(ids, list) or not ids or len(ids) > 2400:
            raise ValueError("請選取要匯出的報表。")
        versions = [self.detail({"id": key})["version"] for key in dict.fromkeys(ids)]
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
            if not isinstance(ids, list) or not 1 <= len(ids) <= 2400:
                raise ValueError("請選取報表。")
            for key in ids:
                self.db.get("earnings_report", key)
            with self.db.library.connect() as db:
                for key in ids:
                    tombstone = {"id": key, "deleted_at": timestamp()}
                    db.execute("INSERT OR REPLACE INTO bot_documents VALUES(?,?,?,?)", (
                        "earnings_deleted", key, json.dumps(tombstone), tombstone["deleted_at"]))
                    db.execute("DELETE FROM bot_documents WHERE kind='earnings_report' AND id=?", (key,))
                for row in db.execute("SELECT kind,id,payload FROM bot_documents WHERE kind IN ('earnings_version','earnings_content','earnings_fetch')").fetchall():
                    if json.loads(row["payload"])["report_id"] in ids:
                        db.execute("DELETE FROM bot_documents WHERE kind=? AND id=?", (row["kind"], row["id"]))
        return {"ok": True}
