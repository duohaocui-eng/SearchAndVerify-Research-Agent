"""运行轨迹：把每一步决策与结果写成可回放的 JSONL（对应 Q52）。

轨迹不是日志。日志是给人看的，轨迹是给评测和复盘用的结构化记录：
每条包含 step / phase / 决策前状态摘要 / 动作 / 结果 / 预算消耗。
有了它才能在面试或复盘时说「这是一条 early-kill 轨迹，第 7 步因为
normal_tissue_penalty=9.1 淘汰了 ASGR1，省下了后续 12 次调用」。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .actions import ActionResult
from .state import RunState


@dataclass
class TraceRecorder:
    run_id: str
    records: list[dict[str, Any]] = field(default_factory=list)
    enabled: bool = True

    def record(
        self,
        state: RunState,
        result: ActionResult | None = None,
        *,
        event: str = "action",
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        entry: dict[str, Any] = {
            "step": state.budget.steps,
            "phase": state.phase,
            "event": event,
            "budget": {
                "source_calls": state.budget.source_calls,
                "cost_units": state.budget.cost_units,
            },
            "alive_candidates": [c.symbol for c in state.alive_candidates()],
            "evidence_summary": state.store.summary(),
        }
        if result is not None:
            entry["result"] = result.to_dict()
        if extra:
            entry.update(extra)
        self.records.append(entry)

    def write(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as fh:
            for record in self.records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ---------------- 轨迹分类：用于产出「四类可回放轨迹」 ----------------

    def classify(self, state: RunState) -> str:
        """把这次运行归入四类之一，便于按类型归档演示。"""
        if any(r.get("event") == "source_failure" for r in self.records):
            if not state.shortlist():
                return "failure_degraded"      # 失败降级
        if state.store.open_conflicts():
            return "conflict"                  # 冲突未决
        if state.killed():
            return "early_kill"                # 含淘汰
        return "success"

    def stats(self) -> dict[str, Any]:
        actions: dict[str, int] = {}
        failures = 0
        for record in self.records:
            result = record.get("result")
            if not result:
                continue
            kind = result.get("action", {}).get("kind", "?")
            actions[kind] = actions.get(kind, 0) + 1
            if not result.get("ok", True):
                failures += 1
        return {
            "total_records": len(self.records),
            "action_counts": actions,
            "failed_actions": failures,
        }
