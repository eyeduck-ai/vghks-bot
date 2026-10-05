from __future__ import annotations

import json
import math
import re
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TAIPEI = timezone(timedelta(hours=8))


def today() -> date:
    return datetime.now(TAIPEI).date()


def timestamp() -> str:
    return datetime.now(TAIPEI).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class Tag:
    id: str
    name: str
    keywords: tuple[str, ...]
    parser: str = "none"
    scope: str = "all"


Category = Tag  # v2 configuration/API compatibility

FOLLOWUP_TAG = Tag("followup", "追蹤", ("# FU",), scope="all")

DEFAULT_TAGS = (
    Tag("surgery", "手術", ("# Arrange",), "surgery", "ap"),
    Tag("review", "審查", ("# APPLY",), scope="ap"),
    FOLLOWUP_TAG,
)


DEFAULT_CATEGORIES = DEFAULT_TAGS


def tags_from(values) -> tuple[Tag, ...]:
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 50:
        raise ValueError("請設定 1–50 個 tag。")
    result = []
    for value in values:
        if isinstance(value, Tag):
            value = asdict(value)
        if not isinstance(value, dict):
            raise ValueError("tag 格式不正確。")
        key = value.get("id") or uuid.uuid4().hex
        name = value.get("name", "")
        keywords = value.get("keywords", [])
        parser = value.get("parser", "none")
        scope = value.get("scope", "all")
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", key):
            raise ValueError("tag 代碼不正確。")
        if not isinstance(name, str) or not name.strip() or len(name) > 40:
            raise ValueError("tag 名稱需為 1–40 個字元。")
        if isinstance(keywords, str):
            keywords = keywords.splitlines()
        if not isinstance(keywords, (list, tuple)) or len(keywords) > 30:
            raise ValueError("每個 tag 最多 30 個關鍵字；留白表示只手動加入。")
        if any(not isinstance(k, str) or not k.strip() or len(k) > 120 or any(ord(c) < 32 for c in k) for k in keywords):
            raise ValueError("關鍵字不可空白或包含控制字元，每個最多 120 字。")
        if parser not in {"none", "surgery"}:
            raise ValueError("不支援此解析方式。")
        if not isinstance(scope, str) or scope not in {"all", "s", "o", "ap", "medications", "orders"}:
            raise ValueError("tag 辨識範圍須為全文、S、O、A+P、藥囑或醫囑。")
        result.append(Tag(key, name.strip(), tuple(dict.fromkeys(k.strip() for k in keywords)), parser, scope))
    if len({c.id for c in result}) != len(result) or len({c.name for c in result}) != len(result):
        raise ValueError("tag 名稱或代碼不可重複。")
    return tuple(result)


categories_from = tags_from  # v2 import compatibility


def parse_mrns(raw, *, limit=500):
    if not isinstance(raw, str) or len(raw) > max(20000, limit * 34):
        raise ValueError("請以文字輸入病歷號，每行一筆或以逗號分隔。")
    mrns = list(dict.fromkeys(filter(None, re.split(r"[\s,;、]+", unicodedata.normalize("NFKC", raw)))))
    if not 1 <= len(mrns) <= limit:
        raise ValueError(f"請輸入 1–{limit} 個病歷號。")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", mrn) for mrn in mrns):
        raise ValueError("病歷號限 1–32 位英數字、底線或連字號，請勿貼入姓名或其他欄位。")
    return mrns


def credentials(username, password):
    if not isinstance(username, str) or len(username) > 80:
        raise ValueError("登入帳號格式不正確。")
    username = username.strip()
    if username and not re.fullmatch(r"[A-Za-z0-9_.@-]+", username):
        raise ValueError("登入帳號只能包含英數字、底線、小數點、@ 或 -。")
    if not isinstance(password, str) or len(password) > 512:
        raise ValueError("密碼格式不正確。")
    return username, password


