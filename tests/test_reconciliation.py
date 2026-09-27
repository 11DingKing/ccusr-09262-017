"""跨机构数据对账：逐项差异、双方金额、系统生成说明、补充说明与逐项确认。

接口层重点验证：双方账目完全一致时差异为空（空差异），以及
差异未逐项确认前不能关闭对账（不静默取一边的值）。
"""
from __future__ import annotations

import tempfile
import unittest

from service_09252_010.domain.errors import (
    NotFoundError,
    PermissionDeniedError,
    StateError,
    ValidationError,
)
from service_09252_010.interfaces.wsgi_app import make_app
from support import INST_A, INST_B, PROJECT, SUPERVISOR, RigTestCase
from test_http_api import INST_A as INST_A_HEADERS
from test_http_api import SUP as SUP_HEADERS
from test_http_api import call


def _items(*pairs: tuple[str, float]) -> list[dict]:
    return [{"business_no": no, "amount": amount} for no, amount in pairs]


class ReconciliationCase(RigTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.rig.grant(INST_A.institution_id, permission="reconcile")

    def create(self, left: list[dict], right: list[dict],
               principal=INST_A, **overrides) -> dict:
        params = dict(project_id=PROJECT, left_institution="机构A",
                      right_institution="机构B", period="2026-08",
                      left_items=left, right_items=right)
        params.update(overrides)
        return self.rig.reconciliations.create_run(principal, **params)

    def create_with_differences(self) -> dict:
        """B001 一致、B002 金额不一致、B003 仅左方、B004 仅右方。"""
        return self.create(
            _items(("B001", 100), ("B002", 200), ("B003", 300)),
            _items(("B001", 100), ("B002", 260), ("B004", 400)),
        )


class DifferenceTests(ReconciliationCase):
    def test_differences_carry_both_amounts_and_generated_description(self):
        run = self.create_with_differences()
        self.assertEqual(run["status"], "open")
        self.assertEqual(run["matched_count"], 1)  # B001 一致，不形成差异项
        self.assertEqual(run["difference_count"], 3)
        self.assertEqual(run["pending_count"], 3)

        diffs = {d["business_no"]: d for d in run["differences"]}
        self.assertEqual(sorted(diffs), ["B002", "B003", "B004"])

        # 金额不一致：双方金额都必须带上，说明由系统生成且提到双方金额
        mismatch = diffs["B002"]
        self.assertEqual(mismatch["kind"], "amount_mismatch")
        self.assertEqual(mismatch["left_amount"], 200.0)
        self.assertEqual(mismatch["right_amount"], 260.0)
        self.assertEqual(mismatch["status"], "pending")
        self.assertIsNone(mismatch["note"])
        for token in ("机构A", "机构B", "200.00", "260.00", "60.00"):
            self.assertIn(token, mismatch["description"])

        # 仅单方入账：另一侧金额为 null，说明指出哪方缺笔
        left_only = diffs["B003"]
        self.assertEqual(left_only["kind"], "left_only")
        self.assertEqual(left_only["left_amount"], 300.0)
        self.assertIsNone(left_only["right_amount"])
        self.assertIn("机构B 无此笔", left_only["description"])

        right_only = diffs["B004"]
        self.assertEqual(right_only["kind"], "right_only")
        self.assertIsNone(right_only["left_amount"])
        self.assertEqual(right_only["right_amount"], 400.0)
        self.assertIn("机构A 无此笔", right_only["description"])

    def test_empty_differences_when_books_agree(self):
        run = self.create(_items(("B001", 100), ("B002", 200)),
                          _items(("B001", 100), ("B002", 200)))
        self.assertEqual(run["differences"], [])
        self.assertEqual(run["matched_count"], 2)
        # 空差异可直接关闭
        closed = self.rig.reconciliations.close(INST_A, run["run_id"])
        self.assertEqual(closed["status"], "closed")

    def test_differences_deterministic_across_rereads(self):
        run = self.create_with_differences()
        again = self.rig.reconciliations.get_run(INST_A, run["run_id"])
        self.assertEqual(run["differences"], again["differences"])


class ConfirmationTests(ReconciliationCase):
    def setUp(self) -> None:
        super().setUp()
        self.run = self.create_with_differences()
        self.run_id = self.run["run_id"]

    def test_confirm_persists_per_item(self):
        result = self.rig.reconciliations.confirm_item(
            INST_A, self.run_id, "B002", note="机构B 含手续费 60，已核实")
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["confirmed_by"], INST_A.institution_id)
        self.assertIsNotNone(result["confirmed_at"])

        # 重新读取：确认与补充说明已落库，其余项仍待确认
        run = self.rig.reconciliations.get_run(INST_A, self.run_id)
        diffs = {d["business_no"]: d for d in run["differences"]}
        self.assertEqual(diffs["B002"]["note"], "机构B 含手续费 60，已核实")
        self.assertEqual(diffs["B002"]["status"], "confirmed")
        self.assertEqual(diffs["B003"]["status"], "pending")
        self.assertEqual(run["pending_count"], 2)
        # 确认不改写双方金额与系统说明
        self.assertEqual(diffs["B002"]["left_amount"], 200.0)
        self.assertEqual(diffs["B002"]["right_amount"], 260.0)
        self.assertIn("双方金额不一致", diffs["B002"]["description"])

    def test_add_note_persists(self):
        self.rig.reconciliations.add_note(
            INST_A, self.run_id, "B003", note="  机构B 尚未入账，待下月补  ")
        run = self.rig.reconciliations.get_run(INST_A, self.run_id)
        diffs = {d["business_no"]: d for d in run["differences"]}
        self.assertEqual(diffs["B003"]["note"], "机构B 尚未入账，待下月补")
        self.assertEqual(diffs["B003"]["status"], "pending")

    def test_close_requires_every_item_confirmed(self):
        with self.assertRaises(StateError) as ctx:
            self.rig.reconciliations.close(INST_A, self.run_id)
        self.assertEqual(ctx.exception.detail["pending"],
                         ["B002", "B003", "B004"])

        for business_no in ("B002", "B003"):
            self.rig.reconciliations.confirm_item(INST_A, self.run_id,
                                                  business_no)
        with self.assertRaises(StateError):
            self.rig.reconciliations.close(INST_A, self.run_id)

        self.rig.reconciliations.confirm_item(INST_A, self.run_id, "B004")
        closed = self.rig.reconciliations.close(INST_A, self.run_id)
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["closed_by"], INST_A.institution_id)
        self.assertIsNotNone(closed["closed_at"])

    def test_confirmed_item_is_terminal(self):
        self.rig.reconciliations.confirm_item(INST_A, self.run_id, "B002")
        with self.assertRaises(StateError):
            self.rig.reconciliations.confirm_item(INST_A, self.run_id, "B002")
        with self.assertRaises(StateError):
            self.rig.reconciliations.add_note(INST_A, self.run_id, "B002",
                                              note="事后改说明")

    def test_closed_run_is_terminal(self):
        for business_no in ("B002", "B003", "B004"):
            self.rig.reconciliations.confirm_item(INST_A, self.run_id,
                                                  business_no)
        self.rig.reconciliations.close(INST_A, self.run_id)
        with self.assertRaises(StateError):
            self.rig.reconciliations.confirm_item(INST_A, self.run_id, "B002")
        with self.assertRaises(StateError):
            self.rig.reconciliations.close(INST_A, self.run_id)


