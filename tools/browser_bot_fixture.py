"""Launch the real UI with synthetic SDK data, never hospital services."""
import argparse
import json
import sys
import tempfile
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vghks_bot.bot_server import BotHandler, BotServer  # noqa: E402
from vghks_bot.databases import DatabaseManager  # noqa: E402
from vghks_bot.selftest_bot import BotSyntheticSDK  # noqa: E402
from vghks_bot.selftest_soap import synthetic_soap  # noqa: E402
from vghks_bot.settings import Settings, today  # noqa: E402
from vghks_bot.soap_data import snapshot as soap_snapshot  # noqa: E402
from vghks_bot.surgery_schedule import EBOARD_URL  # noqa: E402


class LocalBoardHandler(BotHandler):
    """Explicit synthetic-only board target; never redirects to the hospital."""
    def reply(self, status, payload, *, mime="application/json; charset=utf-8", cookie=None):
        if self.path == "/" and isinstance(payload, bytes) and mime.startswith("text/html"):
            payload = payload.replace(EBOARD_URL.encode(), (self.server.origin+"/synthetic-board").encode())
        super().reply(status, payload, mime=mime, cookie=cookie)

    def do_GET(self):
        if self.path == "/synthetic-board":
            if self.local_request():
                self.reply(200, "<!doctype html><html lang=zh-Hant><meta charset=utf-8><title>合成刀房看板</title><h1>合成刀房看板</h1><p>僅供本機介面測試，沒有連線院內。</p></html>".encode(), mime="text/html; charset=utf-8")
        else:
            super().do_GET()


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--followup", action="store_true")
    parser.add_argument("--library", action="store_true")
    parser.add_argument("--local-board", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="vghks-bot-synthetic-") as directory:
        manager = DatabaseManager(Path(directory), Settings(), FollowUpSDK if args.followup else BotSyntheticSDK)
        app = manager.current
        try:
            for user, label in (("SECOND", "合成帳號乙"), ("TEST", "合成帳號甲")):
                key = app.login({"username": user, "password": "synthetic", "label": label})["account"]["id"]
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
            with BotServer(0, app, manager) as server:
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
