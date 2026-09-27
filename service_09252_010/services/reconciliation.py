"""跨机构数据对账服务。

- 创建对账时两侧原始账目逐项比对：金额不一致与单侧缺记录均标为差异，
  差异行**同时固化双方金额**（缺侧为 None）与 Python 生成的说明字段，
  系统不选择、也不回写任何一方的金额；
- 差异逐项落 SQLite，业务人员可逐项确认并补充说明；
- 一致项无需确认（confirm_status 为 None）。
"""
from __future__ import annotations

from ..domain import reconciliation as domain
from ..domain.errors import (
    NotFoundError,
    PermissionDeniedError,
    StateError,
    ValidationError,
)
from ..domain.models import (
    Principal,
    ReconciliationLine,
    ReconciliationRun,
    ReconConfirmStatus,
    ReconItemStatus,
)
from ..persistence.database import Database
from ..persistence.store import Store
from ..ports import Clock, IdGenerator

# 允许在差异项上流转的确认状态；open 为初始态，不可由接口设置。
_CONFIRM_TRANSITIONS: dict[ReconConfirmStatus, set[ReconConfirmStatus]] = {
    ReconConfirmStatus.OPEN: {ReconConfirmStatus.CONFIRMED,
                              ReconConfirmStatus.RESOLVED},
    ReconConfirmStatus.CONFIRMED: {ReconConfirmStatus.RESOLVED},
    ReconConfirmStatus.RESOLVED: set(),
}


class ReconciliationService:
    def __init__(self, db: Database, clock: Clock, ids: IdGenerator) -> None:
        self.db = db
        self.clock = clock
        self.ids = ids

    def create(self, principal: Principal, *, left_institution: str,
               right_institution: str, left_entries: list[dict],
               right_entries: list[dict]) -> dict:
        """执行一次跨机构对账并逐项持久化。"""
        if not isinstance(left_institution, str) or not isinstance(
                right_institution, str):
            raise ValidationError("left_institution / right_institution 必须为字符串")
        left_institution = left_institution.strip()
        right_institution = right_institution.strip()
        self._require_participant(principal, left_institution, right_institution)
        items = domain.build_items(
            left_entries, right_entries,
            left_label=left_institution, right_label=right_institution,
        )
        stats = domain.summarize(items)
        now = self.clock.now()
        with self.db.uow() as uow:
            store = Store(uow.conn)
            run = ReconciliationRun(
                id=self.ids.new_id("recon"),
                left_institution=left_institution.strip(),
                right_institution=right_institution.strip(),
                created_by=principal.institution_id,
                created_at=now,
                total=stats["total"],
                matched=stats["matched"],
                discrepancies=stats["discrepancies"],
            )
            store.add_recon_run(run)
            lines = [
                ReconciliationLine(
                    id=self.ids.new_id("reconline"),
                    run_id=run.id,
                    biz_no=item.biz_no,
                    left_amount=item.left_amount,
                    right_amount=item.right_amount,
                    diff=item.diff,
                    status=ReconItemStatus(item.status),
                    description=item.description,
                    confirm_status=(
                        ReconConfirmStatus.OPEN if item.is_discrepancy else None
                    ),
                    note=None,
                    confirmed_by=None,
                    confirmed_at=None,
                )
                for item in items
            ]
            store.add_recon_lines(lines)
        return self.get_run(principal, run.id)

    def get_run(self, principal: Principal, run_id: str,
                only_discrepancies: bool = False) -> dict:
        with self.db.read() as conn:
            store = Store(conn)
            run = store.get_recon_run(run_id)
            if run is None:
                raise NotFoundError(f"对账任务不存在: {run_id}")
            self._require_participant(
                principal, run.left_institution, run.right_institution
            )
            lines = store.list_recon_lines(
                run_id, only_discrepancies=only_discrepancies
            )
        return {
            "run_id": run.id,
            "left_institution": run.left_institution,
            "right_institution": run.right_institution,
            "created_by": run.created_by,
            "created_at": run.created_at,
            "summary": {
                "total": run.total,
                "matched": run.matched,
                "discrepancies": run.discrepancies,
            },
            "lines": [_line_to_dict(line) for line in lines],
        }

    def confirm_line(self, principal: Principal, run_id: str, biz_no: str, *,
                     note: str | None = None,
                     status: str = ReconConfirmStatus.CONFIRMED.value) -> dict:
        """逐项确认差异并补充说明。只有差异项可确认，状态只能向前流转。"""
        try:
            target = ReconConfirmStatus(status)
        except ValueError:
            raise ValidationError(
                "确认状态仅支持 confirmed / resolved"
            ) from None
        if target is ReconConfirmStatus.OPEN:
            raise ValidationError("差异项不能被重置为 open")
        if note is not None and (not isinstance(note, str) or not note.strip()):
            raise ValidationError("补充说明为空时应省略 note 字段")
        with self.db.uow() as uow:
            store = Store(uow.conn)
            run = store.get_recon_run(run_id)
            if run is None:
                raise NotFoundError(f"对账任务不存在: {run_id}")
            self._require_participant(
                principal, run.left_institution, run.right_institution
            )
            line = store.get_recon_line(run_id, biz_no)
            if line is None:
                raise NotFoundError(
                    f"对账 {run_id} 中不存在业务编号 {biz_no}"
                )
            if line.status is ReconItemStatus.MATCH:
                raise StateError("双方金额一致的账目项无需逐项确认")
            current = line.confirm_status or ReconConfirmStatus.OPEN
            allowed = _CONFIRM_TRANSITIONS[current]
            if target not in allowed:
                raise StateError(
                    f"差异项当前为 {current.value}，不可流转到 {target.value}"
                )
            store.update_recon_line_confirmation(
                line.id,
                confirm_status=target,
                note=note.strip() if note else line.note,
                confirmed_by=principal.institution_id,
                confirmed_at=self.clock.now(),
            )
            updated = store.get_recon_line(run_id, biz_no)
            assert updated is not None
        return _line_to_dict(updated)

    @staticmethod
    def _require_participant(principal: Principal, left_institution: str,
                             right_institution: str) -> None:
        if principal.is_supervisor:
            return
        if principal.institution_id not in (left_institution, right_institution):
            raise PermissionDeniedError(
                f"机构 {principal.institution_id} 不是本次对账的参与方"
                f"（{left_institution} / {right_institution}）"
            )


def _line_to_dict(line: ReconciliationLine) -> dict:
    """对外结构：任何状态都并列双方金额，不存在『取一边』的字段。"""
    return {
        "biz_no": line.biz_no,
        "left_amount": line.left_amount,
        "right_amount": line.right_amount,
        "diff": line.diff,
        "status": line.status.value,
        "is_discrepancy": line.status is not ReconItemStatus.MATCH,
        "description": line.description,
        "confirm_status": (
            line.confirm_status.value if line.confirm_status is not None else None
        ),
        "note": line.note,
        "confirmed_by": line.confirmed_by,
        "confirmed_at": line.confirmed_at,
    }
