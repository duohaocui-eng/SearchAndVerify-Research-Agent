"""early-kill：只有 hard-negative 才淘汰；弱证据和风险不能当成「已证明不可行」。

淘汰与阻塞必须分开：缺证据是不知道，不是不行。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from evidence_core.contract_writer import merged_scores

from .state import STATUS_BENCHMARK, STATUS_BLOCKED, STATUS_KILLED, Candidate, RunState

_CONDITION_RE = re.compile(r"^\s*([a-z_]+)\s*(<=|>=|<|>|==)\s*(-?\d+(?:\.\d+)?)\s*$")
_OPS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
}

# hard: 充分支持的硬否决（分泌/不可及）。
# risk: 记风险，不淘汰。
# demote_if_membrane: 仅当表面证据支持膜受体时降级为 benchmark，分泌蛋白不当对照。
SEVERITY_HARD = "hard"
SEVERITY_RISK = "risk"
SEVERITY_DEMOTE = "demote_if_membrane"


@dataclass
class KillRule:
    rule_id: str
    condition: str
    reason: str
    severity: str = SEVERITY_HARD

    def evaluate(self, scores: dict[str, float]) -> bool | None:
        """True=条件成立，False=未触发，None=证据不足无法判定。"""
        clauses = [c.strip() for c in self.condition.split(" and ")]
        results: list[bool] = []
        for clause in clauses:
            m = _CONDITION_RE.match(clause)
            if not m:
                return None
            field, op, threshold = m.group(1), m.group(2), float(m.group(3))
            if field not in scores:
                return None
            results.append(_OPS[op](scores[field], threshold))
        return all(results) if results else None


def load_rules(agent_config: dict[str, Any]) -> list[KillRule]:
    raw = (agent_config.get("early_kill") or {}).get("rules") or []
    rules: list[KillRule] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        rules.append(KillRule(
            rule_id=str(entry.get("id", "")),
            condition=str(entry.get("when", "")),
            reason=str(entry.get("reason", "")),
            severity=str(entry.get("severity") or SEVERITY_HARD).lower(),
        ))
    return rules


def is_enabled(agent_config: dict[str, Any]) -> bool:
    if (agent_config.get("ablation") or {}).get("disable_early_kill"):
        return False
    return bool((agent_config.get("early_kill") or {}).get("enabled", True))


def evaluate_candidate(
    state: RunState,
    candidate: Candidate,
    rules: list[KillRule],
) -> tuple[bool, list[str]]:
    """兼容旧接口：是否应淘汰，以及 hard-kill 理由。"""
    verified_only = not bool((state.agent_config.get("ablation") or {}).get("disable_verifier"))
    scores = merged_scores(state.store, candidate.symbol, verified_only=verified_only)
    hard, _, _ = _classify_hits(rules, scores)
    return bool(hard), hard


def _classify_hits(
    rules: list[KillRule],
    scores: dict[str, float],
) -> tuple[list[str], list[str], list[str]]:
    hard: list[str] = []
    risk: list[str] = []
    demote: list[str] = []
    for rule in rules:
        result = rule.evaluate(scores)
        if result is not True:
            continue
        detail = ", ".join(f"{f}={scores[f]}" for f in scores if f in rule.condition)
        note = f"[{rule.rule_id}] {rule.reason}（{detail}）"
        if rule.severity == SEVERITY_RISK:
            risk.append(note)
        elif rule.severity == SEVERITY_DEMOTE:
            demote.append(note)
        else:
            hard.append(note)
    return hard, risk, demote


def apply_early_kill(state: RunState, rules: list[KillRule]) -> list[Candidate]:
    """hard-negative 才淘汰；弱内吞/正常组织风险只记 risk_reasons。"""
    if not is_enabled(state.agent_config):
        return []
    newly_killed: list[Candidate] = []
    verified_only = not bool((state.agent_config.get("ablation") or {}).get("disable_verifier"))
    for candidate in state.alive_candidates():
        scores = merged_scores(state.store, candidate.symbol, verified_only=verified_only)
        hard, risk, demote = _classify_hits(rules, scores)
        for note in risk:
            if note not in candidate.risk_reasons:
                candidate.risk_reasons.append(note)

        if hard:
            candidate.status = STATUS_KILLED
            candidate.kill_reasons.extend(hard)
            newly_killed.append(candidate)
            continue

        if demote:
            surface = scores.get("surface_localization_score")
            extra = scores.get("extracellular_accessibility_score")
            membrane_ok = (surface is not None and surface >= 6.0) or (
                extra is not None and extra >= 6.0
            )
            live_membrane = any(
                i.dimension in {"surface_localization", "extracellular_accessibility"}
                and i.score is not None
                and i.score >= 6.0
                and not str(i.source_id).startswith("fixtures")
                for i in state.store.for_target(candidate.symbol, verified_only=verified_only)
            )
            if membrane_ok and live_membrane and classify_route(state, candidate) == "benchmark":
                candidate.status = STATUS_BENCHMARK
                candidate.route_type = "benchmark"
                candidate.block_reasons.append(
                    "降级为 benchmark 阳性对照：" + "；".join(demote)
                    + "。膜可及但参照组织主导，停止深搜但保留为递送对照。"
                )
    return newly_killed


def mark_blocked(candidate: Candidate, reason: str) -> None:
    candidate.status = STATUS_BLOCKED
    candidate.block_reasons.append(reason)


def classify_route(state: RunState, candidate: Candidate) -> str:
    """按证据推断路线。分泌/不可及蛋白不能因为参照组织高表达而被标成 benchmark。"""
    verified_only = not bool((state.agent_config.get("ablation") or {}).get("disable_verifier"))
    scores = merged_scores(state.store, candidate.symbol, verified_only=verified_only)
    tumor = scores.get("tumor_expression_score")
    normal_ref = scores.get("normal_reference_tissue_expression_score")
    surface = scores.get("surface_localization_score")

    if surface is not None and surface < 4:
        return "exploratory"

    if tumor is None or normal_ref is None:
        return "exploratory"

    contrast = tumor - normal_ref
    membrane_ok = surface is None or surface >= 6.0
    if membrane_ok and normal_ref >= 8.0 and contrast <= 1.5:
        return "benchmark"
    if contrast >= 5.0 and tumor >= 7.0:
        return "main"
    if contrast >= 2.0:
        return "backup"
    return "exploratory"
