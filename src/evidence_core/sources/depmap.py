"""DepMap：细胞系表达。作为肿瘤表达的旁证与 evidence_level 加成。

DepMap 没有稳定的公开 JSON 接口，所以这里走「本地文件」路径：
用户手动下载 OmicsExpressionProteinCodingGenesTPMLogp1.csv 与 Model.csv 后，
在 configs/sources.yaml 的 depmap.api.local_file 指向目录即可。

癌种绑定通过癌种配置 sources.depmap.{lineage, oncotree_code} 完成，
不再像老模型那样把 HCC/liver 细胞系名单写死在代码里。
"""
from __future__ import annotations

import csv
import statistics
from pathlib import Path
from typing import Any

from ..http import SourceUnavailable
from ..schema import Observation
from .base import DataSource, FetchRequest, ProbeResult


class DepmapSource(DataSource):
    source_id = "depmap"

    def _data_dir(self) -> Path | None:
        raw = self.spec.api.get("local_file") or ""
        return Path(raw) if raw else None

    def probe(self, cancer_config: dict[str, Any]) -> ProbeResult:
        cfg = dict((cancer_config.get("sources") or {}).get("depmap") or {})
        if not cfg.get("lineage") and not cfg.get("oncotree_code"):
            return ProbeResult(self.source_id, False,
                               detail="癌种配置未提供 depmap.lineage / oncotree_code",
                               missing_config=["lineage", "oncotree_code"])
        d = self._data_dir()
        if not d or not d.exists():
            return ProbeResult(self.source_id, False,
                               detail="未配置本地 DepMap 数据目录（api.local_file）")
        return ProbeResult(self.source_id, True)

    def _select_models(self, model_csv: Path, lineage: str, oncotree: str) -> list[str]:
        """按癌种配置筛出属于该癌种的细胞系 ID。"""
        selected: list[str] = []
        with model_csv.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if oncotree and row.get("OncotreeCode", "") == oncotree:
                    selected.append(row.get("ModelID", ""))
                elif lineage and lineage.lower() in row.get("OncotreeLineage", "").lower():
                    selected.append(row.get("ModelID", ""))
        return [m for m in selected if m]

    def fetch(self, request: FetchRequest) -> list[Observation]:
        self.require_configured()
        d = self._data_dir()
        if not d or not d.exists():
            raise SourceUnavailable(self.source_id, "DepMap 本地数据目录不存在")

        cfg = request.source_config("depmap")
        model_csv = d / "Model.csv"
        expr_csv = d / "OmicsExpressionProteinCodingGenesTPMLogp1.csv"
        if not model_csv.exists() or not expr_csv.exists():
            raise SourceUnavailable(self.source_id, "缺少 Model.csv 或 OmicsExpression CSV")

        models = set(self._select_models(model_csv, cfg.get("lineage", ""),
                                         cfg.get("oncotree_code", "")))
        if not models:
            raise SourceUnavailable(self.source_id, "该癌种在 DepMap 中无匹配细胞系")

        gene = request.identity.symbol
        values: list[float] = []
        with expr_csv.open(newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            header = next(reader, [])
            # 列名形如 "GPC3 (2719)"
            col = next((i for i, h in enumerate(header) if h.split(" ")[0] == gene), None)
            if col is None:
                raise SourceUnavailable(self.source_id, f"DepMap 表中无基因 {gene}")
            for row in reader:
                if row and row[0] in models and row[col]:
                    try:
                        values.append(float(row[col]))
                    except ValueError:
                        continue

        if not values:
            raise SourceUnavailable(self.source_id, f"{gene} 在该癌种细胞系中无表达数据")

        med = statistics.median(values)
        return [self.make_observation(
            request,
            value=med,
            unit="log2(TPM+1)",
            claim=(f"DepMap {cfg.get('oncotree_code') or cfg.get('lineage')} 细胞系中 "
                   f"{gene} median log2(TPM+1)={med:.3f}（n={len(values)}）"),
            source_ref=f"DepMap {gene} {cfg.get('oncotree_code') or cfg.get('lineage')}",
            dataset="depmap",
            context={"n": len(values), "distribution": values},
        )]
