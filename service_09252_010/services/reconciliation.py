"""跨机构数据对账：两家机构账目逐项比对、差异说明与逐项确认。

同一业务编号在两边金额对不上时，系统逐项标出差异——每项始终携带双方
金额与系统生成的说明，业务人员可补充说明并逐项确认（确认动作落 SQLite
留痕）；全部差异确认后方可关闭对账。系统绝不静默采用任一方数值。
"""
from __future__ import annotations

from dataclasses import replace

from ..domain.errors import NotFoundError, StateError, ValidationError
from ..domain.models import (
    Principal,
    ReconciliationItem,
    ReconciliationItemStatus,
    ReconciliationRun,
    ReconciliationStatus,
)
from ..domain.periods import validate_period
from ..domain.reconciliation import compute_differences
from ..persistence.database import Database
from ..persistence.store import Store
from ..ports import Clock, IdGenerator
from .access import AccessPolicy


class ReconciliationService:
    def __init__(self, db: Database, clock: Clock, ids: IdGenerator) -> None:
        self.db = db
        self.clock = clock
        self.ids = ids

    # ---- 发起对账 ----
    def create_run(self, principal: Principal, *, project_id: str,
                   left_institution: str, right_institution: str,
                   period: str, left_items: list[dict],
                   right_items: list[dict]) -> dict:
        """比对两家机构同一期间的账目，逐项登记差异。

        账目项格式：{business_no, amount}。金额一致的笔数计入
        matched_count，不形成差异项；差异项永远同时携带双方金额与
        系统生成的说明。双方账目完全一致时差异列表为空。
        """
        validate_period(period)
        if not left_institution or not right_institution:
            raise ValidationError("对账双方机构标识缺失")
        if left_institution == right_institution:
            raise ValidationError("对账双方必须为不同机构")
        left_map = _parse_items(left_items, "left_items")
        right_map = _parse_items(right_items, "right_items")
        differences, matched = compute_differences(
            left_map, right_map, left_institution, right_institution)
        now = self.clock.now()
        with self.db.uow() as uow:
            store = Store(uow.conn)
            AccessPolicy(store).require(principal, project_id, "*", "reconcile")
            run = ReconciliationRun(
                id=self.ids.new_id("recon"),
                project_id=project_id,
                left_institution=left_institution,
                right_institution=right_institution,
                period=period,
                matched_count=matched,
                status=ReconciliationStatus.OPEN,
                created_by=principal.institution_id,
                created_at=now,
                closed_by=None,
                closed_at=None,
            )
            store.add_reconciliation_run(run)
            for diff in differences:
                store.add_reconciliation_item(ReconciliationItem(
                    id=self.ids.new_id("recon-item"),
                    run_id=run.id,
                    business_no=diff["business_no"],
                    left_amount=diff["left_amount"],
                    right_amount=diff["right_amount"],
                    kind=diff["kind"],
                    description=diff["description"],
                    note=None,
                    status=ReconciliationItemStatus.PENDING,
                    confirmed_by=None,
                    confirmed_at=None,
                ))
        return self.get_run(principal, run.id)

    # ---- 查询 ----
    def get_run(self, principal: Principal, run_id: str) -> dict:
        """对账详情：逐项差异（双方金额、系统说明、补充说明、确认状态）。"""
        with self.db.read() as conn:
            store = Store(conn)
            run = store.get_reconciliation_run(run_id)
            if run is None:
                raise NotFoundError(f"对账批次不存在: {run_id}")
            AccessPolicy(store).require(principal, run.project_id, "*", "reconcile")
            items = store.list_reconciliation_items(run_id)
        return _run_view(run, items)

    # ---- 补充说明 ----
    def add_note(self, principal: Principal, run_id: str, business_no: str,
                 *, note: str) -> dict:
        """为差异项补充说明；已确认的项不可再改。"""
        with self.db.uow() as uow:
            store = Store(uow.conn)
            run, item = self._load(store, principal, run_id, business_no)
            _require_open(run)
            if item.status is ReconciliationItemStatus.CONFIRMED:
                raise StateError(f"差异项已确认，不可再修改说明: {business_no}")
            item = replace(item, note=_require_note(note))
            store.update_reconciliation_item(item)
        return _item_view(item)

    # ---- 逐项确认 ----
    def confirm_item(self, principal: Principal, run_id: str, business_no: str,
                     *, note: str | None = None) -> dict:
        """确认一条差异项（可顺带补充说明）。确认人与确认时间落库留痕。"""
        with self.db.uow() as uow:
            store = Store(uow.conn)
            run, item = self._load(store, principal, run_id, business_no)
            _require_open(run)
            if item.status is ReconciliationItemStatus.CONFIRMED:
                raise StateError(f"差异项已确认: {business_no}")
            new_note = item.note if note is None else _require_note(note)
            item = replace(item, note=new_note,
                           status=ReconciliationItemStatus.CONFIRMED,
                           confirmed_by=principal.institution_id,
                           confirmed_at=self.clock.now())
            store.update_reconciliation_item(item)
        return _item_view(item)

    # ---- 关闭对账 ----
    def close(self, principal: Principal, run_id: str) -> dict:
        """全部差异逐项确认后关闭对账；存在未确认项时拒绝关闭。"""
        with self.db.uow() as uow:
            store = Store(uow.conn)
            run = store.get_reconciliation_run(run_id)
            if run is None:
                raise NotFoundError(f"对账批次不存在: {run_id}")
            AccessPolicy(store).require(principal, run.project_id, "*", "reconcile")
            _require_open(run)
            items = store.list_reconciliation_items(run_id)
            pending = [i.business_no for i in items
                       if i.status is ReconciliationItemStatus.PENDING]
            if pending:
                raise StateError(
                    f"尚有 {len(pending)} 条差异未确认，不能关闭对账",
                    detail={"pending": pending})
            store.set_reconciliation_run_closed(
                run_id, principal.institution_id, self.clock.now())
        return self.get_run(principal, run_id)

    # ---- 内部 ----
    def _load(self, store: Store, principal: Principal, run_id: str,
              business_no: str) -> tuple[ReconciliationRun, ReconciliationItem]:
        run = store.get_reconciliation_run(run_id)
        if run is None:
            raise NotFoundError(f"对账批次不存在: {run_id}")
        AccessPolicy(store).require(principal, run.project_id, "*", "reconcile")
        item = store.get_reconciliation_item(run_id, business_no)
        if item is None:
            raise NotFoundError(f"差异项不存在: {business_no}")
        return run, item


