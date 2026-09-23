"""Entirely synthetic clinical data and an atomic, in-memory Sheets test double."""

import copy
from datetime import date, timedelta
from types import SimpleNamespace

from vghks_sdk.models import (
    BinaryAsset,
    ClinicalOrder,
    NumericHistoryReport,
    NumericReport,
    NumericTable,
    OrderDetail,
    OrderDetailRef,
    OrderReport,
    OrderReportRef,
    PacsImageRef,
    PacsStudy,
    PacsStudyRef,
    PdfAttachmentRef,
    SurgeryRecord,
    VisitCase,
)

from opd_monitor.analysis_store import digest
from opd_monitor.google_sheets import SheetError, SheetUncertain
from opd_monitor.settings import today
from opd_monitor.sheet_plan import HEADERS


class ExamSDK:
    calls = []
    fail_pdf = False
    fail_numeric = False
    cancel_callback = None
    generation = 0

    def __init__(self, settings):
        self.card = settings.username
        self.calls.append(("login", self.card))
        self.auth = SimpleNamespace(check=lambda **kwargs: SimpleNamespace(ok=True))
        self.records = SimpleNamespace(
            get_visit_cases=self.visits,
            get_numeric_history=self.numeric,
            get_numeric_report=self.case_numeric,
        )
        self.orders = SimpleNamespace(
            get_order_history=self.orders_history,
            get_case_orders=self.case_orders,
            get_order_detail=self.detail,
            get_order_report=self.report,
            get_pacs_study=self.pacs,
            download_pdf=self.pdf,
            download_pacs_image=self.image,
        )
        self.surgery = SimpleNamespace(
            get_patient_info=self.patient_info, get_schedule=self.schedule
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def visits(self, mrn):
        self.calls.append(("visits", mrn))
        return [
            VisitCase(mrn, date(2010, 1, 1), "O", "OLD", "70", "眼科"),
            VisitCase(mrn, date(2011, 1, 1), "A", "IPD", "70", "眼科"),
            VisitCase(mrn, today() - timedelta(days=5), "O", "RECENT", "70", "眼科"),
        ]

    def numeric(self, mrn, history_filter):
        self.calls.append(("numeric", mrn))
        if self.fail_numeric:
            raise ValueError("synthetic numeric failure")
        if type(self).cancel_callback:
            type(self).cancel_callback()
        return NumericHistoryReport(
            mrn,
            (
                NumericTable(
                    "Va",
                    ("日期", "OD", "OS"),
                    (
                        ("2024-01-01", "0.3", "HM"),
                        ("2025-01-01", "0.6", "CF"),
                        ("2025-01-01", "0.7", "CF"),
                    ),
                ),
                NumericTable(
                    "驗光-散瞳前",
                    ("日期", "側別", "SPH (D)", "CYL (D)", "Axis (°)"),
                    (("2025-01-01", "OD", "-1.50", "-0.75", "90"),),
                ),
                NumericTable(
                    "Endothelial No",
                    ("日期", "OD (cells/mm²)", "OS (cells/mm²)"),
                    (("2025-01-01", "2500", "2400"),),
                ),
                NumericTable(
                    "KM", ("日期", "OD K1 (D)", "OD K2 (D)"), (("2025-01-01", "43", "44"),)
                ),
            ),
        )

    def case_numeric(self, case):
        self.calls.append(("case_numeric", case.mrn, str(case.visit_date)))
        return NumericReport(case, (NumericTable("Va", ("OD", "OS"), (("0.1", "0.2"),)),))

    def orders_history(self, mrn, history_filter):
        self.calls.append(("orders", mrn, history_filter.category))
        result = []
        names = ["Microsonography", "DBR, free charge", "FAG", "Fundus Color Photo Picture, eac"]
        for index, name in enumerate(names, 1):
            ref = OrderDetailRef(mrn, "RECENT", "O", str(index))
            result.append(
                ClinicalOrder(
                    mrn,
                    "RECENT",
                    "O",
                    name,
                    "2025-01-01",
                    "2025-01-02",
                    status="已執行",
                    detail_ref=ref,
                    extra={"generation": self.generation},
                )
            )
        result.append(
            ClinicalOrder(
                mrn,
                "RECENT",
                "O",
                "FAG",
                "2025-02-01",
                status="未執行",
                detail_ref=OrderDetailRef(mrn, "RECENT", "O", "99"),
            )
        )
        return result

    def case_orders(self, case):
        self.calls.append(("case_orders", case.mrn))
        return []

    def detail(self, ref):
        self.calls.append(("detail", ref.mrn, ref.sequence_no))
        return OrderDetail(
            ref,
            {"exam": "synthetic"},
            report_refs=(OrderReportRef(ref.mrn, ref.case_no, ref.case_type, ref.sequence_no),),
            pacs_refs=(PacsStudyRef(ref.mrn, "R" + ref.sequence_no),),
        )

    def report(self, ref):
        self.calls.append(("report", ref.mrn, ref.sequence_no))
        return OrderReport(
            ref,
            {"結論": "虛構報告 <script>alert(1)</script>"},
            pdf_refs=(PdfAttachmentRef(ref.mrn, "//hfs01_TEST/REPORT/" + ref.mrn + "/exam.pdf"),),
            report_text="合成報告：generation " + str(self.generation),
            report_data_status="TEXT_AVAILABLE",
        )

    def pacs(self, ref):
        self.calls.append(("pacs", ref.mrn, ref.request_no))
        return PacsStudy(
            ref, tuple(PacsImageRef(ref.mrn, ref.request_no, "1", "2", str(i)) for i in (1, 2))
        )

    def pdf(self, ref):
        self.calls.append(("pdf", ref.mrn))
        if self.fail_pdf:
            raise ValueError("synthetic PDF failure")
        return BinaryAsset(b"%PDF-1.4\n% synthetic offline test\n", "application/pdf")

    def image(self, ref):
        self.calls.append(("image", ref.mrn, ref.request_no, ref.uid))
        # Valid 1x1 PNG; test images carry no patient information.
        import base64

        return BinaryAsset(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII="
            ),
            "image/png",
        )

    def patient_info(self, mrn):
        self.calls.append(("patient_info", mrn))
        return {"patient": {"hhisnum": mrn, "name": "測試病人"}, "surgs": []}

    def schedule(self, card, start, end, **kwargs):
        self.calls.append(("schedule", kwargs["mrn"]))
        return [
            SurgeryRecord(
                kwargs["mrn"],
                "OP1",
                "2026-09-30",
                "09:00",
                "10:00",
                "TEST",
                card,
                "測試醫師",
                "Phaco-IOL OD",
                "安排",
                {"orreqno": "S1"},
            )
        ]