class ValidationAndAccessTests(ReconciliationCase):
    def test_duplicate_business_no_rejected(self):
        with self.assertRaises(ValidationError):
            self.create(_items(("B001", 100), ("B001", 200)), _items())

    def test_non_numeric_amount_rejected(self):
        with self.assertRaises(ValidationError):
            self.create(_items(("B001", "一百")), _items())
        with self.assertRaises(ValidationError):
            self.create(_items(("B001", True)), _items())

    def test_same_institution_rejected(self):
        with self.assertRaises(ValidationError):
            self.create(_items(), _items(), right_institution="机构A")

    def test_invalid_period_rejected(self):
        with self.assertRaises(ValidationError):
            self.create(_items(), _items(), period="2026-8")

    def test_empty_note_rejected(self):
        run = self.create_with_differences()
        with self.assertRaises(ValidationError):
            self.rig.reconciliations.add_note(INST_A, run["run_id"], "B002",
                                              note="   ")

    def test_reconcile_requires_grant(self):
        with self.assertRaises(PermissionDeniedError):
            self.create(_items(), _items(), principal=INST_B)
        # 主管单位无需授权
        run = self.create(_items(), _items(), principal=SUPERVISOR)
        self.assertEqual(run["status"], "open")

    def test_unknown_run_and_item_404(self):
        with self.assertRaises(NotFoundError):
            self.rig.reconciliations.get_run(INST_A, "recon-9999")
        run = self.create_with_differences()
        with self.assertRaises(NotFoundError):
            self.rig.reconciliations.confirm_item(INST_A, run["run_id"],
                                                  "B999")


