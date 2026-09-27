"""跨机构对账的领域计算：逐项比对双方账目并生成差异说明。

核心约束：同一业务编号两边金额对不上时，差异项始终同时携带双方金额，
说明文字由系统（Python）生成；系统绝不静默采用任一方数值，
差异的消解只能依靠业务人员逐项确认。
"""
from __future__ import annotations

# 差异类型
KIND_AMOUNT_MISMATCH = "amount_mismatch"  # 双方都有该业务编号但金额不一致
KIND_LEFT_ONLY = "left_only"  # 仅左方入账
KIND_RIGHT_ONLY = "right_only"  # 仅右方入账


def compute_differences(left_items: dict[str, float],
                        right_items: dict[str, float],
                        left_label: str,
                        right_label: str) -> tuple[list[dict], int]:
    """逐项比对双方账目。

    left_items / right_items 为 {业务编号: 金额}。返回 (差异列表, 一致笔数)：
    金额一致的只计数不形成差异项；每条差异都带双方金额与系统生成的说明，
    按业务编号排序，保证同一输入必得同一份差异清单。
    """
    differences: list[dict] = []
    matched = 0
    for business_no in sorted(set(left_items) | set(right_items)):
        left_amount = left_items.get(business_no)
        right_amount = right_items.get(business_no)
        if left_amount is not None and right_amount is not None:
            if left_amount == right_amount:
                matched += 1
                continue
            kind = KIND_AMOUNT_MISMATCH
        elif left_amount is not None:
            kind = KIND_LEFT_ONLY
        else:
            kind = KIND_RIGHT_ONLY
        differences.append({
            "business_no": business_no,
            "left_amount": left_amount,
            "right_amount": right_amount,
            "kind": kind,
            "description": describe_difference(
                kind, business_no, left_label, right_label,
                left_amount, right_amount),
        })
    return differences, matched


def describe_difference(kind: str, business_no: str,
                        left_label: str, right_label: str,
                        left_amount: float | None,
                        right_amount: float | None) -> str:
    """系统生成的差异说明：说清双方各入账多少、差在何处、不得静默取值。"""
    if kind == KIND_AMOUNT_MISMATCH:
        assert left_amount is not None and right_amount is not None
        delta = left_amount - right_amount
        direction = f"{left_label} 多计" if delta > 0 else f"{left_label} 少计"
        return (
            f"业务编号 {business_no} 双方金额不一致：{left_label} 入账 "
            f"{left_amount:.2f}，{right_label} 入账 {right_amount:.2f}，"
            f"差额 {abs(delta):.2f}（{direction}）；需双方核实并逐项确认，"
            f"系统不取任一方数值。"
        )
    if kind == KIND_LEFT_ONLY:
        assert left_amount is not None
        return (
            f"业务编号 {business_no} 仅 {left_label} 入账 {left_amount:.2f}，"
            f"{right_label} 无此笔；需核实是否漏记，系统不自动补记。"
        )
    assert right_amount is not None
    return (
        f"业务编号 {business_no} 仅 {right_label} 入账 {right_amount:.2f}，"
        f"{left_label} 无此笔；需核实是否漏记，系统不自动补记。"
    )
