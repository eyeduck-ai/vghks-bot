"""Read-only live schema and permission check; reports contain no clinical values.

Google API calls are GET-only. Identity migration requests are calculated in
memory and never sent. OAuth token exchange uses only Google's official URI.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google.auth.exceptions import GoogleAuthError  # noqa: E402
from google.auth.transport.requests import AuthorizedSession  # noqa: E402
from google.oauth2 import service_account  # noqa: E402
from vghks_sheet_core.calendar import scan  # noqa: E402

from vghks_bot.google_sheets import SheetsClient, column_name, spreadsheet_id  # noqa: E402
from vghks_bot.sheet_identity import (  # noqa: E402
    ARCHIVE_PATTERN,
    EVENT_ID_ALIASES,
    EVENT_ID_HEADER,
    MONTH_PATTERN,
    SYNC_ID_HEADER,
    SYNC_STATE_TITLE,
    plan_identity_setup,
    schema_kind,
)
from vghks_bot.sheet_plan import FU_HEADERS, HEADERS, entries  # noqa: E402
from vghks_bot.sheet_schema import canonical_header as shared_canonical_header  # noqa: E402
from vghks_bot.sheet_sync_state import STATE_HEADERS, STATE_VERSION  # noqa: E402

SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
)
TOKEN_URI = "https://oauth2.googleapis.com/token"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
KNOWN_HEADERS = {shared_canonical_header(value) for value in (*HEADERS.values(), *FU_HEADERS, *STATE_HEADERS,
                 *EVENT_ID_ALIASES, "IOL Final", "Axis", "ArchiveRowType", "ArchiveReason", "ArchiveAt")}
ISSUE_CODES = {
    "duplicate_header", "missing_identity_header", "identity_write_blocked",
    "invalid_day_header", "invalid_sync_id", "orphan_sync_id", "duplicate_sync_id",
    "protected_identity_range",
}


class ContractError(Exception):
    """A safe, fixed diagnostic code; never retains an HTTP or credential body."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ReadOnlySession:
    def __init__(self, session):
        self.session = session

    def request(self, method, url, **kwargs):
        if method != "GET":
            raise ContractError("mutation_blocked")
        return self.session.request(method, url, **kwargs)

    def close(self):
        self.session.close()


def credentials_from_file(path):
    try:
        if path.stat().st_size > 65536:
            raise ContractError("invalid_service_account")
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict) or raw.get("type") != "service_account" or raw.get("token_uri") != TOKEN_URI:
            raise ContractError("invalid_service_account")
        return service_account.Credentials.from_service_account_info(raw, scopes=SCOPES)
    except ContractError:
        raise
    except Exception:
        raise ContractError("invalid_service_account") from None


def make_session(path):
    return ReadOnlySession(AuthorizedSession(credentials_from_file(path)))


def api_not_enabled(response):
    """Recognize only Google's two fixed disabled-API reasons, never echo bodies."""
    try:
        error = response.json().get("error", {})
        for key, reason in (("errors", "accessNotConfigured"), ("details", "SERVICE_DISABLED")):
            values = error.get(key, [])
            if isinstance(values, list) and any(isinstance(value, dict) and value.get("reason") == reason for value in values):
                return True
    except Exception:
        pass
    return False


def get_json(session, url, *, params):
    try:
        response = session.request("GET", url, params=params, timeout=(10, 55))
        if response.status_code == 403 and api_not_enabled(response):
            raise ContractError("api_not_enabled")
        if response.status_code in {401, 403}:
            raise ContractError("authorization_unverified")
        if response.status_code == 404:
            raise ContractError("target_unavailable")
        if response.status_code == 429 or response.status_code >= 500:
            raise ContractError("api_temporarily_unavailable")
        if not response.ok:
            raise ContractError("api_request_rejected")
        value = response.json()
        if not isinstance(value, dict):
            raise ContractError("invalid_api_response")
        return value
    except ContractError:
        raise
    except GoogleAuthError:
        raise ContractError("authorization_unverified") from None
    except Exception:
        raise ContractError("api_unavailable") from None


def safe_error(exc):
    return exc.code if isinstance(exc, ContractError) else "check_unavailable"


def managed_title(title):
    return isinstance(title, str) and bool(MONTH_PATTERN.fullmatch(title) or ARCHIVE_PATTERN.fullmatch(title)
                                         or title in {"FU", "All", SYNC_STATE_TITLE})


def canonical_header(cell):
    label = shared_canonical_header(cell)
    return label if label in KNOWN_HEADERS else "其他欄位" if label else ""


def sheet_headers(sheet):
    _, index = schema_kind(sheet)
    index = index if index is not None else 0
    rows = sheet.get("rows", [])
    row = rows[index] if index < len(rows) else []
    metadata = sheet.get("column_widths", [])
    return [{"column": column_name(column), "name": canonical_header(cell),
             "hidden": bool(metadata[column].get("hiddenByUser")) if column < len(metadata) else False}
            for column, cell in enumerate(row) if canonical_header(cell)]


