"""Registration HTML through the pinned SDK and production account cache, offline."""
from __future__ import annotations

from datetime import timedelta
from html import escape

from .selftest_session import MRN, SessionScenario
from .settings import today


def registration_page(mrn=MRN, *, empty=False):
    headers = ("病歷號", "姓名", "看診日期", "科別", "診別", "序號", "預計看診",
               "狀態", "掛號時間", "掛號人員", "取消時間", "取消人員", "註記", "院方新增欄位")
    future, past = (today()+timedelta(days=7)).isoformat(), (today()-timedelta(days=5)).isoformat()
    rows = [
        (future, "001", "09:30", "", "MWEB", "", "", "預約 <保留原文>", ""),
        (past, "007", "10:00", "", "OPD_SYN", "", "", "回診", "known"),
        (past, "007", "10:00", "", "OPD_SYN", "", "", "回診", ""),
        (past, "010", "", "取消", "OPD_SYN", "115/10/01 08:30", "STAFF_SYN", "取消註記", ""),
        ("", "011", "", "保留", "UNKNOWN_SYN", "", "", "未知圖示", "unknown"),
    ]
    html = '<table id="row"><thead><tr>' + "".join(f"<th>{escape(h)}</th>" for h in headers) + '</tr></thead><tbody>'
    if empty:
        return html + f'<tr><td colspan="{len(headers)}">查無資料</td></tr></tbody></table>'
    for day, sequence, expected, status, by, cancelled, cancelled_by, notes, icon in rows:
        values = (mrn, "合成病人", day, "70 眼科", "02", sequence, expected, status,
                  "115/09/25 07:30", by, cancelled, cancelled_by, notes, "新欄內容 <保留>")
        cells = [escape(v) for v in values]
        if icon:
            image = "Rsdefyrc.png" if icon == "known" else "Unknown.png"
            cells[12] += f'<label title="未到診"><img src="../images/icon/{image}"/></label>'
        html += '<tr>' + "".join(f"<td>{cell}</td>" for cell in cells) + '</tr>'
    return html + '</tbody></table>'


class RegistrationScenario(SessionScenario):
    def __init__(self):
        super().__init__(mode="ready")
        self.response_mrn, self.empty, self.broken = MRN, False, False

    @property
    def queries(self):
        return [entry for entry in self.requests if entry["method"] == "POST"
                and entry["url"].endswith("/RSV/RSV11W001.do")]

    def request(self, method, url, **kwargs):
        response = super().request(method, url, **kwargs)
        if method == "POST" and url.endswith("/RSV/RSV11W001.do"):
            body = "<p>Unknown page</p>" if self.broken else registration_page(self.response_mrn, empty=self.empty)
            response._content = body.encode("utf-8")
        return response


def check_sdk_registrations(directory):
    from .bot import BotApplication
    from .review import REGISTRATION_SCHEMA
    from .selftest_bot import wait_task
    from .settings import Settings

    scenario = RegistrationScenario()
    app = BotApplication(Settings(), directory, scenario.factory)
    try:
        account = app.login({"username": "SYNTHETIC", "password": "synthetic"})["account"]["id"]
        work = app.workspace(account)
        task = work.review.start({"kind": "registrations", "mrn": MRN})["task_id"]
        result = wait_task(work, task)
        assert result["status"] == "completed", result
        saved, = result["items"]
        assert saved["registration_schema"] == REGISTRATION_SCHEMA
        assert saved["sdk_version"] == "0.23.5"
        rows = saved["payload"]
        assert len(rows) == 5 and [row["no_show"] for row in rows] == [None, True, None, None, None]
        assert rows[0]["sequence_no"] == "001" and rows[0]["expected_time"] == "09:30"
        assert rows[0]["registered_by"] == "MWEB" and rows[1]["registered_by"] == "OPD_SYN"
        assert rows[3]["status"] == "取消" and rows[3]["cancelled_by"] == "STAFF_SYN"
        assert rows[0]["columns"]["院方新增欄位"] == "新欄內容 <保留>"
        assert rows[0]["notes"] == "預約 <保留原文>" and rows[4]["visit_date"] is None
        task = work.review.start({"kind": "registrations", "mrn": MRN})["task_id"]
        reused, = wait_task(work, task)["items"]
        assert reused["cached"] and len(scenario.queries) == 1
        app.logout(account)
        assert work.review.db.get("registrations", MRN)["payload"] == rows
    finally:
        app.close()
