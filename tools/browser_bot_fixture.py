"""Launch the real UI with synthetic SDK data, never hospital services."""
import argparse
import hashlib
import json
import struct
import sys
import tempfile
import time
import zlib
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vghks_bot.bot_server import BotHandler, BotServer  # noqa: E402
from vghks_bot.databases import DatabaseManager  # noqa: E402
from vghks_bot.selftest_bot import BotSyntheticSDK  # noqa: E402
from vghks_bot.selftest_soap import synthetic_soap  # noqa: E402
from vghks_bot.settings import Settings, timestamp, today  # noqa: E402
from vghks_bot.soap_data import snapshot as soap_snapshot  # noqa: E402
from vghks_bot.surgery_schedule import EBOARD_URL  # noqa: E402


class LocalBoardHandler(BotHandler):
    """Explicit synthetic-only board target; never redirects to the hospital."""
    def local_request(self):
        if self.command == "GET" and self.path == "/" and self.headers.get("Host") == self.server.origin.removeprefix("http://"):
            return True
        return super().local_request()

    def reply(self, status, payload, *, mime="application/json; charset=utf-8", cookie=None, **options):
        if self.path == "/" and isinstance(payload, bytes) and mime.startswith("text/html"):
            payload = payload.replace(EBOARD_URL.encode(), (self.server.origin+"/synthetic-board").encode())
        super().reply(status, payload, mime=mime, cookie=cookie, **options)

    def do_GET(self):
        if self.path == "/synthetic-board":
            if self.local_request():
                self.reply(200, "<!doctype html><html lang=zh-Hant><meta charset=utf-8><title>合成刀房看板</title><h1>合成刀房看板</h1><p>僅供本機介面測試，沒有連線院內。</p></html>".encode(), mime="text/html; charset=utf-8")
        else:
            super().do_GET()


class SyntheticSheetHandler(BotHandler):
    def local_request(self):
        # Only the anonymous static entry page permits automation-origin navigation.
        # Authenticated data routes and every mutation retain the production checks.
        if self.command == "GET" and self.path == "/" and self.headers.get("Host") == self.server.origin.removeprefix("http://"):
            return True
        return super().local_request()


class FollowUpSDK(BotSyntheticSDK):
    def _review_rows(self):
        return [replace(row, application_status="Y", processing_status="F",
            fields={**row.fields, "ApplyStatus": "Y", "ApplyFinishFlag": "F"}) for row in super()._review_rows()]

    def review_cases(self, filters):
        time.sleep(.5)
        return super().review_cases(filters)

    def soap(self, case):
        time.sleep(.8)
        return super().soap(case)

    def review_case(self, ref):
        time.sleep(.6)
        row = super().review_case(ref)
        code = "2" if ref.apply_seq == "1003" else "1"
        return replace(row, verify_code=code, fields={**row.fields, "VerifyCode": code})

    def earnings_report(self, context, period):
        time.sleep(.5)
        return super().earnings_report(context, period)

    def surgery_schedule(self, card, start, end, **filters):
        time.sleep(.8)
        return super().surgery_schedule(card, start, end, **filters)


class DemographicsSDK(FollowUpSDK):
    def demographics(self, mrn):
        return replace(super().demographics(mrn), birthday="047/01/01" if self.card == "TEST" else "0700101")


class RegistrationsSDK(DemographicsSDK):
    def registrations(self, mrn):
        from vghks_sdk.parsing.webmaas import parse_registration_records

        from vghks_bot.selftest_registrations import registration_page

        self.calls.append((self.card, "registrations", mrn))
        return parse_registration_records(registration_page(mrn), mrn)


class SessionSDK(DemographicsSDK):
    expired = set()

    def demographics(self, mrn):
        from vghks_sdk import (
            AuthExpiredError,
            AuthorizationError,
            LoginRejectedError,
            NotFoundError,
        )

        self.calls.append((self.card, "lookup", mrn))
        key = self.card, mrn
        if mrn == "00011111" and key not in self.expired:
            self.expired.add(key)
            raise AuthExpiredError("synthetic expired session")
        if mrn == "00000000":
            raise NotFoundError("synthetic missing patient", code="WEBMAAS_PATIENT_NOT_FOUND")
        if mrn == "00022222":
            raise AuthorizationError("synthetic denied access", code="DEMOGRAPHICS_PERMISSION_DENIED")
        if mrn == "00033333":
            raise LoginRejectedError("synthetic rejected credentials")
        return super().demographics(mrn)


