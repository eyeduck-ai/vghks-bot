"""Public synthetic printout for structured SOAP integration and EXE checks."""
from html import escape

from vghks_sdk.parsing.prq import parse_soap


def synthetic_soap(case, label="TEST original", *, mixed_prescriptions=False):
    def pre(text):
        return '<div class="soap"><pre>'+escape(text)+'</pre></div>'

    subjective = "合成病歷 " + label + " 🙂 主訴文字"
    objective = "<script>not executable</script>\nVa OD 0.4 / OS HM"
    assessment_plan = ("Cataract OD\nP: # Arrange CATA OD (IOL SN60WF +21.0 T-0.50) on 20260930\n"
                       "- TEL: 0900000000\n# APPLY Cataract OD")
    medication_header = "藥"+"\u3000"*12+"  名\u3000劑量\u3000 單位 途徑 頻次   天數  總發藥量"
    medication_row = f"{'SYNTHETIC DROP':<32}{'1':<7}{'DROP':<5}{'OU':<5}{'BID':<7}{'7':<6}1.00"
    orders = "檢查驗項目"+" "*26+"數量\n"+f"{'SYNTHETIC FUNDUS':<36}1.00"
    notice = "慢性病連續處方箋處方 服藥期限:2026-09-21~2026-12-14"
    medication_table = notice+"\n"+medication_header+"\n"+medication_row
    if mixed_prescriptions:
        notice = "慢性病連續處方箋處方　服藥期限：2026/09/21 ∼ 2026/12/14"
        chronic = f"{'SYNTHETIC CHRONIC DROP':<32}{'X1':<7}{'BOT':<5}{'OU':<5}{'QPM':<7}{'28':<6}0001"
        medication_table = medication_header+"\n"+medication_row+"\n\n"+notice+"  ◆健保號碼：SYNTHETIC\n"+medication_header+"\n"+chronic
    html = '<div id="data"><table>'
    for label, value in (("S:", subjective), ("O:", objective), ("A+P:", assessment_plan)):
        html += '<tr><th>'+label+'</th><td>'+pre(value)+'</td></tr>'
    html += '</table>'+pre("ICD碼：Z00.00 Synthetic diagnosis")
    html += pre(medication_table)+pre(orders)+pre("其他合成原文")+'</div>'
    return parse_soap(html, case)
