"""证据缺口识别（对应 Q13：Agent 如何知道当前最该查什么）。

这是 Agent 与固定工作流最直接的分野。
固定工作流：对每个候选、每个维度，按固定顺序全查一遍。
Agent：算出每个缺口的「信息价值 / 成本」，优先补最能改变结论的那个。

优先级由四个因子相乘得到，每个因子都对应一条真实的研究直觉：

1. 否决力（veto_power）
   能一票否决候选的维度优先查。膜定位查不到，后面的内吞、临床先例都白查。
   这直接决定了 early-kill 能多早发生，是省成本的主要来源。

2. 阶段权重（stage_weight）
   短名单候选值得深挖；还在初筛的候选只查便宜的筛选维度。

3. 不确定性（uncertainty）
   已有证据越少、冲突越多，补一条的边际价值越大。已经三源互证的维度不必再查。

4. 成本折扣（1 / cost）
   同等价值下先做便宜的。
"""
from __future__ import annotations

from typing import Any

from evidence_core.registry import SourceRegistry
from evidence_core.schema import DIMENSIONS
from evidence_core.source_policy import allowed_sources, sources_already_used

from .state import STATUS_BENCHMARK, STATUS_SHORTLISTED, Candidate, Gap, RunState

# 否决力：这些维度一旦不达标就能直接淘汰候选，因此应该最先查。
# 数值来自 configs/agent.yaml 的 early_kill 规则涉及的维度。
VETO_POWER = {
    "surface_localization": 3.0,
    "normal_reference_expression": 2.4,
    "offtarget_expression": 2.0,
    "safety": 1.8,
    "tumor_expression": 1.5,
    "extracellular_accessibility": 1.6,
    "internalization": 1.7,
    "delivery_precedent": 1.6,
}
DEFAULT_VETO = 1.0

SCREENING_DIMENSIONS = [
    "surface_localization",
    "tumor_expression",
    "normal_reference_expression",
    "offtarget_expression",
    "safety",
]

DELIVERY_VERIFICATION_DIMENSIONS = [
    "extracellular_accessibility",
    "internalization",
    "delivery_precedent",
]

DEEP_DIMENSIONS = [
    "internalization",
    "extracellular_accessibility",
    "delivery_precedent",
    "clinical_precedent",
    "safety",
    "ligand_availability",
    "shedding",
    "heterogeneity",
    "biodistribution",
    "endosomal_escape",
]


def _uncertainty(state: RunState, target: str, dimension: str) -> float:
    """0-1，越大表示越不确定、越值得补。"""
    items = state.store.for_target(target, dimension)
    if not items:
        return 1.0
    groups = state.store.independent_groups(target, dimension)
    conflicts = [c for c in state.store.open_conflicts(target) if c.dimension == dimension]
    if conflicts:
        return 0.9                      # 有冲突 = 高价值，补一条仲裁证据
    if len(groups) >= 2:
        return 0.1                      # 已有独立多源互证
    return 0.45                         # 只有单源


def _min_cost(registry: SourceRegistry, dimension: str, exclude: list[str]) -> int:
    plan = [s for s in registry.plan_for_dimension(dimension) if s not in exclude]
    if not plan:
        return 0
    costs = [registry.specs[s].cost_weight for s in plan if s in registry.specs]
    return min(costs) if costs else 1


def candidate_dimensions(candidate: Candidate, state: RunState) -> list[str]:
    """该候选当前阶段应该考虑哪些维度。"""
    delivery = state.delivery_dimensions() or DELIVERY_VERIFICATION_DIMENSIONS
    if candidate.status == STATUS_BENCHMARK:
        # 阳性对照仍要记递送先例，不能因为降级就把 Candidate 3 主轴豁免掉。
        return [d for d in delivery if d in DIMENSIONS]
    if candidate.status == STATUS_SHORTLISTED:
        required = state.required_dimensions()
        ordered = list(dict.fromkeys(
            SCREENING_DIMENSIONS + required + delivery + DEEP_DIMENSIONS
        ))
        return [d for d in ordered if d in DIMENSIONS]
    return [d for d in SCREENING_DIMENSIONS if d in DIMENSIONS]


def _searchable_candidates(state: RunState) -> list[Candidate]:
    seen: set[str] = set()
    out: list[Candidate] = []
    for candidate in list(state.alive_candidates()) + [
        c for c in state.candidates.values() if c.status == STATUS_BENCHMARK
    ]:
        if candidate.symbol in seen:
            continue
        seen.add(candidate.symbol)
        out.append(candidate)
    return out


def find_gaps(state: RunState, registry: SourceRegistry) -> list[Gap]:
    """算出所有开放缺口，按优先级从高到低排序。"""
    gaps: list[Gap] = []

    for candidate in _searchable_candidates(state):
        if candidate.source_calls >= state.budget.per_candidate_max_calls:
            continue
        stage_weight = 1.4 if candidate.status == STATUS_SHORTLISTED else (
            1.15 if candidate.status == STATUS_BENCHMARK else 1.0
        )

        for dimension in candidate_dimensions(candidate, state):
            tried = set(candidate.tried_sources(dimension)) | sources_already_used(
                state.store, candidate.symbol, dimension
            )
            available = allowed_sources(
                registry, dimension, state.agent_config, exclude=tried
            )
            if not available:
                continue

            uncertainty = _uncertainty(state, candidate.symbol, dimension)
            if uncertainty <= 0.1:
                continue

            veto = VETO_POWER.get(dimension, DEFAULT_VETO)
            cost = max(_min_cost(registry, dimension, tried), 1)
            priority = veto * stage_weight * uncertainty / cost

            existing = len(state.store.for_target(candidate.symbol, dimension))
            reason = (
                f"{DIMENSIONS[dimension]['label']}：已有 {existing} 条证据、"
                f"独立来源 {len(state.store.independent_groups(candidate.symbol, dimension))} 个，"
                f"否决力 {veto}，可用源 {available[:2]}"
            )
            gaps.append(Gap(candidate.symbol, dimension, priority, reason))

    gaps.sort(key=lambda g: g.priority, reverse=True)
    return gaps


def top_gap(state: RunState, registry: SourceRegistry) -> Gap | None:
    gaps = find_gaps(state, registry)
    return gaps[0] if gaps else None


def gap_report(state: RunState, registry: SourceRegistry, limit: int = 10) -> list[dict[str, Any]]:
    return [g.to_dict() for g in find_gaps(state, registry)[:limit]]
