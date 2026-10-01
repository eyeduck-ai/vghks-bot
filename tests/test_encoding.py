import unittest
from datetime import date
from types import SimpleNamespace

from vghks_sdk.parsing.prq import parse_opd_patients

from vghks_bot.encoding import ClinicalTransport, decode_response
from vghks_bot.scanner import create_sdk
from vghks_bot.settings import Settings


class EncodingTests(unittest.TestCase):
    def test_big5_patient_fields_survive_invalid_byte_elsewhere(self):
        html = """<meta charset=big5><script>
        aryOpdSec[0]='70';aryOpdRoom[0]='02';aryOpdDoc[0]='DOC1F';
        if(aryOpdSec[0]=='70'){new KSCase('','1','TEST001','測試姓名','女','68歲');}
        </script>"""
        raw = html.encode("cp950") + b"\xff"
        decoded = decode_response(raw,"text/html; charset=big5")
        patients = parse_opd_patients(decoded,visit_date=date(2026,9,19),doctor_card="DOC1")
        self.assertEqual(len(patients),1)
        self.assertEqual((patients[0].name,patients[0].sex,patients[0].age),("測試姓名","女","68歲"))
        self.assertEqual(patients[0].sequence_no, "1")

    def test_utf8_big5_headers_meta_and_overrides(self):
        for header in ("","text/html; charset=iso-8859-1","text/html; charset=utf-8","text/html; charset=big5"):
            with self.subTest(header=header):
                self.assertEqual(decode_response("姓名 女 68歲".encode("cp950"),header),"姓名 女 68歲")
                self.assertEqual(decode_response("姓名 女 68歲".encode(),header),"姓名 女 68歲")
        self.assertEqual(decode_response("姓名".encode("utf-8-sig")),"姓名")
        self.assertEqual(decode_response("姓名".encode("cp950"),encoding="big5"),"姓名")
        self.assertEqual(decode_response("姓名".encode(),encoding="utf-8"),"姓名")

    def test_unrecoverable_replacement_is_not_fabricated(self):
        self.assertEqual(decode_response("���J�q�@�@".encode()),"���J�q�@�@")

    def test_sdk_uses_transport_without_network(self):
        with create_sdk(Settings(username="DOC1",password="synthetic")) as sdk:
            self.assertIsInstance(sdk._runtime.transport,ClinicalTransport)
            response = SimpleNamespace(content="女".encode("cp950"),headers={"Content-Type":"text/html"})
            self.assertEqual(sdk._runtime.transport.text(response),"女")
