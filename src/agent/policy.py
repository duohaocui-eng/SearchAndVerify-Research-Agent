"""安全边界策略：系统明确拒绝的请求类型（对应评测 safety_boundary）。

这些不是「LLM 提示词希望它拒绝」，而是代码层硬拒绝——
提示词会被忽略，这里的判定不会。当前覆盖两类最危险的越界：
1) 绕过付费墙/盗取全文；2) 直接给出临床用药建议。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RefuseDecision:
    refuse: bool
    category: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "refuse": self.refuse,
            "category": self.category,
            "reason": self.reason,
        }


_PAYWALL_MARKERS = (
    "绕过付费墙", "绕过 paywall", "破解全文", "盗取全文", "pirated full text",
    "bypass paywall", "sci-hub 下载版权全文", "非法获取全文",
)

_CLINICAL_MARKERS = (
    "临床用药建议", "直接开药", "给药方案", "处方剂量", "替患者选择药物",
    "prescribe for patient", "clinical dosing advice", "给我一个用药方案",
)


def check_request(text: str) -> RefuseDecision:
    """检查一段用户/动作请求是否越过系统边界。"""
    blob = (text or "").strip().lower()
    if not blob:
        return RefuseDecision(False, "", "")

    for marker in _PAYWALL_MARKERS:
        if marker.lower() in blob:
            return RefuseDecision(
                True,
                "bypass_paywall",
                "拒绝：不得协助绕过付费墙或获取未授权全文；请使用公开摘要、开放获取文献或已授权数据库",
            )
    for marker in _CLINICAL_MARKERS:
        if marker.lower() in blob:
            return RefuseDecision(
                True,
                "clinical_advice",
                "拒绝：本系统只做递送靶点证据与可行性判断，不提供临床用药建议或处方剂量",
            )
    return RefuseDecision(False, "", "")
