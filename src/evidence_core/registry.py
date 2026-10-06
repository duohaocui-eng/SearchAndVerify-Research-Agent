"""数据源注册表：Agent 的「我该问谁」。

Agent 从不写死数据源名。它说的是「我要 internalization 维度的证据」，
由本模块按 provides / 可用性 / 可靠性 / 成本 挑一个源，不可用就顺 fallback 链往下走。

这样带来的直接好处：
- 新增数据源不需要改 Agent 一行代码（改 configs/sources.yaml + 加一个 parser）；
- API 全没接的现在，系统自动全部落到 fixtures 源，端到端流程照样能跑；
- 换癌种时若某个源不覆盖该癌种，probe 阶段就能发现并换源（Q20）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .config import load_source_registry

RELIABILITY_RANK = {"high": 3, "medium": 2, "low": 1}


@dataclass
class SourceSpec:
    """一个数据源的声明。来自 configs/sources.yaml，不含实现。"""

    source_id: str
    name: str
    provides: list[str]
    layer: str
    reliability: str
    independence_group: str
    cost_weight: int
    api: dict[str, Any]
    fallback: list[str]
    notes: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.api.get("enabled", False))

    @property
    def base_url(self) -> str:
        return str(self.api.get("base_url", "") or "")

    @property
    def api_key_env(self) -> str:
        return str(self.api.get("api_key_env", "") or "")

    @property
    def api_key_required(self) -> bool:
        """key 是否是硬性前提。默认否——大多数源的 key 只是提升限速等可选优化
        （如 pubmed 的 NCBI_API_KEY），缺了也该正常工作，不该整个源直接不可用。"""
        return bool(self.api.get("api_key_required", False))

    def can_provide(self, dimension: str) -> bool:
        return dimension in self.provides


@dataclass
class SourceRegistry:
    specs: dict[str, SourceSpec] = field(default_factory=dict)
    # source_id -> fetcher 实现，由 sources/__init__.py 注册
    fetchers: dict[str, Callable[..., Any]] = field(default_factory=dict)

    # ---------------- 构造 ----------------

    @classmethod
    def from_config(cls, config: dict[str, Any] | None = None) -> "SourceRegistry":
        raw = config if config is not None else load_source_registry()
        specs: dict[str, SourceSpec] = {}
        for source_id, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            provides = entry.get("provides") or []
            if isinstance(provides, str):
                provides = [provides]
            fallback = entry.get("fallback") or []
            if isinstance(fallback, str):
                fallback = [fallback]
            specs[source_id] = SourceSpec(
                source_id=source_id,
                name=str(entry.get("name", source_id)),
                provides=list(provides),
                layer=str(entry.get("layer", "")),
                reliability=str(entry.get("reliability", "medium")),
                independence_group=str(entry.get("independence_group", source_id)),
                cost_weight=int(entry.get("cost_weight", 1) or 1),
                api=dict(entry.get("api") or {}),
                fallback=list(fallback),
                notes=str(entry.get("notes", "")),
            )
        return cls(specs=specs)

    def register_fetcher(self, source_id: str, fetcher: Callable[..., Any]) -> None:
        self.fetchers[source_id] = fetcher

    # ---------------- 查询 ----------------

    def get(self, source_id: str) -> SourceSpec | None:
        return self.specs.get(source_id)

    def is_available(self, source_id: str) -> bool:
        """可用 = 配置启用 + 有实现。缺任一都算不可用，走 fallback。"""
        spec = self.specs.get(source_id)
        return bool(spec and spec.enabled and source_id in self.fetchers)

    def providers_for(self, dimension: str, available_only: bool = True) -> list[SourceSpec]:
        """能提供该维度的源，按 可靠性降序 -> 成本升序 排列。"""
        out = [s for s in self.specs.values() if s.can_provide(dimension)]
        if available_only:
            out = [s for s in out if self.is_available(s.source_id)]
        return sorted(
            out,
            key=lambda s: (-RELIABILITY_RANK.get(s.reliability, 0), s.cost_weight),
        )

    def resolve_chain(self, source_id: str, dimension: str, max_depth: int = 4) -> list[str]:
        """给定首选源，展开成实际可尝试的链（含 fallback，已去重、已过滤不可用）。"""
        chain: list[str] = []
        seen: set[str] = set()

        def walk(sid: str, depth: int) -> None:
            if depth > max_depth or sid in seen:
                return
            seen.add(sid)
            spec = self.specs.get(sid)
            if not spec:
                return
            if self.is_available(sid) and spec.can_provide(dimension):
                chain.append(sid)
            for nxt in spec.fallback:
                walk(nxt, depth + 1)

        walk(source_id, 0)
        return chain

    def plan_for_dimension(self, dimension: str) -> list[str]:
        """某个维度的完整尝试顺序：首选源及其 fallback 链，再补其他可用源。"""
        preferred = self.providers_for(dimension, available_only=True)
        order: list[str] = []
        for spec in preferred:
            for sid in self.resolve_chain(spec.source_id, dimension):
                if sid not in order:
                    order.append(sid)
        # 兜底：fixtures 永远排最后，保证链路不断
        if "fixtures" in self.specs and self.is_available("fixtures"):
            if "fixtures" not in order and self.specs["fixtures"].can_provide(dimension):
                order.append("fixtures")
        return order

    # ---------------- 诊断 ----------------

    def availability_report(self) -> list[dict[str, Any]]:
        """哪些源开了、哪些没开、为什么没开。CLI 的 `sources` 子命令用它。"""
        rows: list[dict[str, Any]] = []
        for spec in sorted(self.specs.values(), key=lambda s: s.source_id):
            if not spec.enabled:
                reason = "配置未启用（api.enabled=false，待接入）"
            elif spec.source_id not in self.fetchers:
                reason = "配置已启用但缺少 fetcher 实现"
            else:
                reason = ""
            rows.append({
                "source_id": spec.source_id,
                "name": spec.name,
                "available": self.is_available(spec.source_id),
                "reason": reason,
                "provides": spec.provides,
                "reliability": spec.reliability,
                "cost_weight": spec.cost_weight,
            })
        return rows

    def uncovered_dimensions(self, dimensions: list[str]) -> list[str]:
        """哪些维度当前没有任何可用源——probe 阶段用来提前报警。"""
        return [d for d in dimensions if not self.providers_for(d, available_only=True)]