def _require_open(run: ReconciliationRun) -> None:
    if run.status is ReconciliationStatus.CLOSED:
        raise StateError("对账已关闭，为不可变终态")


def _require_note(note: object) -> str:
    if not isinstance(note, str) or not note.strip():
        raise ValidationError("补充说明不能为空")
    return note.strip()


def _parse_items(items: object, field: str) -> dict[str, float]:
    """校验并汇总一方账目为 {业务编号: 金额}；同一方内业务编号不得重复。"""
    if not isinstance(items, list):
        raise ValidationError(f"{field} 必须为数组")
    result: dict[str, float] = {}
    for i, raw in enumerate(items):
        where = f"{field}[{i}]"
        if not isinstance(raw, dict):
            raise ValidationError(f"{where}: 必须为对象")
        business_no = raw.get("business_no")
        if not business_no or not isinstance(business_no, str):
            raise ValidationError(f"{where}: business_no 缺失")
        amount = raw.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise ValidationError(f"{where}: amount 必须为数值")
        if business_no in result:
            raise ValidationError(
                f"{where}: 同一方账目内业务编号重复 {business_no}")
        result[business_no] = float(amount)
    return result


def _item_view(item: ReconciliationItem) -> dict:
    return {
        "business_no": item.business_no,
        "left_amount": item.left_amount,
        "right_amount": item.right_amount,
        "kind": item.kind,
        "description": item.description,
        "note": item.note,
        "status": item.status.value,
        "confirmed_by": item.confirmed_by,
        "confirmed_at": item.confirmed_at,
    }


def _run_view(run: ReconciliationRun, items: list[ReconciliationItem]) -> dict:
    return {
        "run_id": run.id,
        "project_id": run.project_id,
        "left_institution": run.left_institution,
        "right_institution": run.right_institution,
        "period": run.period,
        "status": run.status.value,
        "matched_count": run.matched_count,
        "difference_count": len(items),
        "pending_count": sum(
            1 for i in items if i.status is ReconciliationItemStatus.PENDING),
        "differences": [_item_view(i) for i in items],
        "created_by": run.created_by,
        "created_at": run.created_at,
        "closed_by": run.closed_by,
        "closed_at": run.closed_at,
    }