def synthetic_pdf(label, *, two_pages=False):
    content = f"BT /F1 18 Tf 50 770 Td ({label}) Tj ET\n".encode("ascii")
    if two_pages:
        content += b"BT /F1 16 Tf 50 730 Td (PAGE 1 OF 2 - SYNTHETIC ONLY) Tj ET\n"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content)} >>\nstream\n".encode()+content+b"endstream"]
    if two_pages:
        objects[1] = b"<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>"
        second = (b"0.85 0.93 1 rg 50 90 495 580 re f 0 0 0 rg\n"
                  + f"BT /F1 18 Tf 50 770 Td ({label}) Tj ET\n".encode("ascii")
                  + b"BT /F1 16 Tf 50 730 Td (PAGE 2 OF 2 - SYNTHETIC ONLY) Tj ET\n")
        objects.extend([
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 7 0 R >>",
            f"<< /Length {len(second)} >>\nstream\n".encode()+second+b"endstream"])
    pdf = b"%PDF-1.4\n"
    offsets = []
    for index, value in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += f"{index} 0 obj\n".encode()+value+b"\nendobj\n"
    start = len(pdf)
    pdf += f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode()
    pdf += b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets)
    return pdf+f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()


def synthetic_png(number):
    """Create a valid, labelled test grid using only the Python standard library."""
    width, height = 900, 600
    pixels = bytearray(bytes((245, 248, 250)) * width * height)
    accent = ((32, 96, 155), (36, 124, 92), (179, 100, 31))[(number - 1) % 3]

    def rectangle(x, y, wide, tall, color):
        row = bytes(color) * wide
        for line in range(y, y + tall):
            start = (line * width + x) * 3
            pixels[start:start + wide * 3] = row

    glyphs = {
        "0": (14, 17, 19, 21, 25, 17, 14), "1": (4, 12, 4, 4, 4, 4, 14),
        "2": (14, 17, 1, 2, 4, 8, 31), "3": (30, 1, 1, 14, 1, 1, 30),
        "S": (15, 16, 16, 14, 1, 1, 30), "Y": (17, 17, 10, 4, 4, 4, 4),
        "N": (17, 25, 25, 21, 19, 19, 17), "T": (31, 4, 4, 4, 4, 4, 4),
        "H": (17, 17, 17, 31, 17, 17, 17), "E": (31, 16, 16, 30, 16, 16, 31),
        "I": (14, 4, 4, 4, 4, 4, 14), "C": (14, 17, 16, 16, 16, 17, 14),
    }

    def label(text, x, y, scale, color):
        for index, letter in enumerate(text):
            for line, bits in enumerate(glyphs.get(letter, ())):
                for column in range(5):
                    if bits & (1 << (4 - column)):
                        rectangle(x + (index * 6 + column) * scale, y + line * scale,
                                  scale, scale, color)

    rectangle(0, 0, width, 98, accent)
    label(f"SYNTHETIC 0{number}", 30, 25, 7, (255, 255, 255))
    for row in range(5):
        for column in range(9):
            shade = (row + column + number) % 4
            color = tuple(min(255, channel + shade * 24) for channel in accent)
            rectangle(34 + column * 94, 132 + row * 78, 80, 64, color)
            rectangle(66 + column * 94, 148 + row * 78, 16, 32, (255, 255, 255))
            if (row + column) % 2:
                rectangle(58 + column * 94, 156 + row * 78, 32, 16, (255, 255, 255))
    label(f"0{number}", 386, 538, 7, accent)

    def chunk(kind, value):
        return (struct.pack(">I", len(value)) + kind + value
                + struct.pack(">I", zlib.crc32(kind + value)))

    raw = b"".join(b"\0" + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"tEXt", b"Description\0Synthetic gallery test grid; not clinical data")
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def seed_ophthalmic(workspace, mrn="TEST001", *, report_gallery=False):
    from vghks_sdk.models import BinaryAsset, ClinicalOrder

    from vghks_bot.analysis_fetch import clean
    from vghks_bot.selftest_exports import seed_report

    seed_report(workspace, mrn)
    store = workspace.analysis.store
    store.save_step(mrn, "numeric-history", "numeric", {"mrn": mrn, "tables": [
        {"title": "Va", "headers": ["日期", "OD", "OS"], "rows": [["2026-09-11", "0.6", "HM"]]},
        {"title": "驗光-散瞳前", "headers": ["日期", "OD", "OS"], "rows": [
            ["2026-09-11", "2.25 1.00 X 90", "1.25 -0.50 X 175"],
            ["2026-08-14", "-1.25 -1.00 X 170", "0.00 -0.25 X 0"]]},
        {"title": "KM", "headers": ["日期", "OD", "OS"], "rows": [["2026-09-11",
            "K1 41.25 8.20 X 160 K2 42.50 7.96 X 70 CYL -1.25 X 160",
            "K1 41.50 8.14 X 45 K2 42.25 7.98 X 135 CYL 0.75 X 45"]]},
        {"title": "配鏡", "headers": ["日期", "側別", "SPH (D)", "CYL (D)", "SE (D)"],
         "rows": [["2026-07-15", "OD", "1.50", "0.50", "1.75"]]},
    ]}, workspace.username)
    orders = [ClinicalOrder(mrn, f"SYN-{index}", "O", "DBR, free charge", day, day)
              for index, day in enumerate(("2026-09-11", "2026-08-14", "2026-07-15"), 1)]
    for kind in ("*", "OR"):
        store.save_step(mrn, "orders-history:"+kind, "order_index", clean(orders), workspace.username)
    for row in workspace.review.history._public_orders(workspace.review.history._groups(mrn, clean(orders))):
        count = 2 if row["date"] == "2026-09-11" else 1
        if report_gallery and row["date"] == "2026-09-11":
            samples = [(synthetic_png(1), "image/png"),
                       (synthetic_pdf("SYNTHETIC GALLERY - FILE 2 - NOT CLINICAL DATA", two_pages=True), "application/pdf"),
                       (synthetic_png(2), "image/png"), (synthetic_png(3), "image/png")]
            assets = [store.save_asset(mrn, f"history-asset:synthetic-gallery:{row['id']}-{index}",
                BinaryAsset(content, mime), workspace.username)
                for index, (content, mime) in enumerate(samples, 1)]
        else:
            assets = [store.save_asset(mrn, f"history-asset:download_pdf:synthetic-{row['id']}-{index}",
                BinaryAsset(synthetic_pdf(f"Synthetic exam {row['date']} - file {index}"), "application/pdf"),
                workspace.username) for index in range(1, count+1)]
        store.save_step(mrn, "history-order:"+row["id"], "history_order", {
            "order": row, "assets": assets, "texts": [], "details": [], "issues": [], "status": "complete"}, workspace.username)


