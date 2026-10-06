"""数据源插件的抽象接口。

新增一个数据源只需要三步（详见 docs/07_扩展指南.md）：
  1. 在 configs/sources.yaml 声明它能提供哪些维度；
  2. 在本目录新建 <source>.py，继承 DataSource 实现 probe() 和 fetch()；
  3. 在 sources/__init__.py 的 BUILTIN_SOURCES 里登记。
Agent 侧不需要任何改动。

约定：
- fetch() 只返回 Observation（原始观测），绝不返回分数。
  打分是 scoring_maps 的事，这样「同一个数值在不同源之间打分口径一致」。
- 不可用时抛 SourceUnavailable，不要返回空列表——
  「查了但没有」和「没查成」对 Agent 是完全不同的信号，必须区分。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from ..http import SourceUnavailable
from ..normalize import GeneIdentity
from ..registry import SourceSpec
from ..schema import Observation


@dataclass
class FetchRequest:
    """一次取证请求。癌种相关信息全部通过 cancer_config 传入，源自己去取它需要的部分。"""

    identity: GeneIdentity
    dimension: str
    cancer_config: dict[str, Any]
    hints: dict[str, Any] = field(default_factory=dict)   # Agent 的查询改写提示

    @property
    def cancer_id(self) -> str:
        return str(self.cancer_config.get("cancer_id", ""))

    def source_config(self, source_id: str) -> dict[str, Any]:
        """取癌种配置里针对某个源的定位信息（如 cBioPortal 的 study_id）。"""
        return dict((self.cancer_config.get("sources") or {}).get(source_id) or {})


@dataclass
class ProbeResult:
    """探测某个源是否覆盖当前癌种（对应 Q20）。"""

    source_id: str
    covered: bool
    detail: str = ""
    missing_config: list[str] = field(default_factory=list)


class DataSource:
    """所有数据源的基类。"""

    source_id: str = ""

    def __init__(self, spec: SourceSpec) -> None:
        self.spec = spec

    # ---------------- 能力与可用性 ----------------

    def provides(self) -> list[str]:
        return self.spec.provides

    def api_key(self) -> str | None:
        env = self.spec.api_key_env
        return os.environ.get(env) if env else None

    def require_configured(self) -> None:
        """统一的「没接就明确说没接」检查。"""
        if not self.spec.enabled:
            raise SourceUnavailable(
                self.spec.source_id,
                "数据源未接入（configs/sources.yaml 中 api.enabled=false）",
            )
        if not self.spec.base_url and not self.spec.api.get("local_file") \
                and not self.spec.api.get("data_dir"):
            raise SourceUnavailable(self.spec.source_id, "缺少 base_url / local_file / data_dir 配置")
        if self.spec.api_key_required and self.spec.api_key_env and not self.api_key():
            raise SourceUnavailable(
                self.spec.source_id,
                f"缺少环境变量 {self.spec.api_key_env}",
            )

    # ---------------- 子类实现 ----------------

    def probe(self, cancer_config: dict[str, Any]) -> ProbeResult:
        """该源是否覆盖此癌种。默认按癌种配置里是否填了本源的定位信息判断。"""
        needed = dict((cancer_config.get("sources") or {}).get(self.spec.source_id) or {})
        missing = [k for k, v in needed.items() if not v]
        if needed and missing:
            return ProbeResult(
                source_id=self.spec.source_id,
                covered=False,
                detail=f"癌种配置缺少定位信息：{missing}",
                missing_config=missing,
            )
        return ProbeResult(source_id=self.spec.source_id, covered=True)

    def fetch(self, request: FetchRequest) -> list[Observation]:
        raise NotImplementedError

    # ---------------- 工具 ----------------

    def make_observation(
        self,
        request: FetchRequest,
        *,
        value: float | None,
        unit: str,
        claim: str,
        source_ref: str = "",
        dataset: str = "",
        context: dict[str, Any] | None = None,
    ) -> Observation:
        """统一构造 Observation，自动带上本源的 layer/reliability/独立性分组。"""
        return Observation(
            target=request.identity.symbol or request.identity.query,
            dimension=request.dimension,
            value=value,
            unit=unit,
            claim=claim,
            source_id=self.spec.source_id,
            source_dataset=dataset,
            source_ref=source_ref,
            layer=self.spec.layer,
            reliability=self.spec.reliability,
            independence_group=self.spec.independence_group,
            context=context or {},
        )


class NotImplementedSource(DataSource):
    """占位数据源：配置里声明了但实现还没写时使用，行为是明确报「未接入」。

    比起「悄悄返回空」，明确抛错能让 Agent 走 fallback，也能让 availability_report
    如实反映系统当前的能力边界。
    """

    def fetch(self, request: FetchRequest) -> list[Observation]:
        raise SourceUnavailable(self.spec.source_id, "该数据源的 fetch() 尚未实现")
