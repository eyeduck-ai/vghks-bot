"""One explicit data format, independent of the executable's version."""
import re
import uuid

from .settings import timestamp
from .storage import StorageError, atomic_json, read_json

FORMAT = 1
MANIFEST = "database.json"


def schema(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == 0 and not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone():
        db.execute(f"PRAGMA user_version={FORMAT}")
    elif version != FORMAT:
        raise StorageError(f"資料庫格式 {version} 不受此程式支援（支援格式 {FORMAT}）；不會修改或轉換。")


def manifest(directory, *, create=False, name="我的資料庫"):
    path = directory / MANIFEST
    if not path.exists():
        if not create or any(directory.iterdir()):
            raise StorageError("不是目前支援的資料庫目錄，請建立新的資料庫；本版不轉換舊格式。")
        value = {"format": FORMAT, "id": uuid.uuid4().hex, "name": name, "created_at": timestamp()}
        atomic_json(path, value)
        return value
    value = read_json(path)
    if not isinstance(value, dict) or type(value.get("format")) is not int or value["format"] != FORMAT:
        raise StorageError(f"資料庫格式不相容，僅支援格式 {FORMAT}；來源不會被修改。")
    if not re.fullmatch(r"[a-f0-9]{32}", str(value.get("id", ""))) or not isinstance(value.get("name"), str):
        raise StorageError("資料庫識別資訊不完整。")
    return value
