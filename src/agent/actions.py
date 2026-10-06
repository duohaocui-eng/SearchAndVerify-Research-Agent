"""动作空间：Agent 每一步能做的选择（对应 Q12）。

把动作显式建模成数据结构而不是散落的 if/else，有三个好处：
- Planner 可以被替换（规则版 / LLM 版）而循环体不变；
- 每个动作都能原样写进 trace，让轨迹真正可回放；
- 消融时「禁掉某类动作」变成一行过滤，而不是改控制流。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 动作类型
PROBE_SOURCES = "probe_sources"           # 探测数据源对该癌种的覆盖
GENERATE_CANDIDATES = "generate_candidates"
FETCH_EVIDENCE = "fetch_evidence"         # 取某候选某维度的证据
REWRITE_QUERY = "rewrite_query"           # 改写查询后重取（文献源为主）
SWITCH_SOURCE = "switch_source"           # 换一个源取同一维度
EXPAND_CANDIDATES = "expand_candidates"   # 基于已有发现扩展候选池
RESOLVE_CONFLICT = "resolve_conflict"     # 针对冲突补取仲裁证据
KILL_CANDIDATE = "kill_candidate"         # early-kill
SHORTLIST = "shortlist"                   # 圈定短名单，进入深度取证
STOP = "stop"

# 只有「动态重规划」开启时才可用的动作。
# 关掉 dynamic_replanning 后 Agent 退化为固定顺序执行，即固定工作流基线（Q55）。
DYNAMIC_ACTIONS = {REWRITE_QUERY, SWITCH_SOURCE, EXPAND_CANDIDATES, RESOLVE_CONFLICT}


@dataclass
class Action:
    """一个决策。reason 是必填的——说不出理由的决策没有价值，也无法评测。"""

    kind: str
    reason: str
    target: str = ""
    dimension: str = ""
    source_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = {"kind": self.kind, "reason": self.reason}
        for key in ("target", "dimension", "source_id"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.payload:
            out["payload"] = self.payload
        return out

    def describe(self) -> str:
        bits = [self.kind]
        if self.target:
            bits.append(self.target)
        if self.dimension:
            bits.append(self.dimension)
        if self.source_id:
            bits.append(f"via {self.source_id}")
        return " · ".join(bits)


@dataclass
class ActionResult:
    """动作执行结果。ok=False 不等于失败退出——Agent 会据此换源或改写查询。"""

    action: Action
    ok: bool
    detail: str = ""
    evidence_added: int = 0
    cost: int = 0
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.to_dict(),
            "ok": self.ok,
            "detail": self.detail,
            "evidence_added": self.evidence_added,
            "cost": self.cost,
            **({"payload": self.payload} if self.payload else {}),
        }
