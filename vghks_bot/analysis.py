"""Application boundary for cohorts, serial analysis jobs and reviewable Sheets writes."""
from __future__ import annotations

import copy
import json
import threading
import uuid
from collections import Counter
from dataclasses import replace
from datetime import date

from .analysis_fetch import PatientCollector, RunSessions, order_identity, validate_patient
from .analysis_numeric import MODULES, term_match
from .analysis_store import AnalysisStore, digest, identifier
from .google_sheets import GoogleSettings, SheetError, SheetUncertain
from .scanner import Cancelled, ScanState, safe_failure
from .settings import parse_mrns, timestamp, today
from .sheet_plan import Planner, fingerprint, verify
from .storage import ACTIVE, StorageError
from .surgery_candidates import FIELD_NAMES, candidates, enrich


class Analysis:
    def __init__(self, app):
        self.app = app
        self.store = AnalysisStore(app.store.library)
        self.google = GoogleSettings(app.store.directory)
        self.sheet_lock = threading.Lock()
        self.sheet_busy = False
        self.client_factory = self.google.client
        from .cataract_queue import CataractQueue

        self.cataract_queue = CataractQueue(self)

    def handle(self, path, values):
        routes = {
            "cohorts": self.overview, "cohorts/save": self.save_cohort,
            "cohorts/delete": self.delete_cohort, "start": self.start, "results": self.results,
            "raw": self.raw_data, "cataract/status": self.cataract_status,
            "cataract/queue": self.cataract_queue.handle,
            "delete": self.delete_data, "google": lambda _: self.google.public(),
            "google/save": self.google.save, "google/remove": lambda _: self.google.remove_key(),
            "google/test": self.test_google,
            "surgery/candidates": self.surgery_candidates, "surgery/preview": self.preview,
            "surgery/apply": self.apply, "surgery/check": self.reconcile,
            "surgery/history": self.sheet_history,
        }
        if path not in routes:
            raise ValueError("分析功能不存在。")
        return routes[path](values)

    def overview(self, _):
        runs = self.store.documents("analysis_runs")
        for run in runs:
            state = self.app.states.get(run["id"])
            if state is not None and run["status"] in ACTIVE:
                live = state.snapshot(detail=False)
                run.update({k: live[k] for k in ("status", "message", "counts")})
        return {"cohorts": self.store.documents("analysis_cohorts"), "modules": MODULES,
                "runs": runs, "google": self.google.public()}

    def label_history(self, rows):
        analyses = [row for row in rows if row.get("kind") == "analysis"]
        if not analyses:
            return
        # Legacy run journals did not store the selected modules. Read only
        # the module column once, rather than loading each patient's results.
        missing = {row["id"] for row in analyses if not row.get("modules")}
        modules = {}
        if missing:
            with self.store.library.connect() as db:
                modules = {row[0]: json.loads(row[1] or "[]") for row in db.execute(
                    "SELECT id,json_extract(payload,'$.options.modules') FROM analysis_runs") if row[0] in missing}
        for row in analyses:
            selected = row.get("modules") or modules.get(row["id"], [])
            if not isinstance(selected, list):
                selected = []
            row["modules"] = [key for key in selected if key in MODULES]
            row["analysis_name"] = "／".join(MODULES[key]["name"] for key in row["modules"]) or "歷年檢查分析（模組未記錄）"

    def resolve_members(self, values):
        grouped = {}

        def add(mrn, name, usernames, record_ids=(), selected="", registration=None):
            item = grouped.setdefault(mrn, {"mrn": mrn, "name": name, "accounts": [],
                                           "source_records": [], "source_registrations": [], "account_id": selected})
            item["accounts"] = sorted(set(item["accounts"]) | set(usernames))
            item["source_records"] = sorted(set(item["source_records"]) | set(record_ids))
            if registration and registration not in item["source_registrations"]:
                item["source_registrations"].append(registration)
            if not item["account_id"]:
                item["account_id"] = next((a.id for a in self.app.accounts.values()
                                          if a.username in item["accounts"]), "")

        if values.get("source") == "library":
            if values.get("all"):
                records = self.app.store.library.search(values.get("filters", {}), self.app.settings,
                                                        all_results=True)["records"]
            else:
                ids = values.get("record_ids", [])
                if not isinstance(ids, list) or len(ids) > 20000:
                    raise ValueError("勾選病歷格式不正確。")
                records = [self.app.store.library.get_record(key) for key in dict.fromkeys(ids)]
                if any(r is None for r in records):
                    raise ValueError("部分病歷已刪除，請重新選取。")
            for record in records:
                add(record["mrn"], record.get("name", ""), record.get("accounts", []), [record["id"]])
        elif values.get("source") == "list":
            account = self.app._account(values.get("account_id"))
            refs = values.get("rows", [])
            if not isinstance(refs, list) or len(refs) > 20000:
                raise ValueError("門診清單格式不正確。")
            for ref in refs:
                if not isinstance(ref, dict):
                    raise ValueError("門診選取格式不正確。")
                day = ref.get("day", "")
                cached = self.app.store.library.cached_list(account.username, day, allow_stale=True)
                from .clinical_identity import classify_opd_registration
                from .library import registration_id
                from .workflow import patient_model

                row = next((r for r in (cached or {}).get("rows", []) if registration_id(r) == ref.get("id")), None)
                if not row or classify_opd_registration(patient_model(row), doctor_card=account.username) != "DEDICATED":
                    raise ValueError("門診清單已變更或病人不屬於所選帳號，請重新載入。")
                add(row["mrn"], row.get("name", ""), [account.username], selected=account.id,
                    registration={"account": account.username, "day": day, "id": ref["id"]})
        elif values.get("source") == "manual":
            account = self.app._account(values.get("account_id"))
            for mrn in parse_mrns(values.get("mrns")):
                records = self.app.store.library.search({"mrn": mrn}, self.app.settings, all_results=True)["records"]
                add(mrn, next((r["name"] for r in records if r.get("name")), ""),
                    {a for r in records for a in r.get("accounts", [])}, [r["id"] for r in records], account.id)
                grouped[mrn]["origin"] = "manual"
        elif values.get("source") == "tags":
            records = self.app.store.library.tag_patients(values.get("filters", {}), self.app.settings,
                                                           all_results=True)["patients"]
            if not values.get("all"):
                ids = parse_mrns(values.get("mrns"), limit=20000)
                if set(ids) - {r["mrn"] for r in records}:
                    raise ValueError("tag 群組已變更，請重新選取病人。")
                records = [r for r in records if r["mrn"] in set(ids)]
            for record in records:
                add(record["mrn"], record["name"], record["accounts"], record["source_records"])
                grouped[record["mrn"]]["origin"] = "tags"
        else:
            raise ValueError("請輸入病歷號，或從門診清單、病歷資料庫選取病人。")
        if not grouped:
            raise ValueError("請至少選取一位病人。")
        return list(grouped.values())

    def test_google(self, _):
        client = self.client_factory()
        try:
            return client.connection_info()
        finally:
            client.close()

    def save_cohort(self, values):
        with self.app.lock:
            self.app._available()
            name = values.get("name", "").strip()
            if not name or len(name) > 100:
                raise ValueError("分析清單名稱需為 1–100 字。")
            if values.get("id"):
                cohort = self.store.document("analysis_cohorts", values["id"])
                assignments = values.get("assignments", {})
                if not isinstance(assignments, dict):
                    raise ValueError("抓取帳號格式不正確。")
                removed = values.get("remove_mrns", [])
                cohort["members"] = [m for m in cohort["members"] if m["mrn"] not in removed]
                if not cohort["members"]:
                    raise ValueError("分析清單至少需保留一位病人。")
                for member in cohort["members"]:
                    key = assignments.get(member["mrn"], member["account_id"])
                    if key and key not in self.app.accounts:
                        raise ValueError("所選抓取帳號已刪除。")
                    member["account_id"] = key
            else:
                cohort = {"id": uuid.uuid4().hex, "members": self.resolve_members(values),
                          "created_at": timestamp(), "selection": values.get("filters", {})}
                if values.get("account_id") and values.get("source") == "tags":
                    account = self.app._account(values["account_id"])
                    for member in cohort["members"]:
                        member["account_id"] = account.id
            cohort.update(name=name, updated_at=timestamp())
            return self.store.save_document("analysis_cohorts", cohort)

    def delete_cohort(self, values):
        key = identifier(values.get("id"))
        with self.cataract_queue.condition, self.app.lock:
            if any(r.get("cohort_id") == key and r.get("status") in ACTIVE for r in self.store.documents("analysis_runs")):
                raise ValueError("請先停止此清單的分析。")
            self.cataract_queue.discard(key)
            with self.store.library.connect() as db:
                db.execute("DELETE FROM analysis_cohorts WHERE id=?", (key,))
        return {"ok": True}

    @staticmethod
    def options(values):
        modules = values.get("modules", [])
        if not isinstance(modules, list) or not modules or any(m not in MODULES for m in modules):
            raise ValueError("請選擇分析模組。")
        result = {"modules": list(dict.fromkeys(modules))}
        for key in ("start", "end"):
            value = values.get(key, "")
            if not isinstance(value, str):
                raise ValueError("分析日期格式不正確。")
            if value:
                day = date.fromisoformat(value)
                if not date(1912, 1, 1) <= day <= today():
                    raise ValueError("檢查分析日期需介於 1912 年與今天。")
            result[key] = value
        if result["start"] and result["end"] and result["end"] < result["start"]:
            raise ValueError("結束日期不可早於開始日期。")
        for key in ("refresh", "force"):
            if type(values.get(key, False)) is not bool:
                raise ValueError("更新選項不正確。")
            result[key] = values.get(key, False)
        if "mrn" in values:
            mrn = values["mrn"]
            if result["modules"] != ["cataract"] or not isinstance(mrn, str) or not mrn.strip():
                raise ValueError("僅白內障術前分析可指定單一病人。")
            result["mrn"] = mrn.strip()
        return result

    def start(self, values, *, enqueue=True):
        with self.app.lock:
            self.app._available()
            if values.get("resume"):
                original = self.store.document("analysis_runs", values["resume"])
                if original["status"] in ACTIVE:
                    raise ValueError("此分析仍在執行。")
                cohort = {"id": original["cohort_id"], "name": original["name"],
                          "members": original["members"]}
                current = next((c for c in self.store.documents("analysis_cohorts")
                                if c["id"] == original["cohort_id"]), None)
                if current:
                    assignments = {m["mrn"]: m["account_id"] for m in current["members"]}
                    for member in cohort["members"]:
                        member["account_id"] = assignments.get(member["mrn"], member["account_id"])
                options = {**original["options"], "refresh": False, "force": False}
            else:
                cohort = self.store.document("analysis_cohorts", values.get("cohort_id"))
                options = self.options(values)
                if options.get("mrn"):
                    cohort = {**cohort, "members": [member for member in cohort["members"]
                                                   if member["mrn"] == options["mrn"]]}
                    if not cohort["members"]:
                        raise ValueError("病人不在此分析清單。")
                    if (getattr(self.app, "account_id", None)
                            and cohort["members"][0]["account_id"] != self.app.account_id):
                        raise ValueError("此病人未指定給目前登入帳號。")
            settings = {}
            for member in cohort["members"]:
                account = self.app._account(member.get("account_id"))
                settings[account.id] = replace(self.app.settings, username=account.username,
                                              password=account.password, response_encoding=account.response_encoding)
            run_id = uuid.uuid4().hex
            state = ScanState(today(), today(), id=run_id, kind="analysis", account=cohort["name"],
                              account_label="", account_id="", status="queued", stage="queued",
                              categories=[], message="等待分析…", cohort_id=cohort["id"],
                              modules=options["modules"],
                              name="／".join(MODULES[key]["name"] for key in options["modules"]),
                              cataract_background=not enqueue)
            state.data["counts"].update(patients_total=len(cohort["members"]), analysis_cached=0, analysis_fetched=0)
            state.journal = self.app.store.create(state.snapshot(detail=False))
            run = {"id": run_id, "cohort_id": cohort["id"], "name": cohort["name"], "members": cohort["members"],
                   "options": options, "created_at": timestamp(), "status": "queued", "message": "等待分析…",
                   "counts": state.data["counts"], "issues": [], "resume_of": values.get("resume", "")}
            self.store.save_document("analysis_runs", run)
            self.app.states[run_id] = state
            self.app.idle.clear()
            source = {"analysis": run, "settings": settings}
            if not enqueue:
                return state, self.app.settings, source
            if options.get("mrn"):
                self.cataract_queue.invalidate(options["mrn"])
            self.app.queue.put((state, self.app.settings, source))
            return {"run_ids": [run_id]}

    def execute(self, state, source):
        run = source["analysis"]
        state.update(status="running", stage="analysis", message="正在整理分析資料…")
        sessions = RunSessions(self.app.sdk_factory, state)
        try:
            for member in run["members"]:
                state.check_cancel()
                collector = PatientCollector(
                    self.store, state, member, source["settings"][member["account_id"]],
                    self.app.sdk_factory,
                    {**run["options"], "lazy_cataract_orders":
                     run["options"]["modules"] == ["cataract"] and hasattr(self.app, "review")
                     and not run["options"].get("mrn")},
                    sessions, getattr(self.app, "review", None))
                try:
                    collector.run()
                except (Cancelled, StorageError):
                    raise
                except Exception as exc:
                    message, code = safe_failure(exc)
                    state.issue("病人分析", message, code=code, mrn=member["mrn"])
                state.count(patients_done=1)
                self.finish(state)
        finally:
            sessions.close()
        state.check_cancel()
        state.update(status="partial" if state.data["counts"]["errors"] else "completed",
                     stage="done", message="分析完成；資料已保存。" if not state.data["counts"]["errors"]
                     else "部分查詢未完成；已保存資料可先檢閱，請查看 DEBUG 後續跑補抓。")

    def finish(self, state):
        if state.data.get("kind") != "analysis":
            return
        run = self.store.document("analysis_runs", state.data["id"])
        run.update({k: copy.deepcopy(state.data[k]) for k in ("status", "message", "counts", "issues")})
        self.store.save_document("analysis_runs", run)

    def member(self, values):
        cohort = self.store.document("analysis_cohorts", values.get("cohort_id"))
        member = next((m for m in cohort["members"] if m["mrn"] == values.get("mrn")), None)
        if not member:
            raise ValueError("病人不在此分析清單。")
        return member

    def cataract_status(self, values):
        # Queue locks are acquired before the library transaction, matching the
        # scheduler's lock order. The summary never reads SOAP version payloads.
        queue = self.cataract_queue.snapshot(values.get("cohort_id"))
        with self.store.library.batch():
            return self._cataract_status(values, queue)

    def _cataract_status(self, values, queue):
        cohort = self.store.document("analysis_cohorts", values.get("cohort_id"))
        runs = [run for run in self.store.documents("analysis_runs")
                if run["cohort_id"] == cohort["id"] and run["options"]["modules"] == ["cataract"]]
        members = []
        for member in cohort["members"]:
            mrn = member["mrn"]
            soap = self.store.step(mrn, "cataract-soap")
            soap_status = soap["payload"].get("status") if soap else "pending"
            soap_record = None
            if soap_status == "ready":
                with self.store.library.connect() as db:
                    soap_record = db.execute("SELECT 1 FROM records WHERE id=? AND mrn=? "
                                             "AND id NOT IN (SELECT record_id FROM pending_deletions)",
                                             (soap["payload"].get("record_id"), mrn)).fetchone()
            ready = all(self.store.step(mrn, key) is not None for key in
                        ("numeric-history", "orders-history:*", "orders-history:OR", "visits"))
            ready = ready and soap_status in {"ready", "missing", "no_visit"}
            ready = ready and (soap_status != "ready" or soap_record is not None)
            orders = self.cataract_orders(mrn) if ready else []
            ready = ready and all(order["report_complete"] for order in orders)
            related = [run for run in runs if any(row["mrn"] == mrn for row in run["members"])]
            active = next((run for run in related if run["status"] in ACTIVE), None)
            cleared = self.app.library_data.cleared(mrn)
            attempted = cleared or any(run["options"].get("mrn") == mrn for run in related)
            resumable = next((run for run in related if run["options"].get("mrn") == mrn
                              and run["status"] in {"paused", "partial", "failed", "cancelled", "interrupted"}), None)
            members.append({"mrn": mrn, "ready": ready, "attempted": attempted,
                            "cache_cleared": cleared and not ready,
                            "active_run_id": active["id"] if active else "",
                            "resume_run_id": resumable["id"] if resumable else "",
                            "orders_total": len(orders),
                            "orders_complete": sum(order["report_complete"] for order in orders),
                            "soap_status": soap_status,
                            "data_revision": self.store.patient_revision(mrn)})
        current = next((run for run in runs if run["status"] in ACTIVE), None)
        return {"members": members, "ready": all(row["ready"] for row in members),
                "active_run_id": current["id"] if current else "",
                "resume_run_id": "", "queue": queue}

    def cataract_orders(self, mrn, start="", end=""):
        with self.store.library.batch():
            return self._cataract_orders(mrn, start, end)

    def _cataract_orders(self, mrn, start="", end=""):
        history = self.app.review.history
        available_assets = {}

        def available(sha):
            if not isinstance(sha, str):
                return False
            if sha not in available_assets:
                try:
                    self.store.asset(sha)
                    available_assets[sha] = True
                except ValueError:
                    available_assets[sha] = False
            return available_assets[sha]

        allowed = history._allowed_mrns(mrn)
        groups = {}
        for order in history._order_sources(mrn):
            if not isinstance(order, dict) or order.get("mrn") not in allowed:
                continue
            try:
                validate_patient(order, order["mrn"])
            except (TypeError, ValueError):
                continue
            if not any(term_match(order.get("name", ""), term)
                       for term in MODULES["cataract"]["orders"]):
                continue
            groups.setdefault(order_identity(order), []).append(order)
        rows = []
        for order in history._public_orders(groups):
            if order["date"] and ((start and order["date"] < start)
                                  or (end and order["date"] > end)):
                continue
            saved_history = self.store.step(mrn, "history-order:" + order["id"])
            saved_collected = self.store.step(mrn, "collected-order:" + order["id"])
            def complete(step):
                if not step or step["payload"].get("status") not in {
                    "complete", "ready", "missing", "not_executed", "no_data", "no_links"
                }:
                    return False
                try:
                    return all(available(asset["digest"]) for asset in step["payload"].get("assets") or [])
                except (KeyError, TypeError, ValueError):
                    return False

            saved = next((step for step in (saved_history, saved_collected) if complete(step)),
                         saved_collected or saved_history)
            payload = saved["payload"] if saved else {}
            assets = payload.get("assets") or []
            assets_ready = bool(saved and complete(saved))
            attachments = []
            seen = set()
            for asset in assets:
                if not isinstance(asset, dict) or not asset.get("digest") or not asset.get("mime") or asset["digest"] in seen:
                    continue
                seen.add(asset["digest"])
                attachments.append({"digest": asset["digest"], "mime": asset["mime"],
                                    "size": asset.get("size", 0), "name": f"檔案 {len(attachments) + 1}",
                                    "available": available(asset["digest"])})
            rows.append({**order, "exams": [term for term in MODULES["cataract"]["orders"]
                                           if term_match(order["name"], term)],
                         "status": payload.get("status", "indexed"),
                         "report_loaded": bool(saved),
                         "report_complete": assets_ready,
                         "asset_count": len(attachments), "attachments": attachments,
                         "saved_at": saved["saved_at"] if saved else ""})
        return rows

    def cataract_soap(self, mrn):
        marker = self.store.step(mrn, "cataract-soap")
        record = None
        if marker and marker["payload"].get("record_id"):
            record = self.store.library.get_record(marker["payload"]["record_id"], include_versions=False)
        elif not marker:
            cached = next((row for row in self.store.records(mrn)
                           if "眼科" in (row.get("section", "") + " " + row.get("section_code", ""))), None)
            record = self.store.library.get_record(cached["id"], include_versions=False) if cached else None
        if record and record["mrn"] == mrn:
            keys = ("id", "mrn", "name", "date", "section", "case_no", "soap", "soap_structure")
            return {"status": marker["payload"]["status"] if marker else "cached",
                    "record": {key: record.get(key) for key in keys}, "updated_at": record["updated_at"]}
        return {"status": marker["payload"].get("status", "pending") if marker else "pending",
                "record": None, "updated_at": ""}

    def results(self, values):
        with self.store.library.batch():
            return self._results(values)

    def _results(self, values):
        member = self.member(values)
        module = values.get("module", "retina")
        if module not in MODULES:
            raise ValueError("分析模組不正確。")
        start, end = values.get("start", ""), values.get("end", "")
        self.options({"modules": [module], "start": start, "end": end})
        steps = self.store.steps(member["mrn"])
        numeric_rows = []
        # A table row repeated in a history and a case view is the same source
        # observation; preserve the greatest multiplicity within any one source.
        seen_counts = Counter()
        for step in sorted((s for s in steps if s["kind"] == "numeric"),
                           key=lambda s: (s["key"] != "numeric-history", s["key"])):
            local_counts = Counter()
            writable = not getattr(getattr(self.app, "root", None), "read_only", False)
            for row in self.store.numeric_rows(step, persist=writable):
                key = digest([row["title"], row["headers"], row["values"], row["date"]])
                local_counts[key] += 1
                if local_counts[key] <= seen_counts[key]:
                    continue
                if not any(exam in MODULES[module]["numeric"] for exam in row["exams"]):
                    continue
                if row["date"] and ((start and row["date"] < start) or (end and row["date"] > end)):
                    continue
                row["cells"] = [c for c in row["cells"] if c["exam"] in MODULES[module]["numeric"]]
                row["measurements"] = [m for m in row.get("measurements", [])
                                       if m["exam"] in MODULES[module]["numeric"]]
                numeric_rows.append(row)
            seen_counts |= local_counts
        orders = []
        for step in steps:
            row = step["payload"]
            if step["kind"] != "order" or not any(term_match(row["name"], t) for t in MODULES[module]["orders"]):
                continue
            if row["date"] and ((start and row["date"] < start) or (end and row["date"] > end)):
                continue
            orders.append({**row, "saved_at": step["saved_at"],
                           "exams": [t for t in MODULES[module]["orders"] if term_match(row["name"], t)]})
        coverage = next((s["payload"] for s in steps if s["kind"] == "coverage"), None)
        if module == "cataract" and hasattr(self.app, "review"):
            orders = self.cataract_orders(member["mrn"], start, end)
        return {"member": member, "module": module, "numeric": sorted(numeric_rows, key=lambda r: (r["date"], r["id"])),
                "orders": sorted(orders, key=lambda r: (r["date"], r["id"]), reverse=module == "cataract"),
                "latest_soap": self.cataract_soap(member["mrn"]) if module == "cataract" else None,
                "coverage": coverage,
                "data_revision": self.store.patient_revision(member["mrn"]) if module == "cataract" else "",
                "updated_at": max((s["saved_at"] for s in steps), default="")}

    def raw_data(self, values):
        member = self.member(values)
        return {"member": member, **self.store.raw_data(member["mrn"])}

    def delete_data(self, values):
        with self.app.lock:
            if not self.app.idle.is_set() or self.sheet_busy:
                raise ValueError("請先等待或停止目前作業，再刪除分析資料。")
            member = self.member(values)
            return self.app.library_data.delete({"mrns": [member["mrn"]],
                "categories": ["numeric", "orders", "scans", "visits", "analysis"]}, compatibility=True)

    def surgery_candidates(self, values):
        cohort = self.store.document("analysis_cohorts", values.get("cohort_id"))
        result = []
        for member in cohort["members"]:
            rows = candidates(self.store.records(member["mrn"]))
            result.extend(enrich(rows, self.store.steps(member["mrn"])))
        return {"candidates": result, "fields": FIELD_NAMES, "google": self.google.public()}

    def proposals(self, values):
        offered = {c["id"]: c for c in self.surgery_candidates(values)["candidates"]}
        supplied = values.get("proposals", [])
        if not isinstance(supplied, list) or not 1 <= len(supplied) <= 200:
            raise ValueError("每次請選擇 1–200 筆手術預覽。")
        result = []
        for proposal in supplied:
            row = offered.get(proposal.get("id")) if isinstance(proposal, dict) else None
            if not row:
                raise ValueError("手術來源已變更或刪除，請重新載入。")
            if row["cancelled_hint"] and not proposal.get("reviewed"):
                raise ValueError("含取消文字的手術需先勾選「已核對來源」。")
            if row["date_conflict"] and not proposal.get("reviewed"):
                raise ValueError("SOAP 與院內排程日期不同，請核對後勾選「已核對來源」。")
            updates = proposal.get("fields", {})
            if not isinstance(updates, dict) or set(updates) - set(FIELD_NAMES):
                raise ValueError("手術欄位格式不正確。")
            fields_ = {**row["fields"], **updates}
            if any(not isinstance(v, str) or len(v) > 4000 or "\x00" in v for v in fields_.values()):
                raise ValueError("手術欄位內容不正確或過長。")
            if fields_["mrn"] != row["fields"]["mrn"]:
                raise ValueError("不可修改來源病歷號。")
            if not fields_["date"] or not fields_["procedure"] or fields_["side"] not in {"OD", "OS", "OU"}:
                raise ValueError("請填妥刀日、側別與術式。")
            date.fromisoformat(fields_["date"])
            selected = proposal.get("selected_fields", list(FIELD_NAMES))
            clear = proposal.get("clear_fields", [])
            if not isinstance(selected, list) or not isinstance(clear, list) or set(selected + clear) - set(FIELD_NAMES):
                raise ValueError("勾選欄位不正確。")
            result.append({**proposal, "fields": fields_, "selected_fields": selected, "clear_fields": clear,
                           "source_records": [s["record_id"] for s in row["sources"]]})
        return result

    def source_digest(self, mrns):
        return digest({mrn: self.store.records(mrn) for mrn in mrns})

    @staticmethod
    def public_preview(preview):
        return {k: v for k, v in preview.items() if k not in {"requests", "expectations", "baseline", "source_digest"}}

    def sheet_operation(self, callback):
        # A v5 root shares this lock across its account workspaces. A second
        # account waits; duplicate operations within one workspace stay blocked.
        with self.app.lock:
            if self.sheet_busy:
                raise ValueError("刀表作業進行中，請稍候。")
            self.sheet_busy = True
        if not self.sheet_lock.acquire(blocking=hasattr(self.app, "root")):
            self.sheet_busy = False
            raise ValueError("刀表作業進行中，請稍候。")
        client = None
        try:
            client = self.client_factory()
            return callback(client)
        finally:
            if client:
                client.close()
            self.sheet_busy = False
            self.sheet_lock.release()

    def preview(self, values):
        proposals = self.proposals(values)

        def work(client):
            before = self.source_digest({p["fields"]["mrn"] for p in proposals})
            preview = Planner(client.read(), client.key, proposals).build()
            preview["source_digest"] = before
            preview["cohort_id"] = values["cohort_id"]
            self.store.save_document("sheet_previews", preview)
            return self.public_preview(preview)
        return self.sheet_operation(work)

    def _check(self, client, preview):
        book = client.read()
        outcome = verify(book, preview)
        if outcome == "verified":
            preview.update(status="applied", message="已寫入並核對。", applied_at=timestamp())
        elif outcome == "changed":
            preview.update(status="applied_changed", message="更新已送達，但部分內容之後有變動，請檢查刀表。")
        elif preview["status"] in {"applying", "uncertain"}:
            preview.update(status="uncertain", message="尚未找到此批更新；可重試原預覽，系統以同一批次代碼防止重複新增。")
        self.store.save_document("sheet_previews", preview)
        return book, outcome

    def reconcile(self, values):
        preview = self.store.document("sheet_previews", values.get("id"))

        def work(client):
            if client.key != preview["spreadsheet_id"]:
                raise ValueError("刀表設定已改變，請切回此預覽的刀表。")
            self._check(client, preview)
            return self.public_preview(preview)
        return self.sheet_operation(work)

    def apply(self, values):
        if not self.app.idle.is_set():
            raise ValueError("請等待目前抓取作業結束，再套用刀表更新。")
        preview = self.store.document("sheet_previews", values.get("id"))
        if not preview.get("can_apply"):
            raise ValueError("請先處理預覽中的衝突。")

        def work(client):
            if client.key != preview["spreadsheet_id"]:
                raise ValueError("刀表設定已改變，請重新預覽。")
            book, outcome = self._check(client, preview)
            if outcome != "not_found":
                return self.public_preview(preview)
            if preview["status"] in {"applied", "applied_changed"}:
                raise ValueError("此批更新曾完成，禁止重送；請檢查刀表。")
            if self.source_digest(preview["mrns"]) != preview["source_digest"]:
                raise ValueError("來源 SOAP 已變更或刪除，請重新預覽。")
            if fingerprint(book) != preview["baseline"]:
                raise ValueError("刀表內容、位置或格式已變更，請重新產生預覽。")
            preview.update(status="applying", message="正在套用已預覽的更新…")
            self.store.save_document("sheet_previews", preview)
            try:
                client.write(preview["requests"])
            except SheetUncertain:
                preview.update(status="uncertain", message="回應中斷，請先核對更新結果。")
                self.store.save_document("sheet_previews", preview)
                return self.public_preview(preview)
            except SheetError:
                # A concurrent retry may have committed our unique metadata ID.
                try:
                    _, outcome = self._check(client, preview)
                    if outcome != "not_found":
                        return self.public_preview(preview)
                except SheetError:
                    pass
                preview.update(status="failed", message="Google 拒絕更新，原預覽保留。")
                self.store.save_document("sheet_previews", preview)
                raise
            try:
                self._check(client, preview)
            except SheetError:
                preview.update(status="uncertain", message="已送出更新，讀回核對失敗；請核對更新結果。")
                self.store.save_document("sheet_previews", preview)
            return self.public_preview(preview)
        return self.sheet_operation(work)

    def sheet_history(self, _):
        return {"previews": [self.public_preview(p) for p in self.store.documents("sheet_previews")]}
