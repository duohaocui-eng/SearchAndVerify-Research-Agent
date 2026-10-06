"""离线 fixture 源：让整条链路在没有任何 API 的情况下也能端到端跑通。

它有三个用途，缺一不可：
1. 现在所有真实源都还没接，需要它来验证「Agent 决策逻辑」本身是否正确；
2. 评测与单元测试需要确定性输入，真实 API 会漂移；
3. 真实源全部失败时的兜底，此时报告会显式标注证据降级。

诚实性设计：
- source_id 始终以 "fixtures" 开头，报告里一眼能看出不是真实数据；
- 每条 fixture 观测通过 `emulates` 声明它模拟的是哪个真实源，
  借此复用该源的 layer 与 independence_group，让冲突检测、独立来源计数
  这些逻辑在离线态也能被真实地演练到——否则测出来的是假通过。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT
from ..http import SourceUnavailable
from ..schema import Observation
from .base import DataSource, FetchRequest, ProbeResult


class FixtureSource(DataSource):
    source_id = "fixtures"

    def _data_dir(self) -> Path:
        raw = str(self.spec.api.get("data_dir") or "data/fixtures")
        p = Path(raw)
        return p if p.is_absolute() else PROJECT_ROOT / p

    def _load(self, cancer_id: str) -> dict[str, Any]:
        path = self._data_dir() / f"{cancer_id}.json"
        if not path.exists():
            raise SourceUnavailable(
                self.source_id,
                f"无 {cancer_id} 的 fixture 数据（期望 {path.relative_to(PROJECT_ROOT)}）",
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def probe(self, cancer_config: dict[str, Any]) -> ProbeResult:
        cancer_id = str(cancer_config.get("cancer_id", ""))
        path = self._data_dir() / f"{cancer_id}.json"
        if not path.exists():
            return ProbeResult(self.source_id, False, detail=f"无 {cancer_id} fixture")
        return ProbeResult(self.source_id, True, detail=f"使用 {path.name}")

    def candidate_pool(self, cancer_config: dict[str, Any]) -> list[dict[str, Any]]:
        """离线候选池。真实运行时这一步由多个数据源共同产出。"""
        try:
            data = self._load(str(cancer_config.get("cancer_id", "")))
        except SourceUnavailable:
            return []
        return list(data.get("candidate_pool") or [])

    def fetch(self, request: FetchRequest) -> list[Observation]:
        data = self._load(request.cancer_id)
        target = request.identity.symbol or request.identity.query

        records = [
            r for r in data.get("observations", [])
            if r.get("target", "").upper() == target.upper()
            and r.get("dimension") == request.dimension
        ]
        if not records:
            raise SourceUnavailable(
                self.source_id,
                f"fixture 中无 {target} / {request.dimension} 的数据",
            )

        observations: list[Observation] = []
        for rec in records:
            emulates = rec.get("emulates", "")
            observations.append(Observation(
                target=target,
                dimension=request.dimension,
                value=rec.get("value"),
                unit=rec.get("unit", ""),
                claim=rec.get("claim", ""),
                # 明确标记来源是 fixture，同时保留它模拟的真实源
                source_id=f"fixtures:{emulates}" if emulates else "fixtures",
                source_dataset=rec.get("dataset", "fixture"),
                source_ref=rec.get("source_ref", ""),
                layer=rec.get("layer", self.spec.layer),
                reliability=rec.get("reliability", self.spec.reliability),
                # 复用被模拟源的独立性分组，让独立来源计数在离线态也真实
                independence_group=emulates or self.spec.independence_group,
                context=dict(rec.get("context") or {}),
            ))
        return observations
