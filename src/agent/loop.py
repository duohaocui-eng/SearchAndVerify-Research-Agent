"""Search-and-Verify 主循环。

整体流程：
    normalize -> probe -> seed candidates -> [ plan -> act -> verify -> early-kill ]* -> finalize

循环体每一轮都重新看状态再决定动作，这就是「状态驱动」；
把 plan 换成固定顺序即得到 baseline.py 的固定工作流，两者共用下面所有执行逻辑，
保证对照实验比较的是「决策方式」而不是「两套不同的实现」。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from evidence_core.config import load_agent_config, load_cancer_config
from evidence_core.contract_writer import build_row, write_contract
from evidence_core.http import SourceUnavailable, DeadlineExceeded, request_deadline, check_deadline
from evidence_core.normalize import normalize_cancer_type, normalize_gene
from evidence_core.registry import SourceRegistry
from evidence_core.scorer import score_observation
from evidence_core.sources import build_registry, get_source
from evidence_core.sources.base import FetchRequest
from evidence_core.source_policy import is_narrative_source, structured_only_enabled

from . import early_kill as ek
from .actions import (
    EXPAND_CANDIDATES,
    FETCH_EVIDENCE,
    GENERATE_CANDIDATES,
    PROBE_SOURCES,
    RESOLVE_CONFLICT,
    REWRITE_QUERY,
    SHORTLIST,
    STOP,
    SWITCH_SOURCE,
    Action,
    ActionResult,
)
from .planner import apply_shortlist, build_planner
from .state import (
    STATUS_ACTIVE,
    STATUS_BENCHMARK,
    STATUS_COMPLETED,
    STATUS_KILLED,
    STATUS_NEEDS_MORE,
    STATUS_SHORTLISTED,
    Budget,
    Candidate,
    RunState,
)
from .trace import TraceRecorder
from .verifier import build_verifier, verify_pending


def _default_llm_client() -> Any:
    from .llm.client import LLMClient

    client = LLMClient()
    if os.environ.get("SNV_TEST_OFFLINE"):
        # 与 evidence_core 的离线开关保持同一条纪律：测试环境即便宿主 shell
        # 意外设置了 LLM_API_KEY 等变量，也不该真的发起网络调用。
        client.is_configured = lambda: False  # type: ignore[method-assign]
    return client


@dataclass
class RunResult:
    state: RunState
    trace: TraceRecorder
    contract_rows: list[dict[str, str]]
    trace_class: str
    stop_reason: str
    llm_calls: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.state.run_id,
            "cancer_id": self.state.cancer_id,
            "trace_class": self.trace_class,
            "stop_reason": self.stop_reason,
            "candidates": len(self.state.candidates),
            "shortlisted": [c.symbol for c in self.state.shortlisted_ever()],
            "killed": {c.symbol: c.kill_reasons for c in self.state.killed()},
            "evidence": self.state.store.summary(),
            "budget": self.state.budget.to_dict(),
            "trace_stats": self.trace.stats(),
        }


class SearchAndVerifyAgent:
    def __init__(
        self,
        cancer_id: str,
        *,
        agent_config: dict[str, Any] | None = None,
        registry: SourceRegistry | None = None,
        llm_client: Any | None = None,
    ) -> None:
        self.cancer_id = normalize_cancer_type(cancer_id)
        self.cancer_config = load_cancer_config(self.cancer_id)
        self.agent_config = agent_config or load_agent_config()
        self.registry = registry or build_registry()
        # 不传 llm_client 时自动构造一个默认客户端，而不是留 None。
        # LLMClient.is_configured() 在没配置 LLM_API_KEY/LLM_BASE_URL/LLM_MODEL
        # 时天然返回 False，所以这里不会改变现有行为；但如果用户已经设置了
        # 这三个环境变量并把 configs/agent.yaml 的 planner.mode / verification.mode
        # 改成 llm/hybrid，不这么做的话 CLI 跑出来的 Agent 永远拿到的是 None，
        # LLM 路径实质上永远走不到——这是接入 LLM 前必须先修的一个接线问题。
        self.llm_client = llm_client if llm_client is not None else _default_llm_client()

        budget_cfg = self.agent_config.get("budget") or {}
        self.state = RunState(
            cancer_id=self.cancer_id,
            cancer_config=self.cancer_config,
            agent_config=self.agent_config,
            budget=Budget(
                max_steps=int(budget_cfg.get("max_steps", 60)),
                max_source_calls=int(budget_cfg.get("max_source_calls", 200)),
                max_cost_units=int(budget_cfg.get("max_cost_units", 300)),
                max_seconds=int(budget_cfg.get("max_seconds", 900)),
                per_candidate_max_calls=int(budget_cfg.get("per_candidate_max_calls", 20)),
            ),
        )
        self.state.store.conflict_tolerance = float(
            (self.agent_config.get("evidence") or {}).get("conflict_tolerance", 2.0)
        )
        self.planner = build_planner(self.agent_config, self.registry, self.llm_client)
        self.verifier = build_verifier(self.agent_config, self.llm_client)
        self.trace = TraceRecorder(
            run_id=self.state.run_id,
            enabled=bool((self.agent_config.get("output") or {}).get("save_trace", True)),
        )
        self.kill_rules = ek.load_rules(self.agent_config)

    # =====================================================================
    # 阶段一：探测数据源覆盖（Q20）
    # =====================================================================

    def probe(self) -> ActionResult:
        self.state.phase = "probe"
        action = Action(kind=PROBE_SOURCES, reason="确认各数据源对该癌种的覆盖情况")
        results: list[dict[str, Any]] = []

        for source_id in self.registry.specs:
            source = get_source(self.registry, source_id)
            available = self.registry.is_available(source_id)
            if source is None:
                results.append({"source_id": source_id, "available": False,
                                "covered": False, "detail": "无实现"})
                continue
            probe = source.probe(self.cancer_config)
            results.append({
                "source_id": source_id,
                "available": available,
                "covered": probe.covered,
                "detail": probe.detail,
                "missing_config": probe.missing_config,
            })

        self.state.probe_results = results
        usable = [r["source_id"] for r in results if r["available"] and r["covered"]]

        uncovered = self.registry.uncovered_dimensions(self.state.required_dimensions())
        if uncovered:
            self.state.notes.append(
                f"以下必需维度当前没有任何可用数据源：{uncovered}；结论可信度将受限"
            )

        result = ActionResult(
            action=action, ok=bool(usable),
            detail=f"可用且覆盖本癌种的源：{usable}" if usable else "没有任何可用数据源",
            payload={"probe": results, "uncovered_dimensions": uncovered},
        )
        self.trace.record(self.state, result, event="probe")
        return result

    # =====================================================================
    # 阶段二：生成候选池（Q21：靠数据源，不靠 LLM 记忆）
    # =====================================================================

    def seed_candidates(self) -> ActionResult:
        self.state.phase = "seed"
        action = Action(kind=GENERATE_CANDIDATES,
                        reason="从数据源生成候选靶点池，不使用模型记忆列名单")

        pool: list[dict[str, Any]] = []
        for source_id in self.registry.specs:
            source = get_source(self.registry, source_id)
            if source is None or not self.registry.is_available(source_id):
                continue
            getter = getattr(source, "candidate_pool", None)
            if callable(getter):
                pool.extend(getter(self.cancer_config))

        added = 0
        for entry in pool:
            symbol = str(entry.get("symbol", "")).strip()
            if not symbol:
                continue
            identity = normalize_gene(symbol)
            if not identity.symbol:
                identity.symbol = symbol
            if not identity.aliases:
                identity.aliases = list(entry.get("aliases") or [])
            if not identity.protein_name:
                identity.protein_name = str(entry.get("protein_name", ""))
            candidate = Candidate(
                symbol=identity.symbol,
                identity=identity,
                discovered_by=str(entry.get("discovered_by", "")),
            )
            if self.state.add_candidate(candidate):
                added += 1

        result = ActionResult(
            action=action, ok=added > 0,
            detail=f"生成候选 {added} 个" if added else "未能生成任何候选（数据源均不可用）",
            payload={"candidates": [c.symbol for c in self.state.candidates.values()]},
        )
        self.trace.record(self.state, result, event="seed")
        return result

    # =====================================================================
    # 阶段三：动作执行
    # =====================================================================

    def execute(self, action: Action) -> ActionResult:
        if action.kind in (FETCH_EVIDENCE, SWITCH_SOURCE, RESOLVE_CONFLICT, REWRITE_QUERY):
            return self._do_fetch(action)
        if action.kind == SHORTLIST:
            chosen = apply_shortlist(self.state)
            return ActionResult(action=action, ok=bool(chosen),
                                detail=f"短名单：{chosen}", payload={"shortlist": chosen})
        if action.kind == EXPAND_CANDIDATES:
            return self._do_expand(action)
        if action.kind == STOP:
            return ActionResult(action=action, ok=True, detail=action.reason)
        return ActionResult(action=action, ok=False, detail=f"未知动作 {action.kind}")

    def _do_fetch(self, action: Action) -> ActionResult:
        candidate = self.state.candidates.get(action.target)
        if candidate is None:
            return ActionResult(action=action, ok=False, detail=f"候选 {action.target} 不存在")

        source = get_source(self.registry, action.source_id)
        spec = self.registry.get(action.source_id)
        if source is None or spec is None:
            candidate.note_failure(action.dimension, action.source_id)
            return ActionResult(action=action, ok=False,
                                detail=f"数据源 {action.source_id} 无实现")

        if structured_only_enabled(self.agent_config) and is_narrative_source(action.source_id):
            candidate.note_failure(action.dimension, action.source_id)
            return ActionResult(
                action=action, ok=False,
                detail=f"structured_only 禁止叙述性源 {action.source_id}",
            )

        request = FetchRequest(
            identity=candidate.identity,
            dimension=action.dimension,
            cancer_config=self.cancer_config,
            hints=dict(action.payload.get("hints") or {}),
        )

        check_deadline()
        if (self.state.budget.source_calls >= self.state.budget.max_source_calls or
                self.state.budget.cost_units + spec.cost_weight > self.state.budget.max_cost_units):
            return ActionResult(action=action, ok=False, detail="该动作超出剩余调用/成本预算",
                                payload={"budget_exhausted": True})
        self.state.budget.spend(spec.cost_weight)
        candidate.source_calls += 1

        try:
            observations = source.fetch(request)
        except SourceUnavailable as exc:
            candidate.note_failure(action.dimension, action.source_id)
            result = ActionResult(action=action, ok=False, detail=str(exc),
                                  cost=spec.cost_weight)
            self.trace.record(self.state, result, event="source_failure")
            return result
        except DeadlineExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 —— 单个源崩掉不该让整次运行失败
            candidate.note_failure(action.dimension, action.source_id)
            result = ActionResult(action=action, ok=False,
                                  detail=f"数据源异常：{type(exc).__name__}: {exc}",
                                  cost=spec.cost_weight)
            self.trace.record(self.state, result, event="source_failure")
            return result

        added = 0
        for obs in observations:
            obs.context = {**obs.context, "cancer_id": self.cancer_id,
                           "target_aliases": list(candidate.identity.aliases),
                           "target_protein_name": candidate.identity.protein_name}
            score = score_observation(obs)
            if self.state.store.add(obs, score) is not None:
                added += 1

        if added == 0:
            candidate.note_failure(action.dimension, action.source_id)

        return ActionResult(action=action, ok=added > 0,
                            detail=f"新增 {added} 条证据", evidence_added=added,
                            cost=spec.cost_weight)

    def _do_expand(self, action: Action) -> ActionResult:
        """扩展候选池：本地邻接表 +（在线时）STRING interaction partners。

        种子取存活且已有肿瘤表达证据的候选；扩展出的基因走 normalize_gene，
        找不到邻居时诚实返回失败，绝不编造。
        """
        from .expand import suggest_expansions

        seeds = [
            c.symbol for c in self.state.alive_candidates()
            if self.state.store.for_target(c.symbol, "tumor_expression")
        ]
        if not seeds:
            seeds = [c.symbol for c in self.state.alive_candidates()]
        suggestions = suggest_expansions(
            seeds,
            already=set(self.state.candidates),
            max_new=5,
        )
        if not suggestions:
            return ActionResult(action=action, ok=False,
                                detail="未找到可扩展的邻居候选（本地表与 STRING 均无命中）")

        added = 0
        new_symbols: list[str] = []
        for entry in suggestions:
            symbol = str(entry.get("symbol", "")).strip()
            if not symbol:
                continue
            identity = normalize_gene(symbol)
            if not identity.symbol:
                identity.symbol = symbol
            candidate = Candidate(
                symbol=identity.symbol,
                identity=identity,
                discovered_by=str(entry.get("discovered_by", "expand")),
            )
            if self.state.add_candidate(candidate):
                added += 1
                new_symbols.append(candidate.symbol)

        return ActionResult(
            action=action, ok=added > 0,
            detail=f"扩展新增 {added} 个候选：{', '.join(new_symbols)}" if added
                   else "邻居均已在候选池中",
            payload={"added": new_symbols},
        )

    # =====================================================================
    # 停止条件（Q17）
    # =====================================================================

    def _progress_fingerprint(self) -> tuple:
        """状态指纹：只要它变了，就说明这一轮推进了搜索。

        「没有新增证据」不等于「没有进展」：一次失败的取证会把该源标记为已试过，
        从而缩小后续的搜索空间。把它当成停滞会让 Agent 在还有事可做时提前退出。
        真正的停滞是指纹完全不变——那才意味着陷入了原地打转。
        """
        summary = self.state.store.summary()
        return (
            summary["total"],
            summary["verified"],
            summary["rejected"],
            sum(len(v) for c in self.state.candidates.values()
                for v in c.failed_dimensions.values()),
            len(self.state.killed()),
            len(self.state.shortlist()),
        )

    def should_stop(self) -> str:
        exhausted = self.state.budget.exhausted()
        if exhausted:
            return f"预算耗尽：{exhausted}"

        stopping = self.agent_config.get("stopping") or {}

        if self.state.no_progress_rounds >= int(stopping.get("no_progress_rounds", 5)):
            return f"连续 {self.state.no_progress_rounds} 轮无新增已验证证据"

        shortlist = self.state.shortlist()
        if shortlist:
            min_cov = float(stopping.get("min_coverage", 0.8))
            covs = [self.state.coverage(c.symbol) for c in shortlist]
            screening_ok = bool(covs) and min(covs) >= min_cov
            require_delivery = bool(stopping.get("require_delivery_verification", True))
            if screening_ok and not require_delivery:
                return f"短名单全部达到筛选覆盖率阈值 {min_cov}"
            if screening_ok:
                still_missing = [c.symbol for c in shortlist if self.state.delivery_missing(c.symbol)]
                bench_missing = [
                    c.symbol for c in self.state.candidates.values()
                    if c.status == STATUS_BENCHMARK and self.state.delivery_missing(c.symbol)
                ]
                if not still_missing and not bench_missing:
                    return f"短名单筛选覆盖率达标且递送验证维度已齐"

        if not self.state.alive_candidates():
            return "没有存活候选"

        return ""

    # =====================================================================
    # 主循环
    # =====================================================================

    def run(self) -> RunResult:
        started = self.state.budget.started_at
        with request_deadline(started + self.state.budget.max_seconds):
            try:
                return self._run()
            except DeadlineExceeded:
                self.state.stop_reason = f"预算耗尽：超时 {self.state.budget.max_seconds}s"
                self.trace.record(self.state, None, event="budget_exhausted",
                                  extra={"reason": self.state.stop_reason})
                return self._finalize(started)

    def _run(self) -> RunResult:
        started = time.monotonic()
        if self.state.phase != "search_verify":
            check_deadline()
            self.probe()
            check_deadline()
            seed = self.seed_candidates()
            if not seed.ok:
                self.state.phase = "done"
                self.state.stop_reason = "无法生成候选池"
                return self._finalize(started)
            self.state.phase = "search_verify"

        while True:
            self.state.budget.steps += 1

            stop_reason = self.should_stop()
            if stop_reason:
                self.state.stop_reason = stop_reason
                break

            before = self._progress_fingerprint()

            action = self.planner.plan(self.state)
            check_deadline()
            if action.kind == STOP:
                self.state.stop_reason = action.reason
                self.trace.record(self.state, ActionResult(action=action, ok=True,
                                                           detail=action.reason), event="stop")
                break

            result = self.execute(action)
            self.trace.record(self.state, result)
            if result.payload.get("budget_exhausted"):
                self.state.stop_reason = result.detail
                break

            # 验证 -> early-kill：顺序不能反。
            # 必须先剔除不可信证据，再用剩下的证据做淘汰判断，
            # 否则可能因为一条没被核对的证据误杀候选。
            verifications = verify_pending(self.state.store, self.verifier, self.agent_config)
            if verifications:
                self.trace.record(self.state, None, event="verification",
                                  extra={"verifications": [v.to_dict() for v in verifications]})
            killed = ek.apply_early_kill(self.state, self.kill_rules)
            for candidate in killed:
                self.trace.record(
                    self.state, None, event="early_kill",
                    extra={"target": candidate.symbol, "reasons": candidate.kill_reasons},
                )

            after = self._progress_fingerprint()
            self.state.no_progress_rounds = 0 if after != before else self.state.no_progress_rounds + 1
            self.after_step(result)

        return self._finalize(started)

    def after_step(self, result: ActionResult) -> None:
        """Durability hook after execution, verification and policy update."""

    # =====================================================================
    # 收尾：定路线、写契约
    # =====================================================================

    def _finalize(self, started: float) -> RunResult:
        self.state.phase = "finalize"
        verified_only = not bool((self.agent_config.get("ablation") or {}).get("disable_verifier"))

        rows: list[dict[str, str]] = []
        for candidate in self.state.candidates.values():
            if not self.state.store.for_target(candidate.symbol):
                continue                       # 完全没证据的候选不进契约表
            candidate.route_type = ek.classify_route(self.state, candidate)

            extra_notes: list[str] = []
            if candidate.kill_reasons:
                extra_notes.append("early-kill：" + "；".join(candidate.kill_reasons))
            if candidate.risk_reasons:
                extra_notes.append("risk：" + "；".join(candidate.risk_reasons))
            if candidate.block_reasons:
                prefix = "benchmark 降级" if candidate.status == STATUS_BENCHMARK else "受阻"
                extra_notes.append(f"{prefix}：" + "；".join(candidate.block_reasons))
            if candidate.status == STATUS_ACTIVE:
                extra_notes.append("未进入短名单，仅完成初筛")
            missing_delivery = self.state.delivery_missing(candidate.symbol)
            if candidate.was_shortlisted and missing_delivery and candidate.status != STATUS_KILLED:
                extra_notes.append("needs_more / evidence_missing：" + ",".join(missing_delivery))

            rows.append(build_row(
                self.state.store, candidate.symbol,
                route_type=candidate.route_type,
                aliases=candidate.identity.aliases or [candidate.symbol],
                protein_name=candidate.identity.protein_name,
                extra_notes=extra_notes,
                verified_only=verified_only,
            ))

        for candidate in self.state.shortlisted_ever():
            if candidate.status in {STATUS_KILLED, STATUS_BENCHMARK}:
                continue
            missing = self.state.delivery_missing(candidate.symbol)
            screening_ok = self.state.coverage(candidate.symbol) >= float(
                (self.agent_config.get("stopping") or {}).get("min_coverage", 0.8)
            )
            if screening_ok and not missing:
                candidate.status = STATUS_COMPLETED
            elif candidate.status == STATUS_SHORTLISTED:
                candidate.status = STATUS_NEEDS_MORE

        self.state.phase = "done"
        self.state.notes.append(f"总耗时 {time.monotonic() - started:.1f}s")

        return RunResult(
            state=self.state,
            trace=self.trace,
            contract_rows=rows,
            trace_class=self.trace.classify(self.state),
            stop_reason=self.state.stop_reason,
            llm_calls=self.llm_client.dump_calls() if self.llm_client is not None else [],
        )


def run_agent(cancer_id: str, **kwargs: Any) -> RunResult:
    return SearchAndVerifyAgent(cancer_id, **kwargs).run()
