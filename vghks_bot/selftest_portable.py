"""Offline verification of relocation without Windows credential services."""
import json
import shutil

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .bot_store import Registry
from .google_sheets import GoogleSettings


def synthetic_google_key():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return {"type": "service_account", "private_key": pem,
            "client_email": "synthetic@example.iam.gserviceaccount.com",
            "token_uri": "https://oauth2.googleapis.com/token"}


def check_portable_credentials(directory):
    source, copied = directory / "portable-source", directory / "portable-copy"
    source.mkdir()
    copied.mkdir()
    registry = Registry(source / "accounts.sqlite3")
    account = registry.save("TEST", "合成帳號", "高榮", "synthetic-portable-password", True)
    google = GoogleSettings(source)
    sheet_id = "synthetic-portable-sheet-000000000000"
    google.save({"spreadsheet_id": sheet_id, "key": synthetic_google_key()})
    original_client = google.client()
    try:
        signature = original_client.session.credentials.sign_bytes(b"portable")
    finally:
        original_client.close()
    # Only the databases travel; no DPAPI file, JSON key or process state.
    for name in ("accounts.sqlite3", "clinical.sqlite3"):
        shutil.copy2(source / name, copied / name)
    registry = Registry(copied / "accounts.sqlite3")
    assert registry.password(account["id"]) == "synthetic-portable-password"
    google = GoogleSettings(copied)
    client = google.client()
    try:
        assert client.key == sheet_id
        assert client.session.credentials.sign_bytes(b"portable") == signature
    finally:
        client.close()
    assert "private_key" not in json.dumps(google.public())
    assert b"synthetic-portable-password" not in (copied / "accounts.sqlite3").read_bytes()
    registry.forget(account["id"])
    google.remove_key()
    assert not Registry(copied / "accounts.sqlite3").password(account["id"])
    assert not GoogleSettings(copied).public()["configured"]