@dataclass(frozen=True)
class Settings:
    username: str = ""
    password: str = field(default="", repr=False)
    response_encoding: str = "auto"
    categories: tuple[Tag, ...] = DEFAULT_TAGS
    min_delay_seconds: float = 0.05
    max_delay_seconds: float = 0.15
    connect_timeout_seconds: float = 10
    read_timeout_seconds: float = 45
    max_attempts: int = 2
    allow_unverified_tls: bool = True

    def public(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k not in {"username", "password", "response_encoding"}}

    def update(self, values: dict) -> Settings:
        if not isinstance(values, dict) or set(values) - {f.name for f in fields(self)}:
            raise ValueError("設定格式或欄位不正確。")
        data = asdict(self)
        data.update(values)
        data["username"], data["password"] = credentials(data["username"], data["password"])
        if data["response_encoding"] not in {"auto", "big5", "utf-8"}:
            raise ValueError("不支援此文字編碼。")
        data["categories"] = tags_from(data["categories"])
        limits = {
            "min_delay_seconds": (0, 30), "max_delay_seconds": (0, 30),
            "connect_timeout_seconds": (1, 60), "read_timeout_seconds": (5, 180),
            "max_attempts": (1, 3),
        }
        for key, (low, high) in limits.items():
            value = data[key]
            try:
                number = float(value)
            except (ValueError, TypeError) as exc:
                raise ValueError("連線設定必須是數字。") from exc
            if isinstance(value, bool) or not math.isfinite(number) or not low <= number <= high:
                raise ValueError(f"{key} 必須介於 {low} 與 {high} 之間。")
            if key == "max_attempts" and not number.is_integer():
                raise ValueError("重試次數必須是整數。")
            data[key] = int(number) if key == "max_attempts" else number
        if data["min_delay_seconds"] > data["max_delay_seconds"]:
            raise ValueError("最長請求間隔不得小於最短間隔。")
        if type(data["allow_unverified_tls"]) is not bool:
            raise ValueError("TLS 設定必須是布林值。")
        return Settings(**data)


@dataclass(frozen=True)
class Account:
    id: str
    label: str = ""
    username: str = ""
    password: str = field(default="", repr=False)
    mode: str = "single"
    start: str = field(default_factory=lambda: today().isoformat())
    end: str = field(default_factory=lambda: today().isoformat())
    response_encoding: str = "auto"

    def persisted(self):
        return {k: v for k, v in asdict(self).items() if k != "password"}

    def public(self):
        return {**self.persisted(), "password_set": bool(self.password)}

    def update(self, values):
        if not isinstance(values, dict) or set(values) - {f.name for f in fields(self)} - {"clear_password"}:
            raise ValueError("帳號設定格式不正確。")
        data = asdict(self)
        data.update({k: v for k, v in values.items() if k not in {"id", "password", "clear_password"}})
        username, password = credentials(data["username"], values.get("password", ""))
        if type(values.get("clear_password", False)) is not bool:
            raise ValueError("清除密碼選項格式不正確。")
        if username != self.username or values.get("clear_password"):
            data["password"] = ""
        if password:
            data["password"] = password
        data["username"] = username
        if not isinstance(data["label"], str) or len(data["label"]) > 60:
            raise ValueError("帳號名稱最多 60 字。")
        data["label"] = data["label"].strip()
        if data["mode"] not in {"single", "range"} or data["response_encoding"] not in {"auto", "big5", "utf-8"}:
            raise ValueError("日期模式或文字編碼不正確。")
        if data["mode"] == "single":
            data["end"] = data["start"]
        start, end = parse_range(data, allow_future=True)
        data.update(start=start.isoformat(), end=end.isoformat())
        return Account(**data)


def load_defaults() -> Settings:
    return Settings().update(json.loads(Path(__file__).with_name("defaults.json").read_text(encoding="utf-8")))


def parse_range(values: dict, *, allow_future=False) -> tuple[date, date]:
    try:
        start = date.fromisoformat(values.get("start", today().isoformat()))
        end = date.fromisoformat(values.get("end", start.isoformat()))
    except (ValueError, TypeError) as exc:
        raise ValueError("請輸入有效的開始與結束日期。") from exc
    if start > end:
        raise ValueError("開始日期不得晚於結束日期。")
    if not allow_future and end > today():
        raise ValueError("結束日期不得晚於今天。")
    if start.year < 1912:
        raise ValueError("開始日期必須在 1912 年以後。")
    return start, end
