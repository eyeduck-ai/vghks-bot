"""Honest work-unit progress; unknown totals never become guessed percentages."""


def progress(done=0, total=None, *, unit="項", failed=0, stage=""):
    done = max(0, int(done))
    total = None if total is None else max(0, int(total))
    return {"done": done, "total": total, "unit": unit, "failed": max(0, int(failed)),
            "stage": stage, "percent": min(100, int(done * 100 / total)) if total else None}


def run_progress(data):
    if data.get("kind") == "bot" and data.get("progress"):
        return data["progress"]
    counts = data.get("counts", {})
    if data.get("kind") == "list" or data.get("stage") == "registrations":
        return progress(counts.get("days_done", 0), counts.get("days_total"), unit="天",
                        failed=counts.get("days_failed", 0), stage=data.get("message", ""))
    if counts.get("patients_total"):
        return progress(counts.get("patients_done", 0), counts["patients_total"], unit="位病人",
                        failed=counts.get("errors", 0), stage=data.get("message", ""))
    return progress(stage=data.get("message", ""))
