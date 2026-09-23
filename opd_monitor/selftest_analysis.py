"""Offline frozen-binary checks, with disposable keys and synthetic surgery rows."""
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .google_sheets import GoogleSettings
from .sheet_plan import HEADERS, Planner


def check_google_and_sheet(directory):
    # An ephemeral, locally generated key checks the actual bundled signer.
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    google = GoogleSettings(directory)
    google.save({"spreadsheet_id": "synthetic-selftest-sheet-00000000", "key": {"type": "service_account", "private_key": pem,
                         "client_email": "synthetic@example.iam.gserviceaccount.com",
                         "token_uri": "https://oauth2.googleapis.com/token"}})
    client = google.client()
    try:
        if not client.session.credentials.sign_bytes(b"synthetic"):
            raise RuntimeError("Google signer failed")
    finally:
        client.close()
        google.remove_key()
    header = [{"userEnteredValue": {"stringValue": value}} for value in HEADERS.values()]
    book = {"sheets": [{"properties": {"sheetId": 1, "title": "202609",
            "gridProperties": {"rowCount": 100, "columnCount": len(header)}},
            "rows": [header], "merges": []}], "developerMetadata": []}
    fields = {"date": "2026-10-09", "mrn": "0000001", "name": "測試病人", "side": "OD",
              "procedure": "Phaco-IOL", "iol": "SN60WF", "target": "-0.50"}
    preview = Planner(book, "synthetic-sheet", [{"id": "self-test", "fields": fields}]).build()
    if not preview["can_apply"] or preview["items"][0]["action"] != "insert":
        raise RuntimeError("sheet planner failed")