class ReconciliationHttpTests(unittest.TestCase):
    """接口层：空差异与完整对账流。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="svc09252-recon-")
        self.app = make_app(f"{self._tmp.name}/api.db")
        status, _ = call(self.app, "POST", "/grants", {
            "institution_id": "机构A", "project_id": "P1",
            "category": "*", "permission": "reconcile",
        }, SUP_HEADERS)
        assert status == 201

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _create(self, left: list[dict], right: list[dict]):
        return call(self.app, "POST", "/reconciliations", {
            "project_id": "P1", "left_institution": "机构A",
            "right_institution": "机构B", "period": "2026-08",
            "left_items": left, "right_items": right,
        }, INST_A_HEADERS)

    def test_empty_differences_via_api(self) -> None:
        """双方账目完全一致：接口返回空差异，且可直接关闭。"""
        items = _items(("B001", 100), ("B002", 200))
        status, body = self._create(items, [dict(item) for item in items])
        self.assertEqual(status, 201, body)
        self.assertEqual(body["differences"], [])
        self.assertEqual(body["difference_count"], 0)
        self.assertEqual(body["matched_count"], 2)
        self.assertEqual(body["status"], "open")

        status, fetched = call(self.app, "GET",
                               f"/reconciliations/{body['run_id']}",
                               headers=INST_A_HEADERS)
        self.assertEqual(status, 200, fetched)
        self.assertEqual(fetched["differences"], [])

        status, closed = call(self.app, "POST",
                              f"/reconciliations/{body['run_id']}/close", {},
                              INST_A_HEADERS)
        self.assertEqual(status, 200, closed)
        self.assertEqual(closed["status"], "closed")

    def test_full_reconciliation_flow_via_api(self) -> None:
        status, run = self._create(
            _items(("B001", 100), ("B002", 200), ("B003", 300)),
            _items(("B001", 100), ("B002", 260), ("B004", 400)),
        )
        self.assertEqual(status, 201, run)
        run_id = run["run_id"]
        diffs = {d["business_no"]: d for d in run["differences"]}
        # 差异项带双方金额与系统生成说明
        self.assertEqual(diffs["B002"]["left_amount"], 200.0)
        self.assertEqual(diffs["B002"]["right_amount"], 260.0)
        self.assertIn("双方金额不一致", diffs["B002"]["description"])

        # 未逐项确认前不能关闭（不静默取一边的值）
        status, blocked = call(self.app, "POST",
                               f"/reconciliations/{run_id}/close", {},
                               INST_A_HEADERS)
        self.assertEqual(status, 409, blocked)
        self.assertEqual(blocked["error"], "state_error")

        # 补充说明 + 逐项确认
        status, noted = call(
            self.app, "POST",
            f"/reconciliations/{run_id}/items/B002/note",
            {"note": "机构B 含手续费 60"}, INST_A_HEADERS)
        self.assertEqual(status, 200, noted)
        self.assertEqual(noted["note"], "机构B 含手续费 60")

        for business_no in ("B002", "B003", "B004"):
            status, confirmed = call(
                self.app, "POST",
                f"/reconciliations/{run_id}/items/{business_no}/confirm",
                {}, INST_A_HEADERS)
            self.assertEqual(status, 200, confirmed)
            self.assertEqual(confirmed["status"], "confirmed")

        status, closed = call(self.app, "POST",
                              f"/reconciliations/{run_id}/close", {},
                              INST_A_HEADERS)
        self.assertEqual(status, 200, closed)
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["pending_count"], 0)

    def test_reconcile_requires_grant_via_api(self) -> None:
        status, body = call(self.app, "POST", "/reconciliations", {
            "project_id": "P1", "left_institution": "机构X",
            "right_institution": "机构Y", "period": "2026-08",
            "left_items": [], "right_items": [],
        }, {"X-Institution-Id": "陌生机构"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "permission_denied")


if __name__ == "__main__":
    unittest.main()
