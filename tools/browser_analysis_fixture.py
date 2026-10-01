"""UI verification only: synthetic patients, fake SDK, in-memory copy of the knife sheet."""

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from analysis_fixtures import ExamSDK, MemorySheets, soap_record  # noqa: E402
from vghks_sdk import OutpatientPatient  # noqa: E402

from vghks_bot.jobs import Application  # noqa: E402
from vghks_bot.server import LocalServer  # noqa: E402
from vghks_bot.settings import Settings  # noqa: E402


class BrowserSDK(ExamSDK):
    def __init__(self, settings):
        super().__init__(settings)
        self.opd = SimpleNamespace(get_doctor_patients=self.patients)

    def patients(self, account, day):
        return [OutpatientPatient(str(i).zfill(7), "測試病人 " + str(i), day,
                                  "女", "70", "70", "02", account, True) for i in range(1, 4)]


class BrowserSheets(MemorySheets):
    def connection_info(self):
        return {"ok": True, "title": "合成刀表（離線測試）", "months": ["202609"],
                "message": "讀取連線成功；寫入仍取決於編輯者權限與儲存格保護設定。"}


parser = argparse.ArgumentParser()
parser.add_argument("--data-dir", type=Path, required=True)
parser.add_argument("--port", type=int, default=8766)
args = parser.parse_args()
app = Application(Settings(), args.data_dir, BrowserSDK)
sheet = BrowserSheets()
app.analysis.client_factory = lambda: sheet
try:
    key = next(iter(app.accounts))
    app.save_accounts({"accounts": [{"id": key, "username": "TEST", "password": "synthetic-only"}]})
    for i in range(1, 46):
        record = soap_record(str(i).zfill(7))
        record["name"] = "測試病人 " + str(i).zfill(2)
        app.store.library.save_record(record, "TEST", "synthetic-ui")
    cohort = app.analysis.save_cohort(
        {
            "source": "library",
            "name": "眼科分析驗證",
            "record_ids": [soap_record()["id"], soap_record("0000002")["id"]],
        }
    )
    app.analysis.start({"cohort_id": cohort["id"], "modules": ["retina", "cataract", "surgery"]})
    if not app.idle.wait(15):
        raise RuntimeError("fixture not ready")
    with LocalServer(args.port, app) as server:
        print(server.origin + "/#" + app.launch_token, flush=True)
        server.serve_forever(poll_interval=0.1)
finally:
    app.close()
