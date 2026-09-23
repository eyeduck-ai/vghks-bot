"""Account-scoped, read-only preauthorization queries and saved snapshots."""
import copy
import uuid
from datetime import date

from vghks_sdk.models.review import REVIEW_DECISIONS, ReviewCaseFilter, ReviewCaseRef

from .analysis_fetch import clean
from .analysis_store import digest
from .approval_tracker import ApprovalTracker, case_content
from .bot_store import fresh
from .scanner import Cancelled, safe_failure
from .settings import timestamp
from .storage import StorageError

KINDS = {"approval_search", "approval_options", "approval_case", "approval_refresh", "approval_sync"}
PARTS = {"detail": "get_case", "orders": "get_orders", "attachments": "get_attachments", "pacs": "get_pacs"}


class Approvals:
    def __init__(self, app):
        self.app, self.db = app, app.review.db
        self.tracker = ApprovalTracker(self)
        self.index_versions()

    def index_versions(self):
        """Index existing captures while leaving the original observations intact."""
        if self.db.get("preferences", "approval_content_index_v1", required=False):
            return
        tasks = {t["id"]: t["kind"] for t in self.db.all("task")}
        versions, sources = {}, {}
        for row in sorted(self.db.all("approval_observation"), key=lambda r: r["checked_at"]):
            ref, case = row["apply_seq"], row["case"]
            kind = row.get("source_kind") or ("detail" if tasks.get(row.get("source")) in {
                "approval_case", "approval_refresh"} else "list")
            fingerprint = digest(case_content(case, fields=True))
            version = digest([ref, kind, fingerprint])
            versions.setdefault(version, {"id": version, "apply_seq": ref, "case": case,
                "source_kind": kind, "checked_at": row["checked_at"], "source": row.get("source", "saved")})
            key = ref + ":" + kind
            sources[key] = {"id": key, "fingerprint": fingerprint, "version": version, "checked_at": row["checked_at"]}
        self.db.save_batch([*(('approval_version', value) for value in versions.values()),
                            *(('approval_source', value) for value in sources.values()),
                            ("preferences", {"id": "approval_content_index_v1", "done": True})])

    def prepare(self, task, values):
        kind = task["kind"]
        if kind == "approval_sync":
            self.tracker.require_idle()
            return {**task, "name": "同步審查案件", "automatic": values.get("automatic") is True,
                    "filters": clean(ReviewCaseFilter(doctor_card=self.app.username))}
        if kind == "approval_refresh":
            return self.tracker.prepare(task, values)
        if kind == "approval_search":
            supplied = values.get("filters", {})
            if not isinstance(supplied, dict):
                raise ValueError("審查篩選條件不正確。")
            fields = {k: supplied.get(k, "") for k in ("doctor_card", "department", "mrn", "verify_code", "apply_mode")}
            fields["doctor_card"] = supplied.get("doctor_card", self.app.username)
            try:
                for key in ("start_date", "end_date"):
                    fields[key] = date.fromisoformat(supplied[key]) if supplied.get(key) else None
                model = ReviewCaseFilter(**fields)
            except Exception as exc:
                raise ValueError("請填寫有效的日期與審查條件；至少需一項條件。") from exc
            task.update(name="審查案件查詢", filters=clean(model))
        elif kind == "approval_options":
            department = values.get("department", "")
            if department:
                try:
                    department = ReviewCaseFilter(department=department).department
                except Exception as exc:
                    raise ValueError("科別代碼不正確。") from exc
            task.update(name="審查查詢選項", department=department)
        else:
            try:
                ref = ReviewCaseRef(values.get("apply_seq"))
            except Exception as exc:
                raise ValueError("審查案件編號不正確。") from exc
            self.db.get("approval_case", ref.apply_seq)
            parts = values.get("parts", ["detail"])
            if not isinstance(parts, list) or not parts or any(p not in PARTS for p in parts):
                raise ValueError("審查明細類型不正確。")
            task.update(name="審查案件 " + ref.apply_seq, apply_seq=ref.apply_seq, parts=list(dict.fromkeys(parts)))
        return task

    def execute(self, state, task):
        kind = task["kind"]
        with self.app.sdk_factory(self.app.settings) as sdk:
            if kind == "approval_sync":
                self.sync(sdk, state, task)
            elif kind == "approval_refresh":
                self.tracker.execute(sdk, state, task)
            elif kind == "approval_search":
                key = digest(task["filters"])
                cached = self.db.get("approval_query", key, required=False)
                if cached and not task["force"] and fresh(cached["updated_at"], 60):
                    result = copy.deepcopy(cached)
                    result["cached"] = True
                else:
                    args = dict(task["filters"])
                    for field in ("start_date", "end_date"):
                        args[field] = date.fromisoformat(args[field]) if args.get(field) else None
                    response = sdk.reviews.get_cases(ReviewCaseFilter(**args))
                    checked_at = self.app.gateway.response_at()
                    cases = clean(response)
                    references = [r.get("reference", {}).get("apply_seq") for r in cases]
                    if len(set(references)) != len(references) or None in references:
                        raise ValueError("審查案件回應不完整或編號重複。")
                    result = {"id": key, "filters": task["filters"], "cases": cases,
                              "fetched_at": checked_at, "cached": False}
                    with self.tracker.lock:
                        for row in cases:
                            self.tracker.observe(row, source=task["id"], checked_at=checked_at)
                        newer = self.db.get("approval_query", key, required=False)
                        if not newer or newer["fetched_at"] <= checked_at:
                            self.db.save("approval_query", result)
                self.db.item(task["id"], "query", {**result, "status": "ready"})
            elif kind == "approval_options":
                key = task["department"] or "all"
                cached = self.db.get("approval_options", key, required=False)
                if cached and not task["force"] and fresh(cached["updated_at"], 86400):
                    result = cached
                else:
                    options = clean(sdk.reviews.get_options())
                    state.check_cancel()
                    if task["department"]:
                        options["VSDrID"] = clean(sdk.reviews.get_doctors(task["department"]))
                    result = self.db.save("approval_options", {"id": key, "options": options, "fetched_at": timestamp()})
                self.db.item(task["id"], "options", {**result, "status": "ready"})
            else:
                for part in task["parts"]:
                    state.check_cancel()
                    old = self.db.item(task["id"], part)
                    if old and old["status"] == "ready":
                        continue
                    key = task["apply_seq"] + ":" + part
                    cached = self.db.get("approval_part", key, required=False)
                    try:
                        if cached and not task["force"] and fresh(cached["updated_at"], 60):
                            result = cached
                        else:
                            response = getattr(sdk.reviews, PARTS[part])(ReviewCaseRef(task["apply_seq"]))
                            checked_at = self.app.gateway.response_at()
                            value = clean(response)
                            if value.get("reference", {}).get("apply_seq") != task["apply_seq"]:
                                raise ValueError("審查明細與案件編號不符。")
                            with self.tracker.lock:
                                if part == "detail":
                                    self.tracker.observe(value, source=task["id"], checked_at=checked_at, source_kind="detail")
                                result = {"id": key, "part": part, "payload": value, "fetched_at": checked_at}
                                newer = self.db.get("approval_part", key, required=False)
                                if not newer or newer["fetched_at"] <= checked_at:
                                    self.db.save("approval_part", result)
                        self.db.item(task["id"], part, {**result, "status": "ready"})
                    except (Cancelled, StorageError):
                        raise
                    except Exception as exc:
                        if not self.app.gateway.online:
                            raise
                        message, code = safe_failure(exc)
                        self.db.item(task["id"], part, {"part": part, "status": "error", "message": message, "code": code})
                    self.app.review.report(state, task, stage="審查明細已處理")

    def overview(self):
        return {"queries": [t for t in self.db.all("task") if t["kind"] == "approval_search"],
                "options": self.db.all("approval_options"), "decisions": REVIEW_DECISIONS}

    def results(self, values):
        task = self.db.get("task", values.get("id"))
        if task["kind"] != "approval_search":
            raise ValueError("請選擇審查查詢任務。")
        result = self.db.item(task["id"], "query") or {"cases": [], "filters": task["filters"]}
        cases = result["cases"]
        query = str(values.get("q", "")).casefold().strip()
        decision = values.get("decision", "")
        rows = [r for r in cases if (not query or query in " ".join(str(r.get(k, "")) for k in (
            "mrn", "patient_name", "doctor_card", "application_date", "review_label")).casefold()
            or query in r["reference"]["apply_seq"]) and (decision == "" or r["verify_code"] == decision)]
        offset, limit = int(values.get("offset", 0)), int(values.get("limit", 40))
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("分頁範圍不正確。")
        return {**{k: v for k, v in result.items() if k != "cases"}, "task": task,
                "cases": rows[offset:offset+limit], "total": len(rows), "all_count": len(cases)}

    def detail(self, values):
        ref = ReviewCaseRef(values.get("apply_seq"))
        case = self.db.get("approval_case", ref.apply_seq)
        return {**case, "tracking": self.db.get("approval_tracking", ref.apply_seq, required=False),
                "observations": self.history(ref.apply_seq),
                "parts": {p: self.db.get("approval_part", ref.apply_seq+":"+p, required=False) for p in PARTS}}

    def sync(self, sdk, state, task):
        run = {"id": uuid.uuid4().hex, "task_id": task["id"], "started_at": timestamp(),
               "automatic": task.get("automatic", False), "status": "running", "new": 0, "changed": 0, "unchanged": 0}
        self.db.save("approval_sync_run", run)
        try:
            self.app.review.report(state, task, stage="取得完整審查清單")
            response = sdk.reviews.get_cases(ReviewCaseFilter(doctor_card=self.app.username))
            checked = self.app.gateway.response_at()
            cases = clean(response)
            refs = [c.get("reference", {}).get("apply_seq") for c in cases]
            if None in refs or len(set(refs)) != len(refs):
                raise ValueError("審查案件回應不完整或編號重複。")
            task["case_total"] = len(cases)
            task["sync_checked_at"] = checked
            self.db.save("task", {**self.db.get("task", task["id"]), "case_total": len(cases), "sync_checked_at": checked})
            for row in cases:
                state.check_cancel()
                outcome = self.tracker.observe(row, source=task["id"], checked_at=checked)
                change = outcome.get("change", "unchanged")
                run[change] += 1
                ref = row["reference"]["apply_seq"]
                self.db.item(task["id"], ref, {"status": "ready", "apply_seq": ref,
                    "version": outcome.get("version"), "change": change, "checked_at": checked})
                self.db.save("approval_sync_run", {**run, "fetched_at": checked, "total": len(cases)})
                self.app.review.report(state, task, stage="審查案件已保存")
            run.update(status="completed", total=len(cases), fetched_at=checked)
            # Labels are shared selectors, not a second crawl of every case.
            cached = self.db.get("approval_options", "all", required=False)
            if not cached or not fresh(cached["fetched_at"], 86400):
                try:
                    options = clean(sdk.reviews.get_options())
                    self.db.save("approval_options", {"id": "all", "options": options, "fetched_at": timestamp()})
                except Exception as exc:
                    run["options_warning"] = safe_failure(exc)[0]
        except Cancelled:
            run["status"] = "paused"
            raise
        except Exception as exc:
            run.update(status="failed", error=safe_failure(exc)[0])
            raise
        finally:
            self.db.save("approval_sync_run", {**run, "finished_at": timestamp()})

    def history(self, ref):
        rows = sorted((r for r in self.db.all("approval_observation") if r["apply_seq"] == ref),
                      key=lambda r: r["checked_at"])
        result, previous, previous_fields = [], None, {}
        for row in rows:
            content = case_content(row["case"])
            fields = case_content(row["case"], fields=True)["fields"]
            fields_changed = any(fields[k] != previous_fields[k] for k in fields.keys() & previous_fields.keys())
            # Legacy list/detail snapshots differ in fields and generated IDs.
            if content != previous or fields_changed or row.get("version"):
                result.append(row)
            previous = content
            previous_fields = fields
        return list(reversed(result))

    def cases(self, values=None):
        values = values or {}
        data = self.tracker.overview({"mode": values.get("mode", "all")})
        rows = data.pop("rows")
        query = str(values.get("q", "")).casefold().strip()
        for name in ("start", "end"):
            if values.get(name):
                date.fromisoformat(values[name])
        def match(row):
            c = row["case"]
            text = " ".join(str(c.get(k, "")) for k in ("mrn", "patient_name", "doctor_card", "department", "review_label")) + " " + row["id"]
            return ((not query or query in text.casefold())
                and (not values.get("mrn") or values["mrn"] in c["mrn"])
                and (not values.get("start") or c["application_date"] >= values["start"])
                and (not values.get("end") or c["application_date"] <= values["end"])
                and (not values.get("decision") or c["verify_code"] == values["decision"]))
        filtered = sorted((r for r in rows if match(r)), key=lambda r: (r["case"]["application_date"], r["id"]), reverse=True)
        return {**data, "rows": filtered, "total": len(filtered),
                "options": self.db.get("approval_options", "all", required=False),
                "runs": self.db.all("approval_sync_run")[:20]}

    def sync_history(self, _=None):
        return {"runs": self.db.all("approval_sync_run")}
