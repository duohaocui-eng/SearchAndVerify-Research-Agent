"""Human Protein Atlas：蛋白层表达与亚细胞定位。

它是判断「膜定位」的首选源——递送靶点如果不在细胞表面，后面所有工作都白做，
所以 surface_localization 维度优先问 HPA（protein 层）而不是任何 RNA 源。
"""
from __future__ import annotations

from typing import Any

from ..http import SourceUnavailable, build_url, request_json
from ..schema import Observation
from .base import DataSource, FetchRequest


def _normalize_tissue_key(name: str) -> str:
    """把组织名统一成 "小写+空格" 形式，抹平 GTEx 风格（Whole_Blood）与
    HPA 风格（whole blood）的命名差异，做尽力而为的跨源组织名匹配。"""
    return name.strip().lower().replace("_", " ").replace("-", " ")


def _lookup_tissue(tissue_map: dict[str, Any], tissue: str) -> Any | None:
    if not tissue or not tissue_map:
        return None
    key = _normalize_tissue_key(tissue)
    for k, v in tissue_map.items():
        if _normalize_tissue_key(str(k)) == key:
            return v
    return None


class HpaSource(DataSource):
    source_id = "hpa"

    def fetch(self, request: FetchRequest) -> list[Observation]:
        self.require_configured()

        ensembl = request.identity.ensembl_id
        if not ensembl:
            raise SourceUnavailable(
                self.source_id, f"{request.identity.query} 缺少 ensembl_id"
            )

        # HPA 按基因返回一个扁平 JSON（GET https://www.proteinatlas.org/{ensembl}.json）。
        # 真实字段名与老模型/直觉命名不同（已用真实响应核对过，见 docs/03）：
        #   "Subcellular main location"  -> list[str]，surface_localization 最可靠的字段
        #   "RNA tissue specific nTPM"   -> dict[组织名 -> nTPM]，但只在该基因被判定为
        #                                   该组织"富集/特异"时才有条目，覆盖率有限，
        #                                   查不到就该老实报 SourceUnavailable 走 fallback，
        #                                   不能当成"该组织不表达"。
        #   "RNA cancer specific pTPM"   -> dict[HPA 癌种名 -> pTPM]，同样只在
        #                                   cancer-enhanced 时有条目，且键名是 HPA 自己的
        #                                   癌种命名（如 "Liver Hepatocellular Carcinoma
        #                                   (TCGA)"），与本项目 cancer_type_full 不保证一致，
        #                                   暂不消费，tumor_expression 以 cBioPortal/DepMap 为准。
        url = build_url(self.spec.base_url, f"{ensembl}.json")
        data = request_json(url, source_id=self.source_id)
        if not isinstance(data, dict):
            raise SourceUnavailable(self.source_id, "HPA 返回格式异常")

        observations: list[Observation] = []
        dim = request.dimension

        if dim == "surface_localization":
            locations = data.get("Subcellular main location") or data.get("subcellularLocation") or []
            if isinstance(locations, str):
                locations = [locations]
            loc_text = ", ".join(locations)
            if loc_text:
                observations.append(self.make_observation(
                    request,
                    value=None,
                    unit="ordinal",
                    claim=f"HPA 亚细胞定位：{loc_text}",
                    source_ref=f"HPA {ensembl} subcellular location",
                    dataset="hpa",
                    context={"location_text": loc_text},
                ))

        elif dim == "normal_reference_expression":
            tissue = request.cancer_config.get("reference_normal_tissue", "")
            tissue_map = data.get("RNA tissue specific nTPM") or {}
            value = _lookup_tissue(tissue_map, tissue) if isinstance(tissue_map, dict) else None
            if value is not None:
                observations.append(self.make_observation(
                    request,
                    value=float(value),
                    unit="nTPM",
                    claim=f"HPA {request.identity.symbol} 在 {tissue} 的富集表达 {value} nTPM",
                    source_ref=f"HPA {ensembl} {tissue}",
                    dataset="hpa",
                    context={"tissue": tissue},
                ))

        elif dim == "offtarget_expression":
            # 脱靶惩罚要的是"排除参照正常组织后，脱靶最严重的那个组织"，
            # 不是查某一个固定组织，所以在整张富集表里取（除参照组织外）最大值。
            ref_tissue = request.cancer_config.get("reference_normal_tissue", "")
            tissue_map = data.get("RNA tissue specific nTPM") or {}
            candidates = [
                (t, float(v)) for t, v in (tissue_map or {}).items()
                if _normalize_tissue_key(str(t)) != _normalize_tissue_key(ref_tissue or "")
            ]
            if candidates:
                worst_tissue, worst_value = max(candidates, key=lambda tv: tv[1])
                observations.append(self.make_observation(
                    request,
                    value=worst_value,
                    unit="nTPM",
                    claim=f"HPA {request.identity.symbol} 脱靶最高富集组织为 {worst_tissue}，{worst_value} nTPM",
                    source_ref=f"HPA {ensembl} {worst_tissue}",
                    dataset="hpa",
                    context={"tissue": worst_tissue},
                ))

        if not observations:
            raise SourceUnavailable(self.source_id, f"HPA 未提供 {dim} 所需字段")
        return observations