def technical_columns(before, after):
    old, new = sheet_headers(before), sheet_headers(after)
    labels = STATE_HEADERS if before["properties"]["title"] == SYNC_STATE_TITLE else (EVENT_ID_HEADER, SYNC_ID_HEADER)
    result = []
    for label in labels:
        current = next((item for item in old if item["name"] == label), None)
        planned = next((item for item in new if item["name"] == label), None)
        result.append({"name": label, "existing": current is not None,
                       "existing_column": current["column"] if current else None,
                       "hidden_existing": current["hidden"] if current else None,
                       "needs_add": current is None, "planned_column": planned["column"] if planned else None,
                       "hidden_planned": planned["hidden"] if planned else None})
    return result


def header_diagnostic(sheet):
    kind, _ = schema_kind(sheet)
    labels = [item["name"] for item in sheet_headers(sheet)]
    known = set(labels)
    duplicated = sorted(label for label, count in Counter(labels).items() if count > 1 and label != "其他欄位")
    main_required = set(HEADERS.values()) if kind == "month" else set(FU_HEADERS[:-2]) if kind == "fu" else set()
    monitor_required = {"病歷號", "姓名", SYNC_ID_HEADER, EVENT_ID_HEADER}
    monitor_required.add("日期／報到時間" if kind == "month" else "日期" if kind == "fu" else "ArchiveRowType")
    if kind is None:
        main_required, monitor_required = set(STATE_HEADERS), set(STATE_HEADERS)
    return {"tab": sheet["properties"]["title"], "main_missing": sorted(main_required - known),
            "monitor_missing": sorted(monitor_required - known), "duplicate_canonical": duplicated}


def issue_summary(issues):
    counts = Counter((issue.get("code") if issue.get("code") in ISSUE_CODES else "identity_issue",
                      issue.get("sheet") if managed_title(issue.get("sheet")) else "managed_tab") for issue in issues)
    return [{"code": code, "tab": tab, "count": count} for (code, tab), count in sorted(counts.items())]


def monitor_issue_summary(issues):
    checks = (("重複欄名", "duplicate_header"), ("缺少欄位", "missing_header"),
              ("刀日院區", "invalid_hospital"), ("刀日標題", "invalid_day_header"),
              ("上方沒有有效刀日", "missing_day_block"), ("排程 ID 重複", "duplicate_sync_id"),
              ("排程 ID 格式", "invalid_sync_id"), ("缺少排程 ID", "missing_sync_id"),
              ("日曆 ID 重複", "duplicate_event_id"), ("日曆 ID 沒有", "orphan_event_id"),
              ("只剩排程 ID", "orphan_sync_id"), ("追蹤日期", "invalid_fu_date"))
    counts = Counter(next((code for text, code in checks if text in issue), "monitor_schema_issue") for issue in issues)
    return [{"code": code, "count": count} for code, count in sorted(counts.items())]


def analyze_book(book):
    setup = plan_identity_setup(book, include_archives=True)
    planned = {sheet["properties"]["title"]: sheet for sheet in setup["book"]["sheets"]}
    tabs = [{"tab": sheet["properties"]["title"], "headers": sheet_headers(sheet),
             "hidden": bool(sheet["properties"].get("hidden")),
             "technical_columns": technical_columns(sheet, planned[sheet["properties"]["title"]])}
            for sheet in book["sheets"] if managed_title(sheet["properties"]["title"])]
    diagnostics = [header_diagnostic(sheet) for sheet in setup["book"]["sheets"] if managed_title(sheet["properties"]["title"])]
    main, main_errors = {}, 0
    for sheet in setup["book"]["sheets"]:
        if not MONTH_PATTERN.fullmatch(sheet["properties"]["title"]):
            continue
        try:
            for item in entries(sheet):
                if item["kind"] == "patient":
                    main[f"{sheet['properties']['title']}!{item['index'] + 1}"] = (item["fields"]["date"], item["fields"]["hospital"])
        except (ValueError, KeyError, TypeError):
            main_errors += 1
    monitor = scan(setup["book"])
    row_issues = monitor["row_issues"]
    monitored = {f"{item['sheet']}!{item['row'] + 1}": (item["date"], item["hospital"])
                 for item in monitor["patients"] if item["kind"] == "MONTHLY"}
    mismatches = [{"row": ref, "code": "missing_from_main" if ref not in main else "missing_from_monitor" if ref not in monitored else "route_mismatch"}
                  for ref in sorted(main.keys() | monitored.keys()) if main.get(ref) != monitored.get(ref)]
    header_errors = sum(bool(item["main_missing"] or item["monitor_missing"] or item["duplicate_canonical"]) for item in diagnostics)
    return {"tabs": tabs, "header_diagnostics": diagnostics,
            "migration": {"request_count": len(setup["requests"]), "required": bool(setup["requests"]),
                          "issue_count": len(setup["issues"]), "issues": issue_summary(setup["issues"])},
            "parsing": {"match": not mismatches and not main_errors, "monthly_main_count": len(main),
                        "monthly_monitor_count": len(monitored), "mismatch_count": len(mismatches), "mismatches": mismatches,
                        "main_header_error_count": main_errors, "monitor_issue_count": len(monitor["issues"]),
                        "monitor_issues": monitor_issue_summary(monitor["issues"]),
                        "monitor_row_issue_count": len(row_issues),
                        "monitor_row_issues": [{"code": code, "count": count} for code, count in
                                               sorted(Counter(issue["code"] if issue["code"] == "invalid_fu_date"
                                                              else "row_issue" for issue in row_issues).items())]},
            "compatible": not setup["issues"] and not header_errors and not main_errors and not mismatches
                          and not monitor["issues"] and not row_issues}


