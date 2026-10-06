"""ClinicalTrials.gov：临床先例。判断该靶点是否已有在研或获批的递送/靶向药物。

临床先例分的意义不是「有人做过所以好」，而是降低风险不确定性：
已有 phase 2/3 的靶点，说明它的可及性、安全窗至少在人体上被验证过一轮。
"""
from __future__ import annotations

from ..http import SourceUnavailable, build_url, request_json
from ..schema import Observation
from .base import DataSource, FetchRequest

_PHASE_ORDER = ["PHASE1", "PHASE2", "PHASE3", "PHASE4"]


class ClinicalTrialsSource(DataSource):
    source_id = "clinicaltrials"

    def fetch(self, request: FetchRequest) -> list[Observation]:
        self.require_configured()

        gene = request.identity.symbol or request.identity.query
        terms = list((request.cancer_config.get("sources") or {})
                     .get("literature", {}).get("query_terms") or [])
        cancer = terms[0] if terms else request.cancer_config.get("cancer_type_short", "")
        if request.dimension == "delivery_precedent":
            query = f"{gene} ADC OR conjugate OR GalNAc OR CAR-T {cancer}".strip()
        else:
            query = f"{gene} {cancer}".strip()

        # 已用真实响应核对：GET {base}/studies?query.term=...&pageSize=20
        url = build_url(self.spec.base_url, "studies", {
            "query.term": query,
            "pageSize": 20,
        })
        data = request_json(url, source_id=self.source_id)
        studies = (data or {}).get("studies") or []
        if not studies:
            # 「查了但确实没有」是有效证据：先例分应为低分而不是缺失
            return [self.make_observation(
                request,
                value=0.0,
                unit="ordinal",
                claim=f"ClinicalTrials.gov 未检索到 {gene} 在 {cancer} 的临床研究",
                source_ref=f"ClinicalTrials.gov query: {gene} {cancer}",
                dataset="clinicaltrials",
                context={"stage": "none", "n_studies": 0},
            )]

        best = "none"
        best_id = ""
        for study in studies:
            phases = (((study.get("protocolSection") or {}).get("designModule") or {})
                      .get("phases") or [])
            for phase in phases:
                if phase in _PHASE_ORDER and (
                    best == "none" or _PHASE_ORDER.index(phase) > _PHASE_ORDER.index(best.upper())
                ):
                    best = phase
                    best_id = (((study.get("protocolSection") or {}).get("identificationModule")
                                or {}).get("nctId") or "")

        stage_text = best.replace("PHASE", "phase ").lower() if best != "none" else "none"
        return [self.make_observation(
            request,
            value=None,
            unit="ordinal",
            claim=f"{gene} 在 {cancer} 的最高临床阶段为 {stage_text}（{len(studies)} 项研究）",
            source_ref=f"ClinicalTrials.gov {best_id}" if best_id else "ClinicalTrials.gov",
            dataset="clinicaltrials",
            context={"stage": stage_text, "n_studies": len(studies), "nct_id": best_id},
        )]