def seed_large_review(workspace, count):
    """A complete task with one SOAP per patient, created without SDK queries."""
    from vghks_bot.tags import classification

    task_id = "synthetic-review-" + workspace.username.lower()
    day, observed = (today() - timedelta(days=1)).isoformat(), timestamp()
    members = [{"mrn": f"PAGE{i:06}", "name": f"合成病人{i:06}", "sex": "女", "age": "68",
        "registrations": [{"mrn": f"PAGE{i:06}", "visit_date": day, "section_code": "70",
            "room": "02", "sequence_no": str(i)}]} for i in range(1, count + 1)]
    with workspace.store.library.batch():
        group = workspace.review.db.save("set", {"id": task_id + "-set", "name": f"合成大型檢閱 {count} 位",
            "members": members})
        workspace.review.db.save("task", {"id": task_id, "kind": "review", "name": group["name"],
            "set_id": group["id"], "members": members, "mode": "latest", "cutoff": today().isoformat(),
            "department_keywords": ["眼科"], "status": "completed", "created_at": observed,
            "finished_at": observed, "force": False, "message": "合成資料已保存；未連線院內"})
        for i, member in enumerate(members, 1):
            key = hashlib.sha256(f"{workspace.username}:{member['mrn']}:{day}".encode()).hexdigest()[:24]
            record = {"id": key, "mrn": member["mrn"], "name": member["name"], "date": day,
                "section": "眼科", "section_code": "70", "doctor": "合成醫師", "case_no": f"SYN-{i}",
                "soap": f"S: SYNTHETIC ONLY {i}\nO: Va 0.4 / 0.6\nA: 合成眼科紀錄\nP: # FU\n"
                    + ("synthetic local review data " * 300) + f"\n來源帳號 {workspace.username}",
                "updated_at": observed}
            workspace.store.library.save_record(record, workspace.username, task_id)
            record.update(classification(record, workspace.settings))
            workspace.review.db.item(task_id, member["mrn"], {**member, "status": "reviewed", "records": [record],
                "checked_at": observed, "index_checked_at": observed, "message": "已取得"})
            if i % 20 == 1:
                workspace.review.db.save("review_note", {"id": task_id + ":" + member["mrn"],
                    "text": f"合成備註 {i}"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--followup", action="store_true")
    parser.add_argument("--library", action="store_true")
    parser.add_argument("--local-board", action="store_true")
    parser.add_argument("--ophthalmic", action="store_true", help="Seed signed refraction and dated PDF comparison samples")
    parser.add_argument("--report-gallery", action="store_true", help="Seed ophthalmic reports with three synthetic PNG grids and one PDF in the first report")
    parser.add_argument("--demographics", action="store_true", help="Synthetic ROC birthdays and a numeric manual MRN")
    parser.add_argument("--lookup-failures", action="store_true", help="Synthetic expired, missing, forbidden and rejected patient queries")
    parser.add_argument("--registrations", action="store_true", help="Complete registration fields, explicit and unknown attendance")
    parser.add_argument("--sheet-management", action="store_true", help="Synthetic online sheet statistics; no Google calls")
    parser.add_argument("--review-patients", type=int, default=0, help="Seed a completed synthetic review with 1-20000 patients")
    args = parser.parse_args()
    if not 0 <= args.review_patients <= 20000:
        parser.error("--review-patients must be between 0 and 20000")
    with tempfile.TemporaryDirectory(prefix="vghks-bot-synthetic-") as directory:
        sdk = SessionSDK if args.lookup_failures else RegistrationsSDK if args.registrations else DemographicsSDK if args.demographics else FollowUpSDK if args.followup else BotSyntheticSDK
        manager = DatabaseManager(Path(directory), Settings(), sdk)
        app = manager.current
        try:
            for user, label in (("SECOND", "合成帳號乙"), ("TEST", "合成帳號甲")):
                key = app.login({"username": user, "password": "synthetic", "label": label})["account"]["id"]
                if args.review_patients:
                    seed_large_review(app.workspace(key), args.review_patients)
                if args.sheet_management:
                    sys.path.insert(0, str(ROOT / "tests"))
                    from analysis_fixtures import MemorySheets

                    workspace = app.workspace(key)
                    book = json.loads((ROOT / "tests/fixtures/gas_archives.json").read_text(encoding="utf-8"))["before"]
                    client = MemorySheets(book)
                    client.read_all = client.read
                    client.read_for_archive = client.read
                    client.read_metadata = lambda client=client: {"developerMetadata": client.read().get("developerMetadata", [])}
                    workspace.sheets.client_factory = lambda client=client: client
                    workspace.sheets.google.public = lambda client=client: {"spreadsheet_id": client.key, "configured": True}
                if args.library:
                    workspace = app.workspace(key)
                    for index, text in enumerate((
                        "S: 視力模糊\nO: Va 0.4 / 0.6\nA: Cataract OD，左眼穩定\nP: # Arrange CATA OD (IOL +21.0 T-0.50)\n門診追蹤",
                        "S: 主訴\nA+P:\n" + "視網膜穩定，依原計畫追蹤。" * 60,
                        "S: 主訴\nAssessment: 穩定 OS\nPlan: 下次回診\n# APPLY synthetic",
                        "沒有分欄標題的合成病歷，不猜測其段落。",
                        "S: 合成主訴\nA: <script>不會執行</script>\nP: 保留全文與原始字串",
                    )):
                        record = {"id": f"library-{index}", "mrn": f"TEST00{index+1}",
                            "name": f"合成病人{index+1}", "date": (today()-timedelta(days=index+1)).isoformat(),
                            "section": "眼科", "section_code": "70", "soap": text+f"\n來源帳號 {user}"}
                        if index == 0:
                            workspace.store.library.save_record({**record, "soap": "A: 合成舊版\nP: 原追蹤計畫"}, user, "fixture-library")
                            from vghks_sdk import VisitCase

                            case = VisitCase(record["mrn"], today()-timedelta(days=1), "O", "SYNTHETIC", "70", "眼科")
                            record.update(soap_snapshot(synthetic_soap(case, user)))
                        workspace.store.library.save_record(record, user, "fixture-library")
                if args.ophthalmic or args.report_gallery:
                    seed_ophthalmic(app.workspace(key), report_gallery=args.report_gallery)
                    if args.demographics:
                        seed_ophthalmic(app.workspace(key), "00012345", report_gallery=args.report_gallery)
            with BotServer(0, app, manager) as server:
                if args.sheet_management:
                    server.RequestHandlerClass = SyntheticSheetHandler
                if args.local_board:
                    server.RequestHandlerClass = LocalBoardHandler
                print(f"{server.origin}/#{app.launch_token}", flush=True)
                server.serve_forever(poll_interval=.1)
        finally:
            manager.close()
            output = ROOT / ".build" / "browser-sdk-calls.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(BotSyntheticSDK.calls), encoding="utf-8")


if __name__ == "__main__":
    main()
