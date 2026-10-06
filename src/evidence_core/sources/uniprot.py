"""UniProt：蛋白拓扑结构。提供 surface_localization / extracellular_accessibility / shedding。

对递送靶点特别有用的三类信息：
- 跨膜域与拓扑：判断胞外域大小，决定配体能不能结合；
- GPI 锚定：影响内吞路径；
- 分泌/可溶：没有跨膜或 GPI 且注释为 secreted 时，这是 hard-negative，不是「查不到」。
"""
from __future__ import annotations

from ..http import SourceUnavailable, build_url, request_json
from ..schema import Observation
from .base import DataSource, FetchRequest


def secreted_location_from_record(data: dict) -> str | None:
    """从 UniProt JSON 抽出分泌/胞外定位。无则返回 None。不认基因名。"""
    if not isinstance(data, dict):
        return None
    for kw in data.get("keywords") or []:
        name = str(kw.get("name") or "").strip().lower()
        if name == "secreted":
            return "secreted"
    for comment in data.get("comments") or []:
        if str(comment.get("commentType") or "").lower() != "subcellular location":
            continue
        for loc in comment.get("subcellularLocations") or []:
            value = str((loc.get("location") or {}).get("value") or "")
            low = value.lower()
            if "secreted" in low or "extracellular space" in low:
                return value or "secreted"
    return None


class UniprotSource(DataSource):
    source_id = "uniprot"

    def fetch(self, request: FetchRequest) -> list[Observation]:
        self.require_configured()

        acc = request.identity.uniprot_id
        if not acc:
            raise SourceUnavailable(self.source_id, f"{request.identity.query} 缺少 uniprot_id")

        # 已用真实响应核对：GET https://rest.uniprot.org/uniprotkb/{acc}.json
        # 关键字段：features[] 中 type 为 "Transmembrane" / "Topological domain" /
        #           "Lipidation"(GPI-anchor)；comments/keywords 中的 Secreted。
        url = build_url(self.spec.base_url, f"uniprotkb/{acc}.json")
        data = request_json(url, source_id=self.source_id)
        if not isinstance(data, dict):
            raise SourceUnavailable(self.source_id, "UniProt 返回格式异常")
        features = data.get("features", []) or []

        observations: list[Observation] = []
        tm = [f for f in features if f.get("type") == "Transmembrane"]
        extracellular = [
            f for f in features
            if f.get("type") == "Topological domain"
            and "extracellular" in str(f.get("description", "")).lower()
        ]
        gpi = [f for f in features if "gpi" in str(f.get("description", "")).lower()]
        secreted = secreted_location_from_record(data)

        if request.dimension == "surface_localization":
            if tm or gpi:
                kind = "GPI-anchor" if gpi else f"{len(tm)} 个跨膜域"
                observations.append(self.make_observation(
                    request,
                    value=None,
                    unit="ordinal",
                    claim=f"UniProt {acc} 拓扑显示 {kind}，为膜蛋白",
                    source_ref=f"UniProt {acc}",
                    dataset="uniprotkb",
                    context={"location_text": "plasma membrane"},
                ))
            elif secreted:
                observations.append(self.make_observation(
                    request,
                    value=None,
                    unit="ordinal",
                    claim=f"UniProt {acc} 注释为 {secreted}，无跨膜/GPI，非细胞表面递送句柄",
                    source_ref=f"UniProt {acc}",
                    dataset="uniprotkb",
                    context={"location_text": "secreted"},
                ))

        if request.dimension == "extracellular_accessibility":
            if extracellular and (tm or gpi):
                spans = []
                for f in extracellular:
                    loc = f.get("location", {})
                    start = (loc.get("start") or {}).get("value")
                    end = (loc.get("end") or {}).get("value")
                    if start and end:
                        spans.append(int(end) - int(start))
                longest = max(spans) if spans else 0
                observations.append(self.make_observation(
                    request,
                    value=float(longest),
                    unit="aa",
                    claim=f"UniProt {acc} 最大胞外域长度约 {longest} aa",
                    source_ref=f"UniProt {acc} topological domain",
                    dataset="uniprotkb",
                    context={"extracellular_span_aa": longest},
                ))
            elif secreted and not (tm or gpi):
                observations.append(self.make_observation(
                    request,
                    value=0.0,
                    unit="aa",
                    claim=f"UniProt {acc} 为分泌蛋白，无跨膜胞外域，对当前递送路线不可及",
                    source_ref=f"UniProt {acc}",
                    dataset="uniprotkb",
                    context={"extracellular_span_aa": 0, "location_text": "secreted"},
                ))

        if not observations:
            raise SourceUnavailable(self.source_id, f"UniProt 未提供 {request.dimension} 相关特征")
        return observations