def soap_record(mrn="0000001", day="2026-09-18", side="OD", scheduled="20260930", target="-0.50"):
    record = {
        "id": digest([mrn, day, side])[:24],
        "mrn": mrn,
        "name": "測試病人甲",
        "date": day,
        "case_no": "SOAP-" + day,
        "section_code": "70",
        "section": "眼科",
        "soap": "# Arrange CATA "
        + side
        + " (IOL SN60WF T"
        + target
        + ") on "
        + scheduled
        + "\n- TEL: 0900-000-001\n- Diagnosis: CATA\n- Grade: NS2\n- Anesthesia: GA\n- Plan: Discuss glasses",
        "matches": [],
    }
    record["soap_structure"] = {"assessment_plan": record["soap"], "present_sections": ["AP"]}
    return record


def literal(value):
    return {
        "userEnteredValue": {
            "numberValue" if isinstance(value, (float, int)) else "stringValue": value
        }
    }


def book_fixture():
    names = [
        "日期／報到時間",
        "醫院",
        "病歷號",
        "姓名",
        "TEL",
        "GA",
        "側別",
        "診斷",
        "Grade",
        "術式",
        "IOL",
        "IOL Target",
        "IOL Final",
        "Axis",
        "Plan",
        "心得",
        "",
        "SN",
        "CDE",
        "Energy Time",
        "Energy %",
        "屈光數據",
        "CalendarEventId",
    ]
    header = [literal(n) for n in names]
    header[7] = {
        "userEnteredValue": {"formulaValue": '="診斷"&COUNTIF(H2:H100,"CATA")'},
        "formattedValue": "診斷1",
    }

    def day_row(when, hospital):
        row = [{} for _ in names]
        row[0] = literal((date.fromisoformat(when) - date(1899, 12, 30)).days)
        row[0]["userEnteredFormat"] = {"numberFormat": {"type": "DATE", "pattern": "yyyy/m/d ddd"}}
        row[1], row[2] = literal(hospital), literal("◆ 刀日")
        return row

    def patient(name, mrn, side):
        row = [
            literal(v) if v else {}
            for v in [
                "08:15",
                "",
                mrn,
                name,
                "0900-111-111",
                "",
                side,
                "CATA",
                "NS2",
                "Phaco-IOL",
                "",
                "",
                "manual IOL",
                "manual axis",
                "manual plan",
                "manual note",
                "",
                "",
                "manual CDE",
                "",
                "",
                "",
                "event-keep",
            ]
        ]
        row[6]["dataValidation"] = {
            "condition": {
                "type": "ONE_OF_LIST",
                "values": [{"userEnteredValue": v} for v in ["OD", "OS", "OU"]],
            },
            "strict": True,
            "showCustomUi": True,
        }
        row[9]["dataValidation"] = {
            "condition": {
                "type": "ONE_OF_LIST",
                "values": [
                    {"userEnteredValue": v}
                    for v in ["Phaco-IOL", "LenSx-Phaco-IOL", "VT", "VT+MP", "LMR"]
                ],
            },
            "showCustomUi": True,
        }
        row[15]["note"] = "manual cell note"
        return row

    return {
        "properties": {"timeZone": "Asia/Taipei"},
        "developerMetadata": [],
        "sheets": [
            {
                "properties": {
                    "sheetId": 101,
                    "title": "202609",
                    "gridProperties": {"rowCount": 100, "columnCount": 23},
                },
                "rows": [
                    header,
                    day_row("2026-09-30", "聯醫"),
                    patient("測試聯醫", 1, "OD"),
                    [],
                    day_row("2026-09-30", "高榮"),
                    patient("測試病人甲", 1, "OD"),
                    [],
                    day_row("2026-09-29", "高榮"),
                    patient("另一病人", 2, "OS"),
                ],
                "merges": [],
            }
        ],
    }


