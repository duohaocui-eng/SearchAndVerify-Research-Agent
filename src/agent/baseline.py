"""固定工作流基线（对应 Q9、Q29、Q55）。

这是 Agent 的对照组，也是回答「为什么不用普通脚本」时最有力的证据。
它刻意做成「穷举式」：对每个候选、每个维度、每个可用源，按固定顺序全查一遍，
不看状态、不识别缺口、不做冲突仲裁、不提前止损。

实现上复用 SearchAndVerifyAgent 的全部执行逻辑，只替换「怎么选下一个动作」。
这一点很重要：如果基线是另写一套代码，对照实验测出来的差异就可能来自实现差异
而不是决策方式差异，结论会不可信。
"""
from __future__ import annotations

from typing import Any

from .actions import FETCH_EVIDENCE, STOP, Action
from .gap_analysis import DEEP_DIMENSIONS, SCREENING_DIMENSIONS
from .loop import RunResult, SearchAndVerifyAgent
from .planner import apply_shortlist
from evidence_core.source_policy import allowed_sources

# 固定顺序：先筛选维度、再深度维度，写死不变
FIXED_DIMENSION_ORDER = list(dict.fromkeys(SCREENING_DIMENSIONS + DEEP_DIMENSIONS))


class FixedWorkflowPlanner:
    """按固定顺序穷举 (候选 × 维度 × 源)，不做任何状态判断。"""

    mode = "fixed_workflow"

    def __init__(self, registry: Any) -> None:
        self.registry = registry
        self._queue: list[tuple[str, str, str]] | None = None
        self._cursor = 0

    def _build_queue(self, state: Any) -> list[tuple[str, str, str]]:
        queue: list[tuple[str, str, str]] = []
        for symbol in sorted(state.candidates):
            for dimension in FIXED_DIMENSION_ORDER:
                for source_id in allowed_sources(self.registry, dimension, state.agent_config):
                    queue.append((symbol, dimension, source_id))
        return queue

    def plan(self, state: Any) -> Action:
        if self._queue is None:
            self._queue = self._build_queue(state)

        while self._cursor < len(self._queue):
            symbol, dimension, source_id = self._queue[self._cursor]
            self._cursor += 1
            candidate = state.candidates.get(symbol)
            if candidate is None:
                continue
            # 固定工作流不做 early-kill，被淘汰的候选也照查不误（这正是它贵的原因）
            return Action(
                kind=FETCH_EVIDENCE,
                reason=f"固定工作流第 {self._cursor}/{len(self._queue)} 项：穷举 {symbol}/{dimension}",
                target=symbol,
                dimension=dimension,
                source_id=source_id,
            )

        if not state.shortlist() and state.alive_candidates():
            from .actions import SHORTLIST
            return Action(kind=SHORTLIST, reason="固定工作流：穷举完成后统一圈定短名单")

        return Action(kind=STOP, reason="固定工作流队列执行完毕")


class FixedWorkflowBaseline(SearchAndVerifyAgent):
    """固定工作流基线。

    与 Agent 的差异被压缩到三点，便于在报告里逐条说明：
      1. planner 换成固定队列；
      2. 强制关闭 early-kill；
      3. 强制关闭动态重规划（换源、改写查询、冲突仲裁）。
    其余（取证、打分、验证、写契约）完全共用同一套代码。
    """

    def __init__(self, cancer_id: str, **kwargs: Any) -> None:
        agent_config = kwargs.pop("agent_config", None)
        super().__init__(cancer_id, agent_config=agent_config, **kwargs)

        ablation = dict(self.agent_config.get("ablation") or {})
        ablation.update({"disable_early_kill": True, "disable_dynamic_replanning": True})
        self.agent_config = {**self.agent_config, "ablation": ablation}
        self.state.agent_config = self.agent_config

        self.planner = FixedWorkflowPlanner(self.registry)
        self.kill_rules = []


def run_baseline(cancer_id: str, **kwargs: Any) -> RunResult:
    return FixedWorkflowBaseline(cancer_id, **kwargs).run()


def compare(agent_result: RunResult, baseline_result: RunResult) -> dict[str, Any]:
    """Agent vs 固定工作流的对照摘要（对应 Q39：相近质量下是否更省）。"""

    def profile(result: RunResult) -> dict[str, Any]:
        return {
            "source_calls": result.state.budget.source_calls,
            "cost_units": result.state.budget.cost_units,
            "steps": result.state.budget.steps,
            "evidence_total": result.state.store.summary()["total"],
            "evidence_verified": result.state.store.summary()["verified"],
            "shortlist": [c.symbol for c in result.state.shortlisted_ever()],
            "killed": [c.symbol for c in result.state.killed()],
            "stop_reason": result.stop_reason,
            # 预算耗尽而非自然结束时，成本数字是被截断的，对照结论要打折看
            "budget_exhausted": bool(result.state.budget.exhausted()),
        }

    a, b = profile(agent_result), profile(baseline_result)
    saved = b["cost_units"] - a["cost_units"]
    warnings: list[str] = []
    if b["budget_exhausted"]:
        warnings.append(
            "固定工作流因预算耗尽提前中止，其真实成本高于此处数字，节省比例被低估"
        )
    if a["budget_exhausted"]:
        warnings.append("Agent 因预算耗尽提前中止，质量指标可能未达稳定状态")
    return {
        "agent": a,
        "baseline": b,
        "cost_saved_units": saved,
        "cost_saved_ratio": round(saved / b["cost_units"], 3) if b["cost_units"] else 0.0,
        "same_shortlist": set(a["shortlist"]) == set(b["shortlist"]),
        "shortlist_overlap": sorted(set(a["shortlist"]) & set(b["shortlist"])),
        "warnings": warnings,
    }
