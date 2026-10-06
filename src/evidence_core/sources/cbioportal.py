"""cBioPortal：肿瘤队列 bulk RNA 表达。提供 tumor_expression / heterogeneity。

去 HCC 化要点：
老模型把 "lihc_tcga_rna_seq_v2_mrna" 直接写在 URL 里。这里改为从癌种配置
sources.cbioportal.{study_id, molecular_profile_id, sample_list_id} 读取；
换癌种只改癌种配置文件。

打分口径的重要权衡（详见 docs/02_关键权衡与设计决策.md 决策 4）：
本源同时返回队列内的分布（而不只是中位数），让 scoring_maps 能用分位数定标，
避免老模型那种「按 GPC3≈6500 手工校准常数」在换癌种/换平台后失真。
"""
from __future__ import annotations

import statistics
from typing import Any

from ..http import SourceUnavailable, build_url, request_json
from ..schema import Observation
from .base import DataSource, FetchRequest, ProbeResult


class CbioportalSource(DataSource):
    source_id = "cbioportal"

    def probe(self, cancer_config: dict[str, Any]) -> ProbeResult:
        cfg = dict((cancer_config.get("sources") or {}).get("cbioportal") or {})
        missing = [k for k in ("study_id", "molecular_profile_id") if not cfg.get(k)]
        if missing:
            return ProbeResult(
                self.source_id, False,
                detail=f"癌种配置缺少 cBioPortal 队列定位：{missing}；"
                       f"可在 https://www.cbioportal.org/datasets 查到对应 study_id",
                missing_config=missing,
            )
        return ProbeResult(self.source_id, True, detail=f"使用队列 {cfg['study_id']}")

    def fetch(self, request: FetchRequest) -> list[Observation]:
        self.require_configured()

        cfg = request.source_config("cbioportal")
        profile = cfg.get("molecular_profile_id")
        sample_list = cfg.get("sample_list_id") or profile
        if not profile:
            raise SourceUnavailable(self.source_id, "癌种配置未提供 molecular_profile_id")

        entrez = request.identity.entrez_id
        if not entrez:
            raise SourceUnavailable(
                self.source_id, f"{request.identity.query} 缺少 entrez_id，无法查询 cBioPortal"
            )

        # 已用真实响应核对：端点与老模型一致，只是 profile 来自配置。
        url = build_url(self.spec.base_url,
                        f"molecular-profiles/{profile}/molecular-data/fetch")
        rows = request_json(
            url,
            source_id=self.source_id,
            method="POST",
            body={"entrezGeneIds": [int(entrez)], "sampleListId": sample_list},
            timeout=40,
        )
        values = [float(r["value"]) for r in rows if r.get("value") is not None]
        if not values:
            raise SourceUnavailable(self.source_id, f"队列 {profile} 中无 {entrez} 的表达数据")

        med = statistics.median(values)
        observations = [self.make_observation(
            request,
            value=med,
            unit="RSEM",
            claim=(f"{request.identity.symbol} 在 {cfg.get('study_id')} 队列中 "
                   f"bulk RNA median={med:.1f}（n={len(values)}）"),
            source_ref=f"cBioPortal {profile} median {med:.1f}",
            dataset=str(profile),
            # 把分布带出去，供 scoring_maps 做分位数定标
            context={"distribution": values, "n": len(values),
                     "study_id": cfg.get("study_id")},
        )]

        if request.dimension == "heterogeneity" and len(values) >= 10:
            ordered = sorted(values)
            q1 = ordered[len(ordered) // 4]
            q3 = ordered[3 * len(ordered) // 4]
            spread = (q3 - q1) / (med + 1e-9) if med > 0 else 0.0
            observations.append(self.make_observation(
                request,
                value=round(spread, 3),
                unit="IQR/median",
                claim=(f"{request.identity.symbol} 队列内表达离散度 IQR/median={spread:.2f}，"
                       f"提示患者间表达异质性"),
                source_ref=f"cBioPortal {profile} IQR",
                dataset=str(profile),
                context={"q1": q1, "q3": q3, "median": med},
            ))
        return observations
