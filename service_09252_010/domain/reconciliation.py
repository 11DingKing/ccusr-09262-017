"""跨机构数据对账的纯领域逻辑。

核心约束：
- 同一业务编号两边金额不一致时，必须逐项列出，且差异项**同时保留双方金额**，
  调用方只能看到并列的双方数据与差额，不能静默取其中一边的值；
- 差异说明（description）由本模块用 Python 生成，接口层不接受外部传入说明；
- 一方缺失记录同样是差异（missing_left / missing_right），不得按 0 或按对方金额补平。

本模块不接触数据库与 HTTP，便于独立测试。
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import ValidationError
from .models import ReconItemStatus

MATCH = ReconItemStatus.MATCH.value
AMOUNT_MISMATCH = ReconItemStatus.AMOUNT_MISMATCH.value
MISSING_LEFT = ReconItemStatus.MISSING_LEFT.value
MISSING_RIGHT = ReconItemStatus.MISSING_RIGHT.value

# 需业务人员逐项确认的差异状态。
DISCREPANCY_STATUSES: tuple[str, ...] = (
    AMOUNT_MISMATCH,
    MISSING_LEFT,
    MISSING_RIGHT,
)
_ALL_STATUSES = tuple(status.value for status in ReconItemStatus)


@dataclass(frozen=True)
class ReconciliationItem:
    """一个业务编号的逐项对账结果。金额缺侧为 None，绝不以 0 顶替。"""

    biz_no: str
    left_amount: float | None
    right_amount: float | None
    diff: float | None  # 仅双方都有记录时可计算：left - right
    status: str
    description: str  # Python 生成的说明字段

    @property
    def is_discrepancy(self) -> bool:
        return self.status in DISCREPANCY_STATUSES


def _normalize_entries(entries: object, side_label: str) -> dict[str, float]:
    """把一侧账目归一成 biz_no -> amount，并做形态校验。"""
    if not isinstance(entries, list) or not entries:
        raise ValidationError(f"{side_label}账目记录必须为非空列表")
    result: dict[str, float] = {}
    for index, raw in enumerate(entries):
        where = f"{side_label}账目记录[{index}]"
        if not isinstance(raw, dict):
            raise ValidationError(f"{where}: 必须为对象")
        biz_no = raw.get("biz_no")
        if not isinstance(biz_no, str) or not biz_no.strip():
            raise ValidationError(f"{where}: biz_no 缺失或非法")
        biz_no = biz_no.strip()
        amount = raw.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise ValidationError(f"{where}: 业务 {biz_no} 的 amount 必须为数值")
        if biz_no in result:
            raise ValidationError(f"{where}: 业务编号 {biz_no} 在同侧账目中重复")
        result[biz_no] = float(amount)
    return result


def _format_amount(value: float | None) -> str:
    if value is None:
        return "无记录"
    return f"{value:.2f}"


def _describe(item: "ReconciliationItem", left_label: str,
              right_label: str) -> str:
    """逐项说明：任何情况下都把双方金额并列写出。"""
    biz = item.biz_no
    left_text = _format_amount(item.left_amount)
    right_text = _format_amount(item.right_amount)
    if item.status == AMOUNT_MISMATCH:
        return (
            f"业务 {biz} 双方金额不一致：{left_label} 金额 {left_text}，"
            f"{right_label} 金额 {right_text}，"
            f"差额（{left_label}-{right_label}）{_format_amount(item.diff)}；"
            f"双方金额均已保留，未取任一方账值为准"
        )
    if item.status == MISSING_LEFT:
        return (
            f"业务 {biz} {left_label}无记录，{right_label} 金额 {right_text}；"
            f"不得以对方金额或 0 静默补平"
        )
    if item.status == MISSING_RIGHT:
        return (
            f"业务 {biz} {right_label}无记录，{left_label} 金额 {left_text}；"
            f"不得以对方金额或 0 静默补平"
        )
    return (
        f"业务 {biz} 双方金额一致：{left_label} 与 {right_label} 均为 {left_text}"
    )


def build_items(left_entries: object, right_entries: object, *,
                left_label: str, right_label: str) -> list[ReconciliationItem]:
    """按业务编号归并两侧账目，逐项产出对账结果（按 biz_no 排序）。"""
    if not isinstance(left_label, str) or not left_label.strip():
        raise ValidationError("left_institution 缺失")
    if not isinstance(right_label, str) or not right_label.strip():
        raise ValidationError("right_institution 缺失")
    if left_label.strip() == right_label.strip():
        raise ValidationError("跨机构对账的双方机构不能相同")
    left = _normalize_entries(left_entries, f"{left_label}（左账）")
    right = _normalize_entries(right_entries, f"{right_label}（右账）")

    items: list[ReconciliationItem] = []
    for biz_no in sorted(left.keys() | right.keys()):
        in_left, in_right = biz_no in left, biz_no in right
        left_amount = left.get(biz_no)
        right_amount = right.get(biz_no)
        if in_left and in_right:
            if left_amount == right_amount:
                status = MATCH
                diff = 0.0
            else:
                status = AMOUNT_MISMATCH
                diff = left_amount - right_amount
        elif in_right:
            status = MISSING_LEFT
            diff = None
        else:
            status = MISSING_RIGHT
            diff = None
        provisional = ReconciliationItem(
            biz_no=biz_no,
            left_amount=left_amount,
            right_amount=right_amount,
            diff=diff,
            status=status,
            description="",
        )
        items.append(
            ReconciliationItem(
                biz_no=biz_no,
                left_amount=left_amount,
                right_amount=right_amount,
                diff=diff,
                status=status,
                description=_describe(provisional, left_label, right_label),
            )
        )
    return items


def summarize(items: list[ReconciliationItem]) -> dict:
    """对账汇总：总数、一致数、差异数与按状态计数。"""
    by_status = {status: 0 for status in _ALL_STATUSES}
    for item in items:
        by_status[item.status] += 1
    discrepancies = sum(by_status[s] for s in DISCREPANCY_STATUSES)
    return {
        "total": len(items),
        "matched": by_status[MATCH],
        "discrepancies": discrepancies,
        "by_status": by_status,
    }
