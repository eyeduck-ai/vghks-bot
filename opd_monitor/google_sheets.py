"""Portable service-account credentials; Sheets REST via official google-auth."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from urllib.parse import quote

import requests
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from .database_format import schema
from .portable_credentials import seal, unseal
from .storage import StorageError

DEFAULT_SHEET = ""
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SheetError(Exception):
    pass


class SheetUncertain(SheetError):
    pass


def spreadsheet_id(value):
    if not isinstance(value, str):
        raise ValueError("刀表網址不正確。")
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", value)
    result = match.group(1) if match else value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,180}", result):
        raise ValueError("請輸入 Google 試算表網址或代碼。")
    return result


class GoogleSettings:
    def __init__(self, directory):
        self.directory = directory
        self.path = directory / "clinical.sqlite3"
        self.lock = threading.RLock()
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS bot_google_config (
                id INTEGER PRIMARY KEY CHECK(id=1), spreadsheet_id TEXT NOT NULL,
                email TEXT NOT NULL, secret BLOB)""")
            db.execute("INSERT OR IGNORE INTO bot_google_config VALUES(1,?,'',NULL)", (DEFAULT_SHEET,))

    @contextmanager
    def connect(self):
        with self.lock:
            db = None
            try:
                db = sqlite3.connect(self.path, timeout=30)
                db.row_factory = sqlite3.Row
                schema(db)
                db.execute("PRAGMA synchronous=FULL")
                with db:
                    yield db
            except sqlite3.Error as exc:
                raise StorageError("無法讀寫 Google 連線設定。") from exc
            finally:
                if db is not None:
                    db.close()

    @staticmethod
    def validate_key(raw):
        if not isinstance(raw, dict) or raw.get("type") != "service_account":
            raise ValueError("請匯入 Google 服務帳戶 JSON 金鑰。")
        if raw.get("token_uri") != "https://oauth2.googleapis.com/token":
            raise ValueError("服務帳戶 token_uri 必須是 Google 官方位址。")
        try:
            return service_account.Credentials.from_service_account_info(raw, scopes=SCOPES)
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("服務帳戶金鑰格式不正確。") from exc

    def public(self):
        with self.connect() as db:
            row = db.execute("SELECT spreadsheet_id,email,secret FROM bot_google_config WHERE id=1").fetchone()
            return {"spreadsheet_id": row[0], "email": row[1], "configured": row[2] is not None}

    def save(self, values):
        with self.lock:
            config = self.public()
            config["spreadsheet_id"] = spreadsheet_id(values.get("spreadsheet_id", config["spreadsheet_id"]))
            supplied = values.get("key")
            if supplied:
                credential = self.validate_key(supplied)
                config["email"] = credential.service_account_email
            with self.connect() as db:
                if supplied:
                    protected = seal(db, json.dumps(supplied).encode("utf-8"))
                    db.execute("UPDATE bot_google_config SET secret=? WHERE id=1", (protected,))
                db.execute("UPDATE bot_google_config SET spreadsheet_id=?,email=? WHERE id=1",
                           (config["spreadsheet_id"], config["email"]))
            return self.public()

    def remove_key(self):
        with self.connect() as db:
            db.execute("UPDATE bot_google_config SET secret=NULL WHERE id=1")
        return self.public()

    def client(self):
        with self.connect() as db:
            row = db.execute("SELECT spreadsheet_id,secret FROM bot_google_config WHERE id=1").fetchone()
            if row[1] is None:
                raise ValueError("請先匯入服務帳戶金鑰，並將刀表分享給顯示的服務帳戶電子郵件。")
            raw = json.loads(unseal(db, row[1]))
            credentials = self.validate_key(raw)
            return SheetsClient(row[0], AuthorizedSession(credentials))