class MemorySheets:
    key = "synthetic-sheet-00000000000001"

    def __init__(self, book=None):
        self.book = book or book_fixture()
        self.writes = 0
        self.timeout_after_commit = False
        self.timeout_before_commit = False

    def close(self):
        pass

    def read(self):
        return copy.deepcopy(self.book)

    def write(self, requests):
        self.writes += 1
        if self.timeout_before_commit:
            raise SheetUncertain("synthetic timeout")
        book = copy.deepcopy(self.book)

        def sheet(sid):
            return next(s for s in book["sheets"] if s["properties"]["sheetId"] == sid)

        def extend(s, index):
            while len(s["rows"]) <= index:
                s["rows"].append([])

        for request in requests:
            if "addSheet" in request:
                p = copy.deepcopy(request["addSheet"]["properties"])
                if any(
                    s["properties"]["sheetId"] == p["sheetId"]
                    or s["properties"]["title"] == p["title"]
                    for s in book["sheets"]
                ):
                    raise SheetError("duplicate sheet")
                book["sheets"].append({"properties": p, "rows": [], "merges": []})
            elif "createDeveloperMetadata" in request:
                m = request["createDeveloperMetadata"]["developerMetadata"]
                if any(v["metadataId"] == m["metadataId"] for v in book["developerMetadata"]):
                    raise SheetError("duplicate metadata")
                book["developerMetadata"].append(copy.deepcopy(m))
            elif "appendDimension" in request:
                p = request["appendDimension"]
                s = sheet(p["sheetId"])
                s["properties"]["gridProperties"][
                    "rowCount" if p["dimension"] == "ROWS" else "columnCount"
                ] += p["length"]
            elif "insertDimension" in request or "deleteDimension" in request:
                adding = "insertDimension" in request
                p = request["insertDimension" if adding else "deleteDimension"]["range"]
                s = sheet(p["sheetId"])
                start, end = p["startIndex"], p["endIndex"]
                extend(s, start)
                if adding:
                    s["rows"][start:start] = [[] for _ in range(end - start)]
                else:
                    del s["rows"][start:end]
                s["properties"]["gridProperties"]["rowCount"] += (end - start) * (
                    1 if adding else -1
                )
            elif "updateCells" in request:
                p = request["updateCells"]
                r = p["range"]
                s = sheet(r["sheetId"])
                for index, row in enumerate(p.get("rows", []), r["startRowIndex"]):
                    extend(s, index)
                    for col, value in enumerate(row["values"], r.get("startColumnIndex", 0)):
                        while len(s["rows"][index]) <= col:
                            s["rows"][index].append({})
                        cell = s["rows"][index][col]
                        for field in p["fields"].split(","):
                            if field in value:
                                cell[field] = copy.deepcopy(value[field])
                            else:
                                cell.pop(field, None)
                        if "userEnteredValue" in p["fields"]:
                            cell.pop("formattedValue", None)
            elif "cutPaste" in request:
                p = request["cutPaste"]
                src, dst = p["source"], p["destination"]
                a, b = sheet(src["sheetId"]), sheet(dst["sheetId"])
                extend(a, src["startRowIndex"])
                extend(b, dst["rowIndex"])
                row = copy.deepcopy(a["rows"][src["startRowIndex"]])
                a["rows"][src["startRowIndex"]] = []
                b["rows"][dst["rowIndex"]] = row
            else:
                raise AssertionError("Unsupported test request " + str(request))
        self.book = book
        if self.timeout_after_commit:
            raise SheetUncertain("synthetic response lost")


def proposal(**changes):
    fields = {key: "" for key in HEADERS if key != "hospital"}
    fields.update(
        date="2026-09-30",
        mrn="0000001",
        name="測試病人甲",
        side="OD",
        procedure="Phaco-IOL",
        iol="SN60WF",
        target="-0.50",
        tel="0900-000-001",
    )
    fields.update(changes)
    return {
        "id": digest(fields)[:24],
        "fields": fields,
        "selected_fields": ["name", "tel", "iol", "target"],
        "source_records": [],
    }
