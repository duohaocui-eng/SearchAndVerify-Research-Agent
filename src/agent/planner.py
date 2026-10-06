"""Planner：看着当前状态决定下一个动作。

两个实现共用一个接口：
- RulePlanner：确定性策略。它既是 LLM 未接入时的可用实现，也是评测里的对照组
  ——「LLM 规划到底比一个写得不错的启发式强多少」是必须回答的问题（Q29）。
- LLMPlanner ：用 LLM 做规划。未配置 LLM_API_KEY 等环境变量、或 LLM 输出
  没通过校验（非法动作、指向不存在的候选/维度/源）时自动降级到 RulePlanner。

关键约束：Planner 只能产出 Action，不能直接写证据、不能改分数。
它能影响结果的唯一途径是「决定去查什么」，这保证了 LLM 出错的最坏后果是
多花几次调用，而不是往报告里塞进一个编造的数字。
"""
from __future__ import annotations

import json
from typing import Any

from evidence_core.http import DeadlineExceeded, check_deadline

from evidence_core.registry import SourceRegistry
from evidence_core.schema import DIMENSIONS
from evidence_core.source_policy import allowed_sources, sources_already_used, is_narrative_source

from .actions import (
    DYNAMIC_ACTIONS,
    EXPAND_CANDIDATES,
    FETCH_EVIDENCE,
    RESOLVE_CONFLICT,
    REWRITE_QUERY,
    SHORTLIST,
    STOP,
    SWITCH_SOURCE,
    Action,
)
from .gap_analysis import find_gaps
from .state import STATUS_ACTIVE, STATUS_BENCHMARK, STATUS_SHORTLISTED, RunState

# LLM 规划器允许产出的动作集合。PROBE_SOURCES / GENERATE_CANDIDATES /
# KILL_CANDIDATE 不在其中：前两个由 loop.py 在固定阶段直接发出，不经过
# Planner；early-kill 是确定性规则的产物，不接受 LLM 决定"杀哪个候选"。
_PLANNER_ACTION_KINDS = {
    FETCH_EVIDENCE, SWITCH_SOURCE, RESOLVE_CONFLICT, REWRITE_QUERY,
    EXPAND_CANDIDATES, SHORTLIST, STOP,
}
_TARGETED_ACTION_KINDS = {FETCH_EVIDENCE, SWITCH_SOURCE, RESOLVE_CONFLICT, REWRITE_QUERY}


