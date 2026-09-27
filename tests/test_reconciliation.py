"""跨机构数据对账：逐项差异、双方金额并列、Python 说明与逐项确认。"""
from __future__ import annotations

from service_09252_010.domain.errors import (
    NotFoundError,
    PermissionDeniedError,
    StateError,
    ValidationError,
)
from service_09252_010.domain.models import Principal
from service_09252_010.persistence.store import Store

from support import INST_A, INST_B, RigTestCase, SUPERVISOR

INST_C = Principal(institution_id="机构C", role="officer")

LEFT = [
    {"biz_no": "BIZ-001", "amount": 100.0},
    {"biz_no": "BIZ-002", "amount": 200.0},
    {"biz_no": "BIZ-003", "amount": 300.0},  # 右账无此业务
]
RIGHT = [
    {"biz_no": "BIZ-001", "amount": 100.0},  # 一致
    {"biz_no": "BIZ-002", "amount": 250.0},  # 金额不一致
    {"biz_no": "BIZ-004", "amount": 400.0},  # 左账无此业务
]


class ReconciliationTests(RigTestCase):
    def _run(self, principal=SUPERVISOR, left=None, right=None):
        return self.rig.reconciliation.create(
            principal,
            left_institution="机构A",
            right_institution="机构B",
            left_entries=LEFT if left is None else left,
            right_entries=RIGHT if right is None else right,
        )

    def _line(self, result: dict, biz_no: str) -> dict:
        lines = {line["biz_no"]: line for line in result["lines"]}
        return lines[biz_no]

    def test_discrepancies_carry_both_amounts_and_python_description(self) -> None:
        result = self._run()
        self.assertEqual(result["summary"],
                         {"total": 4, "matched": 1, "discrepancies": 3})

        mismatch = self._line(result, "BIZ-002")
        self.assertEqual(mismatch["status"], "amount_mismatch")
        self.assertTrue(mismatch["is_discrepancy"])
        # 双方金额必须并列保留，差额与两边都能对上
        self.assertEqual(mismatch["left_amount"], 200.0)
        self.assertEqual(mismatch["right_amount"], 250.0)
        self.assertEqual(mismatch["diff"], -50.0)
        # 说明字段由 Python 生成，且同时写出双方金额
        self.assertIn("机构A", mismatch["description"])
        self.assertIn("机构B", mismatch["description"])
        self.assertIn("200.00", mismatch["description"])
        self.assertIn("250.00", mismatch["description"])
        self.assertIn("-50.00", mismatch["description"])
        self.assertIn("未取任一方账值为准", mismatch["description"])

        missing_right = self._line(result, "BIZ-003")
        self.assertEqual(missing_right["status"], "missing_right")
        self.assertEqual(missing_right["left_amount"], 300.0)
        self.assertIsNone(missing_right["right_amount"])
        self.assertIsNone(missing_right["diff"])
        self.assertIn("机构B无记录", missing_right["description"])
        self.assertIn("300.00", missing_right["description"])

        missing_left = self._line(result, "BIZ-004")
        self.assertEqual(missing_left["status"], "missing_left")
        self.assertIsNone(missing_left["left_amount"])
        self.assertEqual(missing_left["right_amount"], 400.0)
        self.assertIn("机构A无记录", missing_left["description"])

        matched = self._line(result, "BIZ-001")
        self.assertEqual(matched["status"], "match")
        self.assertFalse(matched["is_discrepancy"])
        self.assertIsNone(matched["confirm_status"])

    def test_lines_persisted_in_sqlite(self) -> None:
        result = self._run()
        run_id = result["run_id"]
        with self.rig.db.read() as conn:
            store = Store(conn)
            rows = store.list_recon_lines(run_id)
            by_biz = {row.biz_no: row for row in rows}
        self.assertEqual(len(rows), 4)
        row = by_biz["BIZ-002"]
        self.assertEqual(row.left_amount, 200.0)
        self.assertEqual(row.right_amount, 250.0)
        self.assertEqual(row.diff, -50.0)
        self.assertEqual(row.description, self._line(result, "BIZ-002")["description"])
        self.assertIsNone(row.note)

        only_discrepancies = self.rig.reconciliation.get_run(
            SUPERVISOR, run_id, only_discrepancies=True
        )
        self.assertEqual(
            sorted(line["biz_no"] for line in only_discrepancies["lines"]),
            ["BIZ-002", "BIZ-003", "BIZ-004"],
        )

    def test_confirm_each_discrepancy_with_note(self) -> None:
        result = self._run(principal=INST_A)
        run_id = result["run_id"]

        confirmed = self.rig.reconciliation.confirm_line(
            INST_B, run_id, "BIZ-002", note="汇率折算差 50，已核对原始凭证"
        )
        self.assertEqual(confirmed["confirm_status"], "confirmed")
        self.assertEqual(confirmed["note"], "汇率折算差 50，已核对原始凭证")
        self.assertEqual(confirmed["confirmed_by"], "机构B")
        self.assertIsNotNone(confirmed["confirmed_at"])
        # 确认动作不改任何金额
        self.assertEqual(confirmed["left_amount"], 200.0)
        self.assertEqual(confirmed["right_amount"], 250.0)

        fetched = self.rig.reconciliation.get_run(INST_A, run_id)
        stored = self._line(fetched, "BIZ-002")
        self.assertEqual(stored["confirm_status"], "confirmed")
        self.assertEqual(stored["note"], "汇率折算差 50，已核对原始凭证")

        # 可继续流转为 resolved
        resolved = self.rig.reconciliation.confirm_line(
            INST_A, run_id, "BIZ-002", status="resolved"
        )
        self.assertEqual(resolved["confirm_status"], "resolved")

    def test_confirm_matched_line_rejected(self) -> None:
        result = self._run()
        with self.assertRaises(StateError):
            self.rig.reconciliation.confirm_line(
                SUPERVISOR, result["run_id"], "BIZ-001", note="无需确认也试一下"
            )

    def test_resolved_is_terminal(self) -> None:
        result = self._run()
        run_id = result["run_id"]
        self.rig.reconciliation.confirm_line(
            SUPERVISOR, run_id, "BIZ-002", status="resolved"
        )
        with self.assertRaises(StateError):
            self.rig.reconciliation.confirm_line(
                SUPERVISOR, run_id, "BIZ-002", note="终态不可改"
            )

    def test_confirm_unknown_biz_not_found(self) -> None:
        result = self._run()
        with self.assertRaises(NotFoundError):
            self.rig.reconciliation.confirm_line(
                SUPERVISOR, result["run_id"], "BIZ-999", note="不存在的业务编号"
            )

    def test_confirm_bogus_status_rejected(self) -> None:
        result = self._run()
        with self.assertRaises(ValidationError):
            self.rig.reconciliation.confirm_line(
                SUPERVISOR, result["run_id"], "BIZ-002", status="bogus"
            )
        with self.assertRaises(ValidationError):
            self.rig.reconciliation.confirm_line(
                SUPERVISOR, result["run_id"], "BIZ-002", status="open"
            )

    def test_get_unknown_run_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.rig.reconciliation.get_run(SUPERVISOR, "recon-nope")

    def test_only_participating_institutions_may_access(self) -> None:
        result = self._run(principal=INST_A)
        run_id = result["run_id"]
        with self.assertRaises(PermissionDeniedError):
            self.rig.reconciliation.get_run(INST_C, run_id)
        with self.assertRaises(PermissionDeniedError):
            self.rig.reconciliation.confirm_line(
                INST_C, run_id, "BIZ-002", note="越权确认"
            )
        # 两个参与方与主管单位均可读
        self.rig.reconciliation.get_run(INST_A, run_id)
        self.rig.reconciliation.get_run(INST_B, run_id)
        self.rig.reconciliation.get_run(SUPERVISOR, run_id)

    def test_rejects_bad_input(self) -> None:
        # 双方机构相同
        with self.assertRaises(ValidationError):
            self.rig.reconciliation.create(
                SUPERVISOR, left_institution="机构A", right_institution="机构A",
                left_entries=LEFT, right_entries=RIGHT,
            )
        # 同侧业务编号重复
        with self.assertRaises(ValidationError):
            self.rig.reconciliation.create(
                SUPERVISOR, left_institution="机构A", right_institution="机构B",
                left_entries=LEFT + [{"biz_no": "BIZ-001", "amount": 9}],
                right_entries=RIGHT,
            )
        # 金额非数值
        with self.assertRaises(ValidationError):
            self.rig.reconciliation.create(
                SUPERVISOR, left_institution="机构A", right_institution="机构B",
                left_entries=[{"biz_no": "X", "amount": "100"}],
                right_entries=[{"biz_no": "X", "amount": 100}],
            )
        # 空账目
        with self.assertRaises(ValidationError):
            self.rig.reconciliation.create(
                SUPERVISOR, left_institution="机构A", right_institution="机构B",
                left_entries=[], right_entries=RIGHT,
            )
