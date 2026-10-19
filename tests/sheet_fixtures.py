"""Synthetic workbook cells for main-program schema diagnostics."""
MONTH_HEADERS = ["日期／報到時間", "醫院", "病歷號", "姓名", "TEL", "GA", "側別", "診斷",
                 "Grade", "術式", "IOL", "IOL Target", "IOL Final", "Axis", "Plan", "心得",
                 "CalendarEventId", "ScheduleSyncId"]
FU_HEADERS = ["病歷號", "姓名", "TEL", "醫院", "Tag", "Condition", "日期", "Plan", "心得",
              "CalendarEventId", "ScheduleSyncId"]


def cell(value):
    return {} if value is None or value == "" else {"userEnteredValue": {
        "numberValue" if isinstance(value, (int, float)) else "stringValue": value}}


def row(labels, **values):
    return [cell(values.get(label, "")) for label in labels]


def monthly(title="202610", sid=1, *, ids=True):
    labels = MONTH_HEADERS if ids else MONTH_HEADERS[:-2]
    return {"properties": {"title": title, "sheetId": sid, "gridProperties": {"rowCount": 100, "columnCount": len(labels)}},
            "rows": [list(map(cell, labels)),
                     row(labels, **{"日期／報到時間": "2026-10-12", "醫院": "高榮", "病歷號": "◆ 刀日", "ScheduleSyncId": "a" * 32}),
                     row(labels, **{"日期／報到時間": "08:30", "醫院": "聯醫", "病歷號": "00001", "姓名": "合成甲", "側別": "OD", "術式": "CATA", "ScheduleSyncId": "b" * 32}),
                     row(labels, **{"日期／報到時間": "2026-10-12", "醫院": "聯醫", "病歷號": "◆ 刀日", "ScheduleSyncId": "c" * 32}),
                     row(labels, **{"病歷號": "00002", "姓名": "合成乙", "ScheduleSyncId": "d" * 32})],
            "column_widths": [], "protectedRanges": []}


def workbook(*sheets):
    return {"properties": {"title": "Synthetic only"}, "sheets": list(sheets or [monthly()]), "developerMetadata": []}
