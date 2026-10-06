"""运行状态：Agent 每一步决策的唯一依据。

「状态驱动」这四个字的落点就在这里。固定工作流不需要状态——它按写死的顺序执行；
Agent 必须在每一步看着当前状态回答「现在最该做什么」，所以状态必须显式、完整、可序列化。

可序列化很关键：state 能 dump 成 JSON，就意味着任何一步都能被复现和回放（Q52）。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from evidence_core.evidence_store import EvidenceStore, coverage_ratio, missing_dimensions
from evidence_core.normalize import GeneIdentity

# 候选状态机
STATUS_ACTIVE = "active"           # 在候选池中，可继续取证
STATUS_SHORTLISTED = "shortlisted"  # 进入短名单，做深度取证
STATUS_KILLED = "killed"           # early-kill 淘汰，不再深搜
STATUS_COMPLETED = "completed"     # 证据已充分
STATUS_BLOCKED = "blocked"         # 数据源不覆盖，无法继续（与 killed 区分！）
STATUS_BENCHMARK = "benchmark"     # 不作肿瘤主线，但保留为阳性对照（与 killed 区分！）
STATUS_NEEDS_MORE = "needs_more"   # 筛选够了但递送验证维度仍缺，不能假装 completed


@dataclass
class Candidate:
    """一个候选靶点及其当前认知状态。"""

    symbol: str
    identity: GeneIdentity
    route_type: str = "exploratory"
    status: str = STATUS_ACTIVE
    discovered_by: str = ""
    # status 会随流程推进被覆盖（shortlisted -> completed），
    # 单独记一个标志才能在最终报告里回答「它曾经进过短名单吗」。
    was_shortlisted: bool = False
    kill_reasons: list[str] = field(default_factory=list)
    risk_reasons: list[str] = field(default_factory=list)
    block_reasons: list[str] = field(default_factory=list)
    source_calls: int = 0
    failed_dimensions: dict[str, list[str]] = field(default_factory=dict)  # dim -> 试过且失败的源

    @property
    def is_alive(self) -> bool:
        return self.status in (STATUS_ACTIVE, STATUS_SHORTLISTED)

    def note_failure(self, dimension: str, source_id: str) -> None:
        self.failed_dimensions.setdefault(dimension, []).append(source_id)

    def tried_sources(self, dimension: str) -> list[str]:
        return self.failed_dimensions.get(dimension, [])

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "route_type": self.route_type,
            "status": self.status,
            "discovered_by": self.discovered_by,
            "kill_reasons": self.kill_reasons,
            "risk_reasons": self.risk_reasons,
            "block_reasons": self.block_reasons,
            "source_calls": self.source_calls,
            "identity": self.identity.to_dict(),
        }


@dataclass
class Budget:
    """预算。Agent 必须在预算内停下来，这是「不会无限搜索」的硬保证（Q17）。"""

    max_steps: int = 60
    max_source_calls: int = 200
    max_cost_units: int = 300
    max_seconds: int = 900
    per_candidate_max_calls: int = 20

    steps: int = 0
    source_calls: int = 0
    cost_units: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def exhausted(self) -> str | None:
        """返回耗尽原因，None 表示还有余量。"""
        if self.steps >= self.max_steps:
            return f"达到最大步数 {self.max_steps}"
        if self.source_calls >= self.max_source_calls:
            return f"达到最大数据源调用数 {self.max_source_calls}"
        if self.cost_units >= self.max_cost_units:
            return f"达到成本上限 {self.max_cost_units}"
        if self.elapsed >= self.max_seconds:
            return f"超时 {self.max_seconds}s"
        return None

    def spend(self, cost: int = 1) -> None:
        self.source_calls += 1
        self.cost_units += cost

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "source_calls": self.source_calls,
            "cost_units": self.cost_units,
            "elapsed_seconds": round(self.elapsed, 2),
            "limits": {
                "max_steps": self.max_steps,
                "max_source_calls": self.max_source_calls,
                "max_cost_units": self.max_cost_units,
                "max_seconds": self.max_seconds,
            },
        }


@dataclass
class Gap:
    """一个证据缺口：某候选缺某维度。priority 越大越该先补。"""

    target: str
    dimension: str
    priority: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "dimension": self.dimension,
            "priority": round(self.priority, 3),
            "reason": self.reason,
        }


@dataclass
class RunState:
    """一次运行的完整状态。"""

    cancer_id: str
    cancer_config: dict[str, Any]
    agent_config: dict[str, Any]
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    candidates: dict[str, Candidate] = field(default_factory=dict)
    store: EvidenceStore = field(default_factory=EvidenceStore)
    budget: Budget = field(default_factory=Budget)
    phase: str = "init"            # init / probe / seed / search_verify / finalize / done
    stop_reason: str = ""
    probe_results: list[dict[str, Any]] = field(default_factory=list)
    no_progress_rounds: int = 0
    last_shortlist: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # ---------------- 候选 ----------------

    def add_candidate(self, candidate: Candidate) -> bool:
        """加入候选池。超过上限或已存在返回 False。"""
        if candidate.symbol in self.candidates:
            return False
        cap = int(((self.cancer_config.get("candidate_generation") or {})
                   .get("max_candidates")) or 40)
        if len(self.candidates) >= cap:
            return False
        self.candidates[candidate.symbol] = candidate
        return True

    def alive_candidates(self) -> list[Candidate]:
        return [c for c in self.candidates.values() if c.is_alive]

    def shortlist(self) -> list[Candidate]:
        """当前仍在深度取证中的短名单候选。"""
        return [c for c in self.candidates.values() if c.status == STATUS_SHORTLISTED]

    def shortlisted_ever(self) -> list[Candidate]:
        """所有进过短名单的候选（含已完成的）。报告与评测用这个。"""
        return [c for c in self.candidates.values() if c.was_shortlisted]

    def killed(self) -> list[Candidate]:
        return [c for c in self.candidates.values() if c.status == STATUS_KILLED]

    # ---------------- 证据视图 ----------------

    def required_dimensions(self) -> list[str]:
        return list((self.agent_config.get("evidence") or {}).get("required_dimensions") or [])

    def delivery_dimensions(self) -> list[str]:
        return list((self.agent_config.get("evidence") or {}).get("delivery_required_dimensions") or [])

    def coverage(self, symbol: str) -> float:
        verified_only = not self._verifier_disabled()
        return coverage_ratio(self.store, symbol, self.required_dimensions(),
                              verified_only=verified_only)

    def delivery_missing(self, symbol: str) -> list[str]:
        verified_only = not self._verifier_disabled()
        dims = self.delivery_dimensions()
        if not dims:
            return []
        return missing_dimensions(self.store, symbol, dims, verified_only=verified_only)

    def gaps_for(self, symbol: str) -> list[str]:
        verified_only = not self._verifier_disabled()
        return missing_dimensions(self.store, symbol, self.required_dimensions(),
                                  verified_only=verified_only)

    def _verifier_disabled(self) -> bool:
        return bool((self.agent_config.get("ablation") or {}).get("disable_verifier"))

    # ---------------- 序列化 ----------------

    def snapshot(self) -> dict[str, Any]:
        """当前状态的可读快照。写进 trace，也是 LLM planner 的输入。"""
        return {
            "run_id": self.run_id,
            "cancer_id": self.cancer_id,
            "phase": self.phase,
            "budget": self.budget.to_dict(),
            "candidates": {
                c.symbol: {
                    "status": c.status,
                    "route_type": c.route_type,
                    "coverage": round(self.coverage(c.symbol), 2),
                    "gaps": self.gaps_for(c.symbol),
                    "delivery_missing": self.delivery_missing(c.symbol),
                    "covered_dimensions": sorted(self.store.dimensions_covered(c.symbol, verified_only=True)),
                    "failed_sources_by_dimension": dict(c.failed_dimensions),
                    "kill_reasons": c.kill_reasons,
                    "risk_reasons": c.risk_reasons,
                    "source_calls": c.source_calls,
                }
                for c in self.candidates.values()
            },
            "evidence": self.store.summary(),
            "open_conflicts": [c.to_dict() for c in self.store.open_conflicts()],
            "stop_reason": self.stop_reason,
        }