def gas_contract(repo):
    if repo is None:
        return {"verification": "not_requested"}
    try:
        value = json.loads((repo / "tests" / "fixtures" / "schema_parsing.json").read_text(encoding="utf-8"))
        version = next((value[key] for key in ("contract_version", "contractVersion", "version") if key in value), None)
        if isinstance(version, (str, int)) and not isinstance(version, bool) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(version)):
            return {"verification": "version_available", "contract_version": version}
    except Exception:
        pass
    return {"verification": "unverified", "code": "gas_fixture_unavailable"}


def build_report(sheets, session, calendar_id, *, gas_repo=None):
    report = {"report_version": 1, "sync_state_version": STATE_VERSION, "read_only": True,
              "checked_at": datetime.now(UTC).isoformat(), "gas_contract": gas_contract(gas_repo)}
    try:
        sheets.request("GET", params={"fields": "spreadsheetId,properties(timeZone),sheets(properties)"})
        report["sheet"] = {"readable": True, **analyze_book(sheets.read_all())}
    except Exception as exc:
        report["sheet"] = {"readable": False, "code": safe_error(exc)}
    try:
        value = get_json(session, "https://www.googleapis.com/calendar/v3/calendars/" + quote(calendar_id, safe="") + "/events",
                         params={"maxResults": 1, "fields": "accessRole,timeZone"})
        role = value.get("accessRole")
        role = role if role in {"none", "freeBusyReader", "reader", "writerWithoutPrivateAccess", "writer", "owner"} else "unknown"
        zone = value.get("timeZone", "")
        report["calendar"] = {"readable": True, "access_role": role, "can_write": role in {"writer", "owner"},
                              "time_zone": zone if isinstance(zone, str) and re.fullmatch(r"[A-Za-z0-9_+./-]{1,100}", zone) else "unverified"}
    except Exception as exc:
        report["calendar"] = {"readable": False, "can_write": None, "code": safe_error(exc)}
    try:
        value = get_json(session, "https://www.googleapis.com/drive/v3/files/" + quote(sheets.key, safe=""),
                         params={"fields": "mimeType,capabilities(canEdit)", "supportsAllDrives": "true"})
        can_edit = value.get("capabilities", {}).get("canEdit")
        verified = isinstance(can_edit, bool) and value.get("mimeType") == SHEET_MIME
        report["sheet_edit_permission"] = {"verification": "verified" if verified else "unverified",
                                           "is_google_spreadsheet": value.get("mimeType") == SHEET_MIME,
                                           "can_edit": can_edit if verified else None}
    except Exception as exc:
        report["sheet_edit_permission"] = {"verification": "unverified", "can_edit": None, "code": safe_error(exc)}
    report["ok"] = (report["sheet"].get("compatible") is True and report["calendar"].get("can_write") is True
                    and report["sheet_edit_permission"].get("can_edit") is True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-account", type=Path, required=True)
    parser.add_argument("--spreadsheet", required=True)
    parser.add_argument("--calendar-id", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--gas-repo", type=Path)
    args = parser.parse_args(argv)
    if args.report.resolve() == args.service_account.resolve():
        print("Report path must differ from the service-account file.", file=sys.stderr)
        return 2
    session = None
    try:
        key = spreadsheet_id(args.spreadsheet)
        if not args.calendar_id.strip() or len(args.calendar_id) > 2000 or "\x00" in args.calendar_id:
            raise ContractError("invalid_calendar_id")
        session = make_session(args.service_account)
        report = build_report(SheetsClient(key, session), session, args.calendar_id, gas_repo=args.gas_repo)
    except Exception as exc:
        report = {"report_version": 1, "read_only": True, "ok": False, "code": safe_error(exc)}
    finally:
        if session is not None:
            session.close()
    try:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        print("Unable to save the contract report.", file=sys.stderr)
        return 2
    print("Sheet contract check: " + ("passed" if report["ok"] else "needs attention") + "; report saved.")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