class SheetsClient:
    def __init__(self, key, session):
        self.key, self.session = key, session
        self.base = "https://sheets.googleapis.com/v4/spreadsheets/" + quote(key, safe="")

    def close(self):
        self.session.close()

    def request(self, method, suffix="", **kwargs):
        try:
            response = self.session.request(method, self.base + suffix, timeout=(10, 55), **kwargs)
        except requests.RequestException as exc:
            if method == "POST":
                raise SheetUncertain("寫入回應中斷，請按「核對更新結果」；請勿另建相同更新。") from exc
            raise SheetError("無法連線 Google Sheets，請確認網路。") from exc
        except GoogleAuthError as exc:
            raise SheetError("服務帳戶授權失敗，請檢查金鑰、系統時間與網路。") from exc
        if response.status_code in {401, 403}:
            raise SheetError("Google 授權失敗：確認 Sheets API 已啟用，且服務帳戶有刀表編輯權限。")
        if response.status_code == 404:
            raise SheetError("找不到刀表，請確認網址與服務帳戶分享權限。")
        if response.status_code >= 500 or response.status_code == 429:
            if method == "POST":
                raise SheetUncertain("Google 尚未確認寫入結果，請先核對更新結果。")
            raise SheetError("Google 暫時無法完成請求，稍後可重試。")
        if not response.ok:
            raise SheetError("Google 拒絕此更新；請重新讀取刀表、檢查欄位或保護範圍。")
        try:
            return response.json()
        except ValueError as exc:
            if method == "POST":
                raise SheetUncertain("無法辨識寫入回應，請核對更新結果。") from exc
            raise SheetError("無法辨識 Google 回應。") from exc

    def connection_info(self):
        meta = self.request("GET", params={"fields": "spreadsheetId,properties(title),sheets(properties(title))"})
        return {"ok": True, "title": meta.get("properties", {}).get("title", ""),
                "months": [s["properties"]["title"] for s in meta.get("sheets", [])
                           if re.fullmatch(r"\d{6}", s["properties"]["title"])],
                "message": "讀取連線成功；寫入仍取決於編輯者權限與儲存格保護設定。"}

    def read(self):
        meta = self.request("GET", params={"fields": "spreadsheetId,properties,sheets(properties),developerMetadata"})
        # The clinical schedule lives in month tabs. Do not fetch FU/IVI or unrelated tabs.
        sheets = [s for s in meta.get("sheets", []) if re.fullmatch(r"\d{6}", s["properties"]["title"])]
        for sheet in sheets:
            p = sheet["properties"]
            columns = p["gridProperties"]["columnCount"]
            if columns > 1000:
                raise SheetError("月分頁超過 1,000 欄，請確認選擇的是手術刀表。")
            last = column_name(columns - 1)
            row_count = p["gridProperties"]["rowCount"]
            data = []
            # Bounded reads preserve typed values, formulas, validation and formatting.
            for start in range(1, row_count + 1, 400):
                result = self.request("GET", params=[
                    ("ranges", f"'{p['title']}'!A{start}:{last}{min(start + 399, row_count)}"),
                    ("fields", "sheets(data(startRow,startColumn,columnMetadata(pixelSize),rowData(values(userEnteredValue,formattedValue,userEnteredFormat,dataValidation,note,textFormatRuns))),merges)")])
                part = (result.get("sheets") or [{}])[0]
                data.extend(part.get("data", []))
                sheet["merges"] = part.get("merges", [])
            rows = []
            for block in data:
                for index, row in enumerate(block.get("rowData", []), block.get("startRow", 0)):
                    while len(rows) <= index:
                        rows.append([])
                    rows[index] = row.get("values", [])
            sheet["rows"] = rows
            sheet["column_widths"] = next((b["columnMetadata"] for b in data if b.get("columnMetadata")), [])
        return {"properties": meta.get("properties", {}), "sheets": sheets,
                "developerMetadata": meta.get("developerMetadata", [])}

    def write(self, requests_):
        return self.request("POST", ":batchUpdate", json={"requests": requests_, "includeSpreadsheetInResponse": False})


def column_name(index):
    value = ""
    while index >= 0:
        index, rem = divmod(index, 26)
        value = chr(65 + rem) + value
        index -= 1
    return value
