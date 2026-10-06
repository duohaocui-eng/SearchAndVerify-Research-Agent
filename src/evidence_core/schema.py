"""癌种中立的证据契约：维度定义、字段契约、校验规则。

这是整个系统的「词汇表」。三层都围绕它对话：
- 数据源产出 Observation，声明自己填的是哪个 dimension；
- Agent 只按 dimension 思考「还缺什么」，不认识具体数据库；
- contract_writer 把 dimension 折算成下游闸门模型认识的 CSV 列。

去 HCC 化的关键改动（对照 hcc_target-evidence-model/src/hcc_target_evidence/schema.py）：
    hcc_expression_score            -> tumor_expression_score
    hcc_expression_evidence         -> tumor_expression_evidence
    normal_liver_expression_score   -> normal_reference_tissue_expression_score
「参照正常组织」是什么由癌种配置决定，代码里不再出现 liver。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ============================================================================
# 证据维度：Agent 思考的最小单位
# ============================================================================

DIMENSIONS: dict[str, dict[str, Any]] = {
    "tumor_expression": {
        "label": "肿瘤表达",
        "contract_field": "tumor_expression_score",
        "direction": "higher_better",
        "required": True,
    },
    "normal_reference_expression": {
        "label": "参照正常组织表达",
        "contract_field": "normal_reference_tissue_expression_score",
        "direction": "lower_better",
        "required": True,
    },
    "offtarget_expression": {
        "label": "脱靶组织表达",
        "contract_field": "normal_tissue_penalty",
        "direction": "lower_better",
        "required": False,
    },
    "surface_localization": {
        "label": "细胞表面定位",
        "contract_field": "surface_localization_score",
        "direction": "higher_better",
        "required": True,
    },
    "extracellular_accessibility": {
        "label": "细胞外可及性",
        "contract_field": "extracellular_accessibility_score",
        "direction": "higher_better",
        "required": False,
    },
    "internalization": {
        "label": "内吞",
        "contract_field": "internalization_score",
        "direction": "higher_better",
        "required": True,
    },
    "ligand_availability": {
        "label": "配体可得性",
        "contract_field": "ligand_availability_score",
        "direction": "higher_better",
        "required": False,
    },
    "clinical_precedent": {
        "label": "临床靶点先例",
        "contract_field": "clinical_precedent_score",
        "direction": "higher_better",
        "required": False,
    },
    "delivery_precedent": {
        "label": "递送先例",
        "contract_field": "delivery_precedent_score",
        "direction": "higher_better",
        "required": False,
    },
    "endosomal_escape": {
        "label": "内体逃逸需求",
        "contract_field": "endosomal_escape_need",
        "direction": "lower_better",
        "required": False,
    },
    "biodistribution": {
        "label": "生物分布风险",
        "contract_field": "biodistribution_risk",
        "direction": "lower_better",
        "required": False,
    },
    "safety": {
        "label": "安全风险",
        "contract_field": "safety_risk",
        "direction": "lower_better",
        "required": True,
    },
    "shedding": {
        "label": "受体脱落",
        "contract_field": "",          # 不直接映射列，作为可及性/安全的修正项
        "direction": "lower_better",
        "required": False,
    },
    "heterogeneity": {
        "label": "表达异质性",
        "contract_field": "",          # 同上，写入 notes 供人判断
        "direction": "lower_better",
        "required": False,
    },
}

REQUIRED_DIMENSIONS = [k for k, v in DIMENSIONS.items() if v["required"]]

# 维度 -> 契约列（仅取有映射的）
DIMENSION_TO_FIELD = {k: v["contract_field"] for k, v in DIMENSIONS.items() if v["contract_field"]}


# ============================================================================
# 契约字段：与 delivery-gate-model 的接口
# ============================================================================

IDENTITY_FIELDS = ["target_symbol", "alias", "route_type", "protein_name"]

SCORE_FIELDS = [
    "tumor_expression_score",
    "normal_reference_tissue_expression_score",
    "normal_tissue_penalty",
    "surface_localization_score",
    "extracellular_accessibility_score",
    "internalization_score",
    "ligand_availability_score",
    "clinical_precedent_score",
    "delivery_precedent_score",
    "endosomal_escape_need",
    "biodistribution_risk",
    "safety_risk",
]

META_FIELDS = ["evidence_level", "tumor_expression_evidence", "notes", "source_refs"]

CONTRACT_FIELDS = IDENTITY_FIELDS + SCORE_FIELDS + META_FIELDS

# 通用列名 -> 既有闸门模型的旧列名。
# delivery-gate-model 会优先读通用名、缺失才回落旧名，因此严格来说不必输出旧列；
# 但同时输出可让老的 HCC 脚本、既有报告与人工核对流程零改动继续工作。
LEGACY_ALIASES = {
    "tumor_expression_score": "hcc_expression_score",
    "normal_reference_tissue_expression_score": "normal_liver_expression_score",
    "tumor_expression_evidence": "hcc_expression_evidence",
}

ROUTE_TYPES = ("main", "benchmark", "backup", "exploratory")


# ============================================================================
# 证据条目：evidence.jsonl 的一行
# ============================================================================

@dataclass
class Observation:
    """数据源返回的一条原始观测。数据源只负责产出它，不负责打分。"""

    target: str
    dimension: str
    value: float | None                # 原始数值，如 TPM
    unit: str                          # TPM / nTPM / log2(TPM+1) / bool / ordinal
    claim: str                         # 自然语言结论
    source_id: str
    source_dataset: str = ""           # 具体数据集/队列 id
    source_ref: str = ""               # URL / PMID / accession，Verifier 校验用
    layer: str = ""                    # rna / protein / functional / clinical
    reliability: str = "medium"
    independence_group: str = ""
    context: dict[str, Any] = field(default_factory=dict)   # 组织名、样本数等


@dataclass
class EvidenceItem:
    """归档后的证据。score 由确定性映射算出，LLM 不能写这个字段。"""

    evidence_id: str
    target: str
    dimension: str
    value: float | None
    unit: str
    score: float | None                # 0-10，由 scoring_maps 计算
    claim: str
    source_id: str
    source_dataset: str
    source_ref: str
    layer: str
    reliability: str
    independence_group: str
    retrieved_at: str
    verification: str = "unverified"   # unverified / verified / rejected
    verifier_note: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "target": self.target,
            "dimension": self.dimension,
            "value": self.value,
            "unit": self.unit,
            "score": self.score,
            "claim": self.claim,
            "source_id": self.source_id,
            "source_dataset": self.source_dataset,
            "source_ref": self.source_ref,
            "layer": self.layer,
            "reliability": self.reliability,
            "independence_group": self.independence_group,
            "retrieved_at": self.retrieved_at,
            "verification": self.verification,
            "verifier_note": self.verifier_note,
            "context": self.context,
        }


# ============================================================================
# 校验
# ============================================================================

def validate_dimension(name: str) -> None:
    if name not in DIMENSIONS:
        raise ValueError(f"未知证据维度 '{name}'。已定义：{sorted(DIMENSIONS)}")


def validate_contract_row(row: dict[str, str]) -> list[str]:
    """校验一行契约输出。返回错误列表，空表示通过。

    与老模型一致：分数 0-10、evidence_level 0-5。差别是允许分数为空——
    Agent 可能诚实地「没查到」，此时写空而不是编一个默认值（对应 Q60 诚实输出不确定）。
    """
    errors: list[str] = []
    for f in IDENTITY_FIELDS:
        if not row.get(f, "").strip():
            errors.append(f"缺少必填字段: {f}")
    if row.get("route_type") and row["route_type"] not in ROUTE_TYPES:
        errors.append(f"route_type 必须是 {ROUTE_TYPES} 之一，实际 '{row['route_type']}'")
    for f in SCORE_FIELDS:
        raw = row.get(f, "")
        if raw == "":
            continue
        try:
            v = float(raw)
        except ValueError:
            errors.append(f"{f} 必须是数值")
            continue
        if not 0 <= v <= 10:
            errors.append(f"{f} 必须在 0-10 之间，实际 {v}")
    raw_level = row.get("evidence_level", "")
    if raw_level != "":
        try:
            level = int(raw_level)
            if not 0 <= level <= 5:
                errors.append("evidence_level 必须在 0-5 之间")
        except ValueError:
            errors.append("evidence_level 必须是整数")
    return errors
