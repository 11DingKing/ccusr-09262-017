"""跨机构对账 HTTP 边界：逐项差异、补充说明确认，以及空差异对账。"""
from __future__ import annotations

import io
import json
import tempfile
import unittest

from service_09252_010.interfaces.wsgi_app import make_app

SUP = {"X-Institution-Id": "主管单位", "X-Role": "supervisor"}
INST_A = {"X-Institution-Id": "机构A"}
INST_B = {"X-Institution-Id": "机构B"}
INST_C = {"X-Institution-Id": "机构C"}


def call(app, method: str, path: str, body: dict | None = None,
         headers: dict | None = None, query: str = ""):
    payload = json.dumps(body).encode("utf-8") if body is not None else b""
    env = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(payload)),
        "wsgi.input": io.BytesIO(payload),
    }
    for key, value in (headers or {}).items():
        env["HTTP_" + key.upper().replace("-", "_")] = value
    captured: dict = {}

    def start_response(status, response_headers):
        captured["status"] = int(status.split()[0])

    chunks = app(env, start_response)
    return captured["status"], json.loads(b"".join(chunks).decode("utf-8"))


class ReconciliationHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="svc09252-recon-http-")
        self.app = make_app(f"{self._tmp.name}/api.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _create(self, left, right, headers=SUP,
                left_name="机构A", right_name="机构B"):
        return call(self.app, "POST", "/reconciliations", {
            "left_institution": left_name,
            "right_institution": right_name,
            "left_entries": left,
            "right_entries": right,
        }, headers)

    def test_empty_discrepancy_set_over_http(self) -> None:
        """接口测空差异：两边账目完全一致时差异必须为 0，且无 open 项。"""
        ledger = [
            {"biz_no": "BIZ-001", "amount": 100.0},
            {"biz_no": "BIZ-002", "amount": 200.0},
            {"biz_no": "BIZ-003", "amount": 300.0},
        ]
        status, body = self._create(ledger, [dict(r) for r in ledger])
        self.assertEqual(status, 201, body)
        self.assertEqual(body["summary"],
                         {"total": 3, "matched": 3, "discrepancies": 0})
        self.assertEqual(len(body["lines"]), 3)
        for line in body["lines"]:
            self.assertEqual(line["status"], "match")
            self.assertFalse(line["is_discrepancy"])
            self.assertIsNone(line["confirm_status"])
            self.assertEqual(line["left_amount"], line["right_amount"])
            self.assertEqual(line["diff"], 0.0)
            self.assertIn("金额一致", line["description"])

        # 仅看差异时应为空列表，而不是报错或塞入一致项
        run_id = body["run_id"]
        status, filtered = call(
            self.app, "GET", f"/reconciliations/{run_id}",
            headers=SUP, query="discrepancies_only=true",
        )
        self.assertEqual(status, 200, filtered)
        self.assertEqual(filtered["summary"]["discrepancies"], 0)
        self.assertEqual(filtered["lines"], [])

        # 空差异下不允许对一致项做"确认"
        status, resp = call(
            self.app, "POST",
            f"/reconciliations/{run_id}/lines/BIZ-001/confirm",
            {"note": "没有差异也要确认"}, SUP,
        )
        self.assertEqual(status, 409)
        self.assertEqual(resp["error"], "state_error")

    def test_discrepancy_full_flow_over_http(self) -> None:
        left = [
            {"biz_no": "BIZ-001", "amount": 100.0},
            {"biz_no": "BIZ-002", "amount": 200.0},
            {"biz_no": "BIZ-003", "amount": 300.0},
        ]
        right = [
            {"biz_no": "BIZ-001", "amount": 100.0},
            {"biz_no": "BIZ-002", "amount": 250.0},
            {"biz_no": "BIZ-004", "amount": 400.0},
        ]
        status, body = self._create(left, right)
        self.assertEqual(status, 201, body)
        self.assertEqual(body["summary"],
                         {"total": 4, "matched": 1, "discrepancies": 3})
        run_id = body["run_id"]

        by_biz = {line["biz_no"]: line for line in body["lines"]}
        mismatch = by_biz["BIZ-002"]
        self.assertEqual(mismatch["left_amount"], 200.0)
        self.assertEqual(mismatch["right_amount"], 250.0)
        self.assertEqual(mismatch["diff"], -50.0)
        self.assertEqual(mismatch["confirm_status"], "open")

        # 参与方逐项确认并补充说明
        status, confirmed = call(
            self.app, "POST",
            f"/reconciliations/{run_id}/lines/BIZ-002/confirm",
            {"note": "折算时差 50，凭证已补"}, INST_B,
        )
        self.assertEqual(status, 200, confirmed)
        self.assertEqual(confirmed["confirm_status"], "confirmed")
        self.assertEqual(confirmed["note"], "折算时差 50，凭证已补")
        self.assertEqual(confirmed["confirmed_by"], "机构B")
        # 确认结果里依旧同时带双方金额
        self.assertEqual(confirmed["left_amount"], 200.0)
        self.assertEqual(confirmed["right_amount"], 250.0)

        # 外部传入的 description 不会被采纳：说明字段由服务端生成
        self.assertNotIn("客户端伪造", mismatch["description"])
        status, inject = call(
            self.app, "POST",
            f"/reconciliations/{run_id}/lines/BIZ-003/confirm",
            {"note": "左账漏登", "description": "客户端伪造"}, INST_A,
        )
        self.assertEqual(status, 200, inject)
        status, fetched = call(
            self.app, "GET", f"/reconciliations/{run_id}", headers=INST_A
        )
        line3 = {l["biz_no"]: l for l in fetched["lines"]}["BIZ-003"]
        self.assertNotIn("客户端伪造", line3["description"])
        self.assertIn("机构B无记录", line3["description"])

    def test_non_participant_forbidden(self) -> None:
        status, body = self._create(
            [{"biz_no": "X", "amount": 1}],
            [{"biz_no": "X", "amount": 2}],
        )
        self.assertEqual(status, 201, body)
        run_id = body["run_id"]
        status, resp = call(
            self.app, "GET", f"/reconciliations/{run_id}", headers=INST_C
        )
        self.assertEqual(status, 403)
        self.assertEqual(resp["error"], "permission_denied")

    def test_missing_field_422(self) -> None:
        status, resp = call(self.app, "POST", "/reconciliations", {
            "left_institution": "机构A",
            "left_entries": [{"biz_no": "X", "amount": 1}],
            "right_entries": [{"biz_no": "X", "amount": 1}],
        }, SUP)
        self.assertEqual(status, 422)
        self.assertEqual(resp["error"], "validation_error")

    def test_unknown_run_404(self) -> None:
        status, resp = call(
            self.app, "GET", "/reconciliations/recon-nope", headers=SUP
        )
        self.assertEqual(status, 404)
        self.assertEqual(resp["error"], "not_found")

    def test_confirm_unknown_biz_404(self) -> None:
        status, body = self._create(
            [{"biz_no": "X", "amount": 1}], [{"biz_no": "X", "amount": 2}]
        )
        self.assertEqual(status, 201, body)
        status, resp = call(
            self.app, "POST",
            f"/reconciliations/{body['run_id']}/lines/NO-SUCH/confirm",
            {"note": "不存在的业务编号"}, SUP,
        )
        self.assertEqual(status, 404)
        self.assertEqual(resp["error"], "not_found")


if __name__ == "__main__":
    unittest.main()
