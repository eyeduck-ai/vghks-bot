"""Synthetic HTTP responses through the real SDK; never access hospital hosts."""
from __future__ import annotations

import json
from types import SimpleNamespace

import requests
from vghks_sdk import PortalCredentials, RequestPolicy, SDKSettings, VghksSDK
from vghks_sdk.adapters.auth import AppSession
from vghks_sdk.core.errors import RequestError

from .encoding import ClinicalTransport

MRN = "00012345"
PATIENT = {"patno": MRN, "patname": "合成病人", "birthday": "047/01/01", "hsex": "女"}
TIMEOUT = '<html><p>Page time out, Please <a>重新登入</a></p></html>'


def form(page="RSV11W001", token="fresh-token"):
    identifier = "QUY15WForm" if page == "QUY15W001" else "RSV11WForm"
    return f'<form id="{identifier}"><input type="hidden" name="org.apache.struts.taglib.html.TOKEN" value="{token}"></form>'


def basic_page():
    return '<div id="DETAIL">' + "".join(
        f'<div class="form-group"><label>{label}</label><span class="label label-orange">{value}</span></div>'
        for label, value in (("病歷號", MRN), ("姓名", "合成病人"), ("生日", "1958-01-01"), ("性別", "女"))) + '</div>'


class SessionScenario:
    """Seed an authenticated SDK, then emulate lost WebMAAS cookies on reads."""

    def __init__(self, mode="timeout", page="RSV11W001"):
        self.mode, self.page = mode, page
        self.expired = mode in {"timeout", "persistent_timeout", "sso_network"}
        self.requests, self.connections = [], []

    def factory(self, settings):
        configuration = SDKSettings(portal_base_url="https://synthetic.invalid",
            webmaas_base_url="https://synthetic.invalid/webmaas", sectord_base_url="https://synthetic.invalid/SectOrdWeb",
            request_policy=RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, max_attempts=1))
        session = requests.Session()
        session.request = self.request
        transport = ClinicalTransport(policy=configuration.request_policy, verify=True, session=session,
                                      response_encoding=settings.response_encoding)
        sdk = VghksSDK(settings=configuration, transport=transport,
                       credentials=PortalCredentials("SYNTHETIC", "SECRET_PASSWORD"))
        auth = sdk._runtime.auth
        auth._portal_authenticated, auth._generation = True, 1
        auth._webmaas_page = self.page
        route = "/QUY/QUY15W001.do" if self.page == "QUY15W001" else "/RSV/RSV11W001.do"
        auth._apps["webmaas"] = AppSession("webmaas", "SYNTHETIC-HID", configuration.webmaas_base_url + route,
                                            form(self.page, "stale-token"))
        auth._apps["sectord"] = AppSession("sectord", "SYNTHETIC-HID", configuration.sectord_base_url)
        auth.check_portal_session = lambda: configuration.portal_base_url + "/sessionCheck.do"
        public_check = sdk.auth.check
        sdk.auth.check = lambda only=None: (SimpleNamespace(ok=True, reauthenticated=False, targets=())
                                            if tuple(only or ()) == ("prq",) else public_check(only=only))
        self.connections.append(sdk)
        return sdk

    def request(self, method, url, **kwargs):
        if not url.startswith("https://synthetic.invalid/"):
            raise AssertionError("synthetic session cannot leave its fake origin")
        self.requests.append({"method": method, "url": url, "params": kwargs.get("params", {}),
                              "data": kwargs.get("data", {})})
        response = requests.Response()
        response.status_code, response.url = 200, url
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        if url.endswith("/so.do"):
            if self.mode == "sso_network":
                raise RequestError("synthetic DNS failure", code="NETWORK_DNS_FAILED", retry_safe=True)
            body = "ssID=fresh&keyOne=1&keyTwo=2&keyThree=3"
        elif url.endswith("/WPSAutoLogon"):
            if self.mode == "persistent_timeout":
                body = TIMEOUT
                response.url = "https://synthetic.invalid/webmaas/comm/pageTimeOut.do"
            else:
                self.expired = False
                response.url = kwargs["params"]["targetURL"]
                body = form("QUY15W001" if "/QUY/" in response.url else "RSV11W001")
        elif self.mode == "unknown":
            body = "<html><p>Unrecognized synthetic page</p></html>"
        elif self.mode in {"form_missing", "token_missing"} and method == "GET" and "/webmaas/" in url:
            body = "<html>Unknown query page</html>" if self.mode == "form_missing" else form(self.page, "")
        elif self.mode == "http_503":
            response.status_code, body = 503, "<html>Service unavailable</html>"
        elif self.expired:
            body = TIMEOUT
            response.url = "https://synthetic.invalid/webmaas/comm/pageTimeOut.do"
        elif url.endswith("/ajax/AJAXAction.do"):
            response.headers["Content-Type"] = "application/json; charset=utf-8"
            body = json.dumps(PATIENT, ensure_ascii=False)
        elif method == "POST" and url.endswith("/QUY/QUY15W001.do"):
            body = basic_page()
        else:
            body = form("QUY15W001" if "/QUY/" in url else "RSV11W001")
        response._content = body.encode("utf-8")
        return response


def check_sdk_session_recovery(directory):
    """Exercise public patients API and platform DEBUG from a frozen executable."""
    from .bot import BotApplication
    from .settings import Settings

    scenario = SessionScenario()
    app = BotApplication(Settings(), directory, scenario.factory)
    try:
        account = app.login({"username": "SYNTHETIC", "password": "SECRET_PASSWORD"})["account"]["id"]
        workspace = app.workspace(account)
        original_session = workspace.gateway.session_id
        with workspace.gateway.task_context("selftest-sdk-session", "resolve"):
            value = workspace.gateway.invoke("patients", "get_demographics", MRN)
        assert value.mrn == MRN and value.name == PATIENT["patname"]
        assert len(scenario.connections) == 1 and workspace.gateway.session_id == original_session
        assert workspace.gateway.recovery_count == 1 and workspace.gateway.online
        assert len([entry for entry in scenario.requests if entry["url"].endswith("/WPSAutoLogon")]) == 1
        assert not any(entry["method"] == "POST" and "/webmaas/" not in entry["url"] for entry in scenario.requests)
        incident, = workspace.diagnostics.query({"task_id": "selftest-sdk-session"})["items"]
        assert incident["recovered"] and incident["error"]["cause"]["code"] == "WEBMAAS_SESSION_TIMEOUT"
    finally:
        app.close()
