"""数据源装配：把 configs/sources.yaml 的声明与具体实现接起来。

新增数据源时唯一需要改的地方就是 BUILTIN_SOURCES 这张表。
"""
from __future__ import annotations

import os
from typing import Type

from ..registry import SourceRegistry
from .base import DataSource, FetchRequest, NotImplementedSource, ProbeResult
from .cbioportal import CbioportalSource
from .clinicaltrials import ClinicalTrialsSource
from .depmap import DepmapSource
from .fixtures import FixtureSource
from .gtex import GtexSource
from .hpa import HpaSource
from .pubmed import PubmedSource
from .uniprot import UniprotSource

BUILTIN_SOURCES: dict[str, Type[DataSource]] = {
    "hpa": HpaSource,
    "gtex": GtexSource,
    "cbioportal": CbioportalSource,
    "tcga_gdc": NotImplementedSource,   # 声明了但未实现，availability_report 会如实标注
    "depmap": DepmapSource,
    "uniprot": UniprotSource,
    "pubmed": PubmedSource,
    "clinicaltrials": ClinicalTrialsSource,
    "fixtures": FixtureSource,
}


def build_registry(config: dict | None = None) -> SourceRegistry:
    """构造注册表并挂上所有可用实现。

    SNV_TEST_OFFLINE 设置时（由 tests/conftest.py 自动设置），除 fixtures 外的
    所有真实源都不注册 fetcher，与 configs/sources.yaml 里 enabled 的取值无关。
    这样测试永远是确定性的离线运行，不受"真实数据源已接入"这一事实影响；
    生产环境不设这个变量，行为完全由 sources.yaml 决定。
    """
    offline = bool(os.environ.get("SNV_TEST_OFFLINE"))
    registry = SourceRegistry.from_config(config)
    for source_id, spec in registry.specs.items():
        if offline and source_id != "fixtures":
            continue
        impl = BUILTIN_SOURCES.get(source_id)
        if impl is None:
            continue
        registry.register_fetcher(source_id, impl(spec))
    return registry


def get_source(registry: SourceRegistry, source_id: str) -> DataSource | None:
    obj = registry.fetchers.get(source_id)
    return obj if isinstance(obj, DataSource) else None


__all__ = [
    "BUILTIN_SOURCES",
    "DataSource",
    "FetchRequest",
    "ProbeResult",
    "build_registry",
    "get_source",
]
