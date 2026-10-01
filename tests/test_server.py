import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from vghks_bot.selftest import SyntheticSDK
from vghks_bot.server import Application, LocalServer
from vghks_bot.settings import Settings


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(Settings(),Path(self.temp.name),SyntheticSDK)
        self.server = LocalServer(0,self.app)
        self.thread = threading.Thread(target=self.server.serve_forever,kwargs={"poll_interval":.01},daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        self.app.close()
        self.temp.cleanup()

    def request(self,path,body=None,*,auth=True,csrf=True,headers=None):
        connection = http.client.HTTPConnection("127.0.0.1",self.server.server_port,timeout=4)
        request_headers = {}
        if auth:
            request_headers["Cookie"] = f"opd_session={self.app.session_token}"
        if csrf:
            request_headers["X-CSRF-Token"] = self.app.csrf_token
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        connection.request("POST" if body is not None else "GET",path,json.dumps(body) if body is not None else None,request_headers)
        response = connection.getresponse()
        result = response.status,dict(response.getheaders()),response.read()
        connection.close()
        return result

    def test_assets_no_demo_and_security_headers(self):
        status,headers,body = self.request("/",auth=False)
        self.assertEqual(status,200)
        self.assertIn("門診病歷工作台",body.decode())
        self.assertNotIn("查看示範",body.decode())
        self.assertNotIn(self.app.launch_token.encode(),body)
        self.assertEqual(headers["Cache-Control"],"no-store")
        self.assertIn("frame-ancestors 'none'",headers["Content-Security-Policy"])
        for path in ("/app.js","/workspace.js","/analysis.js","/patient-tags.js","/analysis.css","/style.css","/favicon.svg"):
            self.assertEqual(self.request(path,auth=False)[0],200)
        for path in ("/api/demo","/api/clear"):
            self.assertEqual(self.request(path,{})[0],404)
        self.assertEqual(self.request("/../defaults.json")[0],404)

    def test_history_endpoints_require_session(self):
        for path in ("/api/history","/api/run?id="+"a"*32,"/api/bootstrap"):
            self.assertEqual(self.request(path,auth=False)[0],401)
        self.assertEqual(self.request("/api/start",{},auth=False)[0],401)

    def test_analysis_endpoints_and_attachments_require_session_and_csrf(self):
        from vghks_sdk.models import BinaryAsset
        for path in ("cohorts", "cohorts/save", "cohorts/delete", "start", "results", "raw", "delete",
                     "google", "google/save", "google/remove", "google/test", "surgery/candidates",
                     "surgery/preview", "surgery/apply", "surgery/check", "surgery/history"):
            self.assertEqual(self.request("/api/analysis/" + path, {}, auth=False)[0], 401, path)
            self.assertEqual(self.request("/api/analysis/" + path, {}, csrf=False)[0], 403, path)
        content = b"%PDF-1.4\nsynthetic report"
        asset = self.app.analysis.store.save_asset("TEST", "report", BinaryAsset(content, "application/pdf"), "TEST")
        url = "/api/analysis/asset?id=" + asset["digest"]
        self.assertEqual(self.request(url, auth=False)[0], 401)
        code, headers, body = self.request(url)
        self.assertEqual((code, body), (200, content))
        self.assertEqual(headers["Content-Type"], "application/pdf")
        self.assertEqual(headers["Cache-Control"], "no-store")
        code, headers, body = self.request(url, headers={"Range": "bytes=0-7"})
        self.assertEqual((code, body), (206, content[:8]))
        self.assertEqual(headers["Content-Range"], "bytes 0-7/" + str(len(content)))
        self.assertEqual(self.request(url, headers={"Range": "bytes=999-1000"})[0], 416)
        self.assertEqual(self.request("/api/analysis/asset?id=../settings.json")[0], 404)
        self.app.analysis.store.delete_patients(["TEST"])
        self.assertEqual(self.request(url)[0], 404)

    def test_session_cookie_csrf_host_origin_and_fetch_site(self):
        self.assertEqual(self.request("/api/session",{"token":"wrong"},auth=False)[0],401)
        status,headers,_ = self.request("/api/session",{"token":self.app.launch_token},auth=False)
        self.assertEqual(status,200)
        self.assertIn("HttpOnly",headers["Set-Cookie"])
        self.assertIn("SameSite=Strict",headers["Set-Cookie"])
        self.assertEqual(self.request("/api/settings",{},csrf=False)[0],403)
        for headers in ({"Host":"evil.example"},{"Origin":"http://evil.example"},{"Sec-Fetch-Site":"cross-site"}):
            self.assertEqual(self.request("/api/history",headers=headers)[0],403)
            self.assertEqual(self.request("/api/settings",{},headers=headers)[0],403)

    def test_patient_tags_require_auth_csrf_and_keep_soap_source_separate(self):
        for path in ("/api/patient-tags/search", "/api/patient-tags/update"):
            self.assertEqual(self.request(path, {}, auth=False)[0], 401)
            self.assertEqual(self.request(path, {}, csrf=False)[0], 403)
        reply = self.request("/api/patient-tags/update", {"mrns": "0000001", "new_tag": "研究候選"})
        self.assertEqual(reply[0], 200)
        tag = json.loads(reply[2])["settings"]["categories"][-1]["id"]
        patients = json.loads(self.request("/api/patient-tags/search", {"tag": tag})[2])["patients"]
        self.assertEqual(patients[0]["mrn"], "0000001")
        self.assertEqual(patients[0]["auto_tags"], [])
        self.assertEqual(patients[0]["source_records"], [])
        self.assertEqual(self.request("/api/patient-tags/update", {"mrns": "1 姓名", "tag_ids": [tag]})[0], 400)

    def test_save_query_and_history_no_password_in_responses(self):
        key = next(iter(self.app.accounts))
        status,_,body = self.request("/api/accounts/save",{"accounts":[{"id":key,"username":"DOC1","password":"synthetic-secret"}]})
        self.assertEqual(status,200)
        self.assertNotIn(b"synthetic-secret",body)
        self.assertNotIn(b"synthetic-secret",self.request("/api/bootstrap")[2])
        status,_,body = self.request("/api/start",{"account_ids":[key]})
        self.assertEqual(status,202)
        run_id = json.loads(body)["run_ids"][0]
        self.assertTrue(self.app.idle.wait(5))
        result = json.loads(self.request("/api/run?id="+run_id)[2])
        self.assertEqual(result["status"],"completed")
        self.assertEqual(result["counts"]["soap_read"],3)
        self.assertEqual(result["records"][0]["name"],"測試病人甲")
        self.assertNotIn("synthetic-secret",str(result))
        self.assertEqual(json.loads(self.request(f'/api/run?id={run_id}&revision={result["revision"]}')[2]),{"unchanged":True})
        self.assertEqual(len(json.loads(self.request("/api/history")[2])["runs"]),1)

    def test_invalid_run_id_and_credentials_rejected(self):
        self.assertEqual(self.request("/api/run?id=../settings")[0],400)
        self.assertEqual(self.request("/api/start",{"account_ids":[next(iter(self.app.accounts))]})[0],400)

    def test_two_stage_library_endpoints_and_delete_require_session_csrf(self):
        key = next(iter(self.app.accounts))
        self.app.save_accounts({"accounts": [{"id": key, "username": "TEST", "password": "synthetic", "start": "2026-09-18"}]})
        for path in ("/api/lists", "/api/lists/browse", "/api/lists/delete", "/api/fetch", "/api/library/search", "/api/library/record", "/api/library/delete"):
            self.assertEqual(self.request(path, {}, auth=False)[0], 401)
            self.assertEqual(self.request(path, {}, csrf=False)[0], 403)
        self.assertEqual(self.request("/api/lists/browse", {"account_ids": [key]})[0], 202)
        self.assertTrue(self.app.idle.wait(5))
        rows = json.loads(self.request("/api/lists", {"account_id": key})[2])["days"][0]["rows"]
        self.assertEqual(json.loads(self.request("/api/library/search", {})[2])["total"], 0)
        self.assertEqual(self.request("/api/fetch", {"selections": [{"account_id": key, "rows": [{"id": rows[2]["id"], "day": rows[2]["day"]}]}]})[0], 202)
        self.assertTrue(self.app.idle.wait(5))
        result = json.loads(self.request("/api/library/search", {"q": "追蹤"})[2])
        self.assertEqual(result["total"], 1)
        record_id = result["records"][0]["id"]
        self.assertEqual(json.loads(self.request("/api/library/record", {"id": record_id})[2])["version_count"], 1)
        self.assertEqual(self.request("/api/library/delete", {"ids": [record_id]})[0], 200)
        self.assertEqual(json.loads(self.request("/api/library/search", {})[2])["total"], 0)
