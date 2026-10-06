"""统一的数据源策略：structured_only 等开关必须在所有取证路径生效。

Planner、冲突仲裁、主循环 fetch 都走这里，避免某一条路径绕过 ablation。
"""
from __future__ import annotations

from typing import Any, Iterable

from .registry import SourceRegistry

NARRATIVE_SOURCE_IDS = {"pubmed", "literature", "europepmc"}
STRUCTURED_LAYERS = {"rna", "protein", "mixed", "clinical"}


def base_source_id(source_id: str) -> str:
    return source_id.split(":", 1)[0]


def is_narrative_source(source_id: str) -> bool:
    """PubMed 等叙述性源。fixtures:literature 仍算 fixture 降级，不算 live pubmed。"""
    return base_source_id(source_id) in NARRATIVE_SOURCE_IDS


def structured_only_enabled(agent_config: dict[str, Any] | None) -> bool:
    return bool(((agent_config or {}).get("ablation") or {}).get("structured_only"))


def filter_source_ids(
    source_ids: Iterable[str],
    registry: SourceRegistry,
    agent_config: dict[str, Any] | None,
) -> list[str]:
    """按当前 ablation 过滤可用源。structured_only 时禁止叙述性源。"""
    out: list[str] = []
    structured = structured_only_enabled(agent_config)
    for sid in source_ids:
        if structured and is_narrative_source(sid):
            continue
        spec = registry.get(sid)
        if structured and spec is not None and spec.layer not in STRUCTURED_LAYERS:
            continue
        out.append(sid)
    return out


def sources_already_used(store: Any, target: str, dimension: str) -> set[str]:
    """已对该 (target, dimension) 出过证据的源，避免成功后再打同一源。"""
    used: set[str] = set()
    for item in store.for_target(target, dimension):
        used.add(base_source_id(item.source_id))
        if ":" in item.source_id:
            used.add(item.source_id.split(":", 1)[1])
        if item.independence_group:
            used.add(item.independence_group)
    return used


def allowed_sources(
    registry: SourceRegistry,
    dimension: str,
    agent_config: dict[str, Any] | None,
    *,
    exclude: Iterable[str] | None = None,
) -> list[str]:
    tried = set(exclude or [])
    planned = [s for s in registry.plan_for_dimension(dimension) if s not in tried]
    return filter_source_ids(planned, registry, agent_config)
