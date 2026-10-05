"""A durable daily MIS check; no OS scheduler or background service required."""
from .approval_tracker import later
from .monitoring import interval
from .settings import timestamp


class EarningsMonitor:
    def __init__(self, earnings):
        self.earnings, self.app, self.db = earnings, earnings.app, earnings.db

    def preferences(self, values=None):
        with self.app.root.lock:
            current = self.db.get("preferences", "earnings_monitor", required=False) or {
                "id": "earnings_monitor", "enabled": True, "hours": 24, "next_check": ""}
            if values is None:
                return current
            if type(values.get("enabled")) is not bool:
                raise ValueError("請設定監控開關。")
            frequency = interval(values, current)
            next_check = current.get("next_check", "")
            if values["enabled"] and not current["enabled"]:
                next_check = timestamp()
            elif current.get("last_attempt"):
                next_check = later(current["last_attempt"], frequency["hours"])
            return self.db.save("preferences", {**current, **frequency, "enabled": values["enabled"], "next_check": next_check})

    def started(self, task):
        if task["kind"] != "earnings_capture" or not task["all_available"] or not task["force"]:
            return
        value, now = self.preferences(), timestamp()
        self.db.save("preferences", {**value, "last_task": task["id"], "last_attempt": now,
                                     "next_check": later(now, value["hours"])})

    def overview(self):
        value = self.preferences()
        tasks = self.db.task_summaries(key=value.get("last_task", ""))
        task = tasks[0] if tasks else None
        configured = self.earnings.public_credentials()["configured"]
        return {**value, "configured": configured, "due": value["enabled"] and configured and value["next_check"] <= timestamp(),
                "last_status": task["status"] if task else "", "last_message": task.get("message", "") if task else ""}

    def schedule(self):
        if self.app.root.read_only or self.app.closing or not self.app.gateway.online or not self.app.entered:
            return
        value = self.overview()
        if not value["due"]:
            return
        if any(t["kind"].startswith("earnings_") and t["status"] in {"queued", "running", "cancelling", "paused"}
               for t in self.db.task_summaries(kinds=('earnings_options','earnings_capture'))):
            return
        # Open both current month menus and re-read every still-published report.
        # The archive deduplicates unchanged content and retains changed versions.
        self.app.review.start({"kind": "earnings_capture", "all_available": True, "force": True, "automatic": True})