class RulePlanner:
    """确定性规划器。

    优先级顺序本身就是这个 Agent 的「研究策略」，按重要性从高到低：
      1. 有未解决冲突 -> 先仲裁。带着冲突继续搜集只会放大错误。
      2. 有证据缺口   -> 补优先级最高的那个（由 gap_analysis 算出）。
      3. 短名单未定   -> 用当前证据圈定短名单，进入深度取证。
      4. 无事可做     -> 停。
    """

    mode = "rule"

    def __init__(self, registry: SourceRegistry) -> None:
        self.registry = registry

    def plan(self, state: RunState) -> Action:
        dynamic = self._dynamic_enabled(state)

        # 1. 冲突仲裁
        if dynamic:
            conflicts = state.store.open_conflicts()
            for conflict in conflicts:
                candidate = state.candidates.get(conflict.target)
                if not candidate or not candidate.is_alive:
                    continue
                tried = set(candidate.tried_sources(conflict.dimension)) | sources_already_used(
                    state.store, conflict.target, conflict.dimension
                )
                options = allowed_sources(
                    self.registry, conflict.dimension, state.agent_config, exclude=tried
                )
                if options:
                    return Action(
                        kind=RESOLVE_CONFLICT,
                        reason=f"{conflict.target} 的 {conflict.dimension} 存在未解决冲突：{conflict.detail}",
                        target=conflict.target,
                        dimension=conflict.dimension,
                        source_id=options[0],
                        payload={"conflict": conflict.to_dict()},
                    )

        # 2. 补缺口
        gaps = find_gaps(state, self.registry)
        if gaps:
            gap = gaps[0]
            candidate = state.candidates[gap.target]
            tried = set(candidate.tried_sources(gap.dimension)) | sources_already_used(
                state.store, gap.target, gap.dimension
            )
            options = allowed_sources(
                self.registry, gap.dimension, state.agent_config, exclude=tried
            )
            if options:
                # 换源属于动态动作；关掉动态重规划后只允许用首选源
                kind = SWITCH_SOURCE if (tried and dynamic) else FETCH_EVIDENCE
                if kind in DYNAMIC_ACTIONS and not dynamic:
                    kind = FETCH_EVIDENCE
                return Action(
                    kind=kind,
                    reason=gap.reason,
                    target=gap.target,
                    dimension=gap.dimension,
                    source_id=options[0],
                    payload={"priority": round(gap.priority, 3)},
                )

        # 3. 圈定短名单
        if not state.shortlist() and state.alive_candidates():
            return Action(
                kind=SHORTLIST,
                reason="初筛维度已覆盖，按当前证据圈定短名单进入深度取证",
            )

        # 4. 扩展候选（只有在短名单太小、且允许扩展时）
        if dynamic and self._should_expand(state):
            return Action(
                kind=EXPAND_CANDIDATES,
                reason="存活候选数量不足，尝试基于已有发现扩展候选池",
            )

        return Action(kind=STOP, reason="无剩余高价值动作")

    def _dynamic_enabled(self, state: RunState) -> bool:
        if (state.agent_config.get("ablation") or {}).get("disable_dynamic_replanning"):
            return False
        return bool((state.agent_config.get("planner") or {}).get("dynamic_replanning", True))

    def _should_expand(self, state: RunState) -> bool:
        if not (state.agent_config.get("planner") or {}).get("allow_candidate_expansion", True):
            return False
        target_size = int(((state.cancer_config.get("candidate_generation") or {})
                           .get("shortlist_size")) or 8)
        return len(state.alive_candidates()) < max(3, target_size // 2)


def _action_catalog(dynamic: bool, allow_expansion: bool = True) -> str:
    """给 LLM 的动作说明书。dynamic=False 时只列固定工作流允许的子集，
    保证 --ablation no_replanning 场景下 LLM 也不会绕过这个约束。"""
    rows = [
        (FETCH_EVIDENCE, "target, dimension, source_id",
         "为某候选的某个证据维度，向某个数据源发起首次取证"),
        (SHORTLIST, "（都不填）", "用当前证据圈定短名单，进入深度取证阶段"),
        (STOP, "（都不填）", "没有更高价值的动作了，结束本轮搜索"),
    ]
    if dynamic:
        rows[1:1] = [
            (SWITCH_SOURCE, "target, dimension, source_id",
             "某维度已试过的源都失败/不覆盖了，换一个源再取"),
            (RESOLVE_CONFLICT, "target, dimension, source_id",
             "该候选该维度存在未解决冲突，向另一个源取仲裁证据"),
            (REWRITE_QUERY, "target, dimension, source_id, "
             "payload.rewritten_queries（字符串数组，必填）",
             "文献检索词效果不好时，改写查询词再试；只对文献类源有意义"),
            (EXPAND_CANDIDATES, "（都不填）",
             "存活候选太少时，基于已有发现扩展候选池"),
        ]
    if not allow_expansion:
        rows = [row for row in rows if row[0] != EXPAND_CANDIDATES]
    return "\n".join(f"- {kind}：需要 {fields}。{desc}" for kind, fields, desc in rows)


def _fetch_options(registry: SourceRegistry, state: RunState, target: str,
                   dimension: str) -> list[str]:
    candidate = state.candidates[target]
    tried = set(candidate.tried_sources(dimension)) | sources_already_used(
        state.store, target, dimension
    )
    return allowed_sources(registry, dimension, state.agent_config, exclude=tried)


class LLMPlanner:
    """LLM 规划器。

    设计契约：
      输入 : state.snapshot() + 可用动作列表 + 可用数据源及其能力
      输出 : 一个 Action 的 JSON，必须包含 kind 与 reason
      约束 : 输出经 schema 校验；非法动作、指向不存在候选/维度/源的动作，
             以及关闭动态重规划时选了动态动作，一律丢弃并回落规则版。
    这样即便 LLM 胡说，最坏情况也只是退化成 RulePlanner，不会污染证据——
    Planner 的产出只是"下一步查什么"，从不直接写分数或证据。
    """

    mode = "llm"

    def __init__(self, registry: SourceRegistry, client: Any | None = None) -> None:
        self.registry = registry
        self.client = client
        self._fallback = RulePlanner(registry)
        self._feedback = "无"
        self._validation_error = ""
        self._rewrites: dict[tuple[str, str, str], set[tuple[str, ...]]] = {}

    def _fallback_to_rule(self, state: RunState, why: str) -> Action:
        self._feedback = why
        action = self._fallback.plan(state)
        action.payload["planner_fallback"] = why
        return action

    def plan(self, state: RunState) -> Action:
        if self.client is None or not getattr(self.client, "is_configured", lambda: False)():
            return self._fallback_to_rule(state, "LLM 未接入，使用规则规划器")

        from .llm.prompts import PLAN_PROMPT, render

        dynamic = self._dynamic_enabled(state)
        prompt = render(
            PLAN_PROMPT,
            cancer=state.cancer_id,
            state_json=json.dumps(state.snapshot(), ensure_ascii=False, indent=2),
            action_catalog=_action_catalog(dynamic, self._fallback._should_expand(state)),
            source_catalog=json.dumps(self.executable_options(state), ensure_ascii=False),
            feedback=self._feedback,
        )
        try:
            check_deadline()
            raw = self.client.complete_json(prompt, purpose="plan")
        except DeadlineExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 —— LLM 调用失败的最坏后果只能是降级，不能是崩掉
            return self._fallback_to_rule(state, f"LLM 调用/解析失败：{type(exc).__name__}: {exc}")

        action = self._validate(raw, state, dynamic)
        if action is None:
            return self._fallback_to_rule(
                state, f"{self._validation_error}；已丢弃：{json.dumps(raw, ensure_ascii=False)}"
            )
        action.payload["planner_source"] = "llm"
        if action.kind == STOP:
            rule_action = self._fallback.plan(state)
            if rule_action.kind != STOP:
                return self._fallback_to_rule(
                    state,
                    "LLM 选择 STOP，但规则规划器仍有合法动作，禁止提前结束",
                )
        if action.kind == REWRITE_QUERY:
            key = (action.target, action.dimension, action.source_id)
            queries = tuple(q.casefold() for q in action.payload["hints"]["rewritten_queries"])
            self._rewrites.setdefault(key, set()).add(queries)
        self._feedback = "上次规划通过校验；根据最新状态选择下一步。"
        return action

    def executable_options(self, state: RunState) -> dict[str, Any]:
        """与校验器共用同一组动作条件，含全部递送维度；不替模型排序。"""
        rows = {}
        dynamic = self._dynamic_enabled(state)
        for target, candidate in state.candidates.items():
            if (not candidate.is_alive and candidate.status != STATUS_BENCHMARK) or (
                candidate.source_calls >= state.budget.per_candidate_max_calls
            ):
                continue
            dims = {}
            for dim in DIMENSIONS:
                fetch = _fetch_options(self.registry, state, target, dim)
                rewrite = self._rewrite_sources(state, target, dim) if dynamic else []
                if fetch or rewrite:
                    dims[dim] = {"fetch_sources": fetch, "rewrite_sources": rewrite,
                                 "open_conflict": any(c.dimension == dim for c in
                                                      state.store.open_conflicts(target))}
            rows[target] = dims
        return rows

    def _rewrite_sources(self, state: RunState, target: str, dimension: str) -> list[str]:
        tried = set(state.candidates[target].tried_sources(dimension)) | sources_already_used(
            state.store, target, dimension
        )
        return [sid for sid in allowed_sources(self.registry, dimension, state.agent_config)
                if is_narrative_source(sid) and sid in tried
                and len(self._rewrites.get((target, dimension, sid), set())) < 2]

    def _reject(self, why: str) -> None:
        self._validation_error = why
        return None

    def _dynamic_enabled(self, state: RunState) -> bool:
        if (state.agent_config.get("ablation") or {}).get("disable_dynamic_replanning"):
            return False
        return bool((state.agent_config.get("planner") or {}).get("dynamic_replanning", True))

    def _validate(self, raw: Any, state: RunState, dynamic: bool) -> Action | None:
        """把 LLM 的原始 JSON 变成一个确认合法的 Action，任何一步不满足就返回 None。"""
        if not isinstance(raw, dict):
            return self._reject("输出必须为 JSON 对象")
        kind = str(raw.get("kind") or "")
        reason = str(raw.get("reason") or "").strip()
        if kind not in _PLANNER_ACTION_KINDS or not reason:
            return self._reject("动作类型非法或缺少 reason")
        if kind in DYNAMIC_ACTIONS and not dynamic:
            return self._reject("当前配置禁止动态动作")
        if kind == EXPAND_CANDIDATES and not self._fallback._should_expand(state):
            return self._reject("当前配置或候选数量不允许扩展")
        if kind == SHORTLIST and (state.shortlist() or not state.alive_candidates()):
            return self._reject("已有短名单或没有存活候选")
        if kind not in _TARGETED_ACTION_KINDS:
            return Action(kind=kind, reason=reason)

        target = str(raw.get("target") or "").strip()
        dimension = str(raw.get("dimension") or "").strip()
        source_id = str(raw.get("source_id") or "").strip()
        candidate = state.candidates.get(target)
        if candidate is None or (not candidate.is_alive and candidate.status != STATUS_BENCHMARK):
            return self._reject("靶点不存在或已不可搜索")
        if candidate.source_calls >= state.budget.per_candidate_max_calls:
            return self._reject("该候选调用预算已耗尽")
        if dimension not in DIMENSIONS:
            return self._reject("未知证据维度")
        if source_id not in allowed_sources(self.registry, dimension, state.agent_config):
            return self._reject(f"源 {source_id} 不可用或不支持 {dimension}")
        if kind == RESOLVE_CONFLICT and not any(
            c.dimension == dimension for c in state.store.open_conflicts(target)
        ):
            return self._reject("该靶点该维度没有开放冲突；补证请用 fetch_evidence")

        payload: dict[str, Any] = {}
        if kind == REWRITE_QUERY:
            if source_id not in self._rewrite_sources(state, target, dimension):
                return self._reject("改写仅用于已尝试的文献源，每个靶点/维度/源最多两次")
            queries = raw.get("payload", {}).get("rewritten_queries") if isinstance(raw.get("payload"), dict) else None
            if not isinstance(queries, list) or not 1 <= len(queries) <= 3 or not all(
                isinstance(q, str) and q.strip() and len(q) <= 500 for q in queries
            ):
                return self._reject("改写需要 1–3 个非空查询，每条不超过 500 字符")
            queries = list(dict.fromkeys(q.strip() for q in queries))
            canonical = tuple(q.casefold() for q in queries)
            previous = self._rewrites.get((target, dimension, source_id), set())
            if any(set(canonical) & set(old) for old in previous):
                return self._reject("改写查询已尝试，不得重复")
            payload["hints"] = {"rewritten_queries": queries}
        elif source_id not in _fetch_options(self.registry, state, target, dimension):
            return self._reject("该靶点/维度/源已尝试或已有同源证据，请换源或有界改写")

        return Action(kind=kind, reason=reason, target=target, dimension=dimension,
                      source_id=source_id, payload=payload)


def build_planner(agent_config: dict[str, Any], registry: SourceRegistry,
                  client: Any | None = None):
    mode = str((agent_config.get("planner") or {}).get("mode", "rule")).lower()
    if mode == "llm":
        return LLMPlanner(registry, client)
    return RulePlanner(registry)


def select_shortlist(state: RunState) -> list[str]:
    """按当前证据给存活候选排序，取前 N 进短名单。

    排序依据刻意只用「已验证证据算出的分」，不引入 LLM 判断，
    这样短名单是可复现的，评测里 Recall@K 才有意义。
    """
    from evidence_core.contract_writer import merged_scores

    verified_only = not bool((state.agent_config.get("ablation") or {}).get("disable_verifier"))
    size = int(((state.cancer_config.get("candidate_generation") or {})
                .get("shortlist_size")) or 8)

    scored: list[tuple[float, str]] = []
    for candidate in state.alive_candidates():
        # 完全没证据的候选不进短名单——否则空候选会靠 0 分占位挤掉真候选。
        if not state.store.for_target(candidate.symbol):
            continue
        scores = merged_scores(state.store, candidate.symbol, verified_only=verified_only)
        if not scores:
            continue
        tumor = scores.get("tumor_expression_score", 0.0)
        normal = scores.get("normal_reference_tissue_expression_score", 0.0)
        surface = scores.get("surface_localization_score", 0.0)
        penalty = scores.get("normal_tissue_penalty", 0.0)
        # 初筛分：肿瘤选择性 + 膜可及性 - 脱靶
        screen = (tumor - normal) * 0.5 + surface * 0.4 - penalty * 0.2
        scored.append((screen, candidate.symbol))

    scored.sort(reverse=True)
    return [symbol for _, symbol in scored[:size]]


def apply_shortlist(state: RunState) -> list[str]:
    chosen = select_shortlist(state)
    for symbol in chosen:
        candidate = state.candidates.get(symbol)
        if candidate and candidate.is_alive:
            candidate.status = STATUS_SHORTLISTED
            candidate.was_shortlisted = True
    for candidate in state.alive_candidates():
        if candidate.symbol not in chosen and candidate.status == STATUS_SHORTLISTED:
            candidate.status = STATUS_ACTIVE
    return chosen
