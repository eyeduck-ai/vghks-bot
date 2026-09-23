"""Local UI test harness. Uses only synthetic SDK data, never the intranet."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opd_monitor.selftest import SyntheticSDK  # noqa: E402
from opd_monitor.server import Application, LocalServer  # noqa: E402
from opd_monitor.settings import Settings  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8765)
parser.add_argument("--data-dir", type=Path, required=True)
args = parser.parse_args()
app = Application(Settings(), args.data_dir, SyntheticSDK)
try:
    with LocalServer(args.port, app) as server:
        print(f"{server.origin}/#{app.launch_token}", flush=True)
        server.serve_forever(poll_interval=.1)
finally:
    app.close()
