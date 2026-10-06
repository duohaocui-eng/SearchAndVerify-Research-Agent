"""Observation -> 分数 的中央调度。

为什么单独一个模块：这是「所有数字的唯一出口」。
数据源只报原始观测，Agent 只决定查什么，任何 0-10 的分都必须从这里出来。
想知道某个分是怎么来的，只需要看这一个文件——这对可解释性和评测复现都很关键。

每个维度一个 handler，新增维度时在 _HANDLERS 里加一条即可。
"""
from __future__ import annotations

import math
from typing import Any, Callable

from .schema import DIMENSIONS, Observation
from .scoring_maps import (
    anchored_expression_to_score,
    clamp,
    clinical_stage_to_score,
    internalization_to_score,
    localization_to_score,
    percentile_to_score,
    tpm_to_score,
)

RELIABILITY_BASE = {"high": 8.0, "medium": 6.5, "low": 5.0}


def _score_tumor_expression(obs: Observation) -> float | None:
    if obs.value is None:
        return None
    dist = obs.context.get("distribution")
    if dist and len(dist) >= 20:
        # 首选：分位数定标，与量纲无关，跨癌种可比
        return percentile_to_score(obs.value, dist)
    anchors = obs.context.get("anchors")
    if anchors:
        return anchored_expression_to_score(obs.value, [tuple(a) for a in anchors])
    return anchored_expression_to_score(obs.value)


def _score_expression_risk(obs: Observation) -> float | None:
    """正常组织 / 脱靶 / 关键器官表达：值越高风险越大。"""
    if obs.value is None:
        return None
    return tpm_to_score(obs.value)


def _score_surface_localization(obs: Observation) -> float | None:
    text = str(obs.context.get("location_text") or obs.claim)
    score = localization_to_score(text)
    return score if score > 0 else None


def _score_extracellular(obs: Observation) -> float | None:
    aa = obs.context.get("extracellular_span_aa", obs.value)
    if aa is None:
        return None
    try:
        span = float(aa)
    except (TypeError, ValueError):
        return None
    if span <= 0:
        return 0.0
    return round(clamp(math.log10(span + 1) * 3.3), 1)


def _score_internalization(obs: Observation) -> float | None:
    kind = str(obs.context.get("evidence_kind") or "unknown")
    return internalization_to_score(kind)


def _score_clinical(obs: Observation) -> float | None:
    stage = str(obs.context.get("stage") or obs.claim)
    return clinical_stage_to_score(stage)


_DELIVERY_HINTS = (
    "adc", "antibody-drug", "antibody drug conjugate", "galnac",
    "car-t", "car t", "ligand-conjugat", "sirna conjugate", "payload",
    "receptor-mediated delivery", "conjugate",
)
_NON_DELIVERY_DRUG = (
    "tki", "tyrosine kinase", "kinase inhibitor", "small molecule inhibitor",
)


def _score_delivery_precedent(obs: Observation) -> float | None:
    """递送先例：ADC / GalNAc / CAR / ligand-conjugate，不是普通获批抑制剂。"""
    stage = str(obs.context.get("stage") or "")
    text = " ".join([
        obs.claim or "",
        stage,
        str(obs.context.get("quote") or ""),
        str(obs.context.get("title") or ""),
    ]).lower()
    if any(k in text for k in _NON_DELIVERY_DRUG) and not any(k in text for k in _DELIVERY_HINTS):
        return 1.0
    return clinical_stage_to_score(stage or obs.claim or "")


def _score_ligand(obs: Observation) -> float | None:
    """配体可得性：没有统一量纲，按证据可靠性给基准分。

    这是本模块里最「软」的一个维度。做法是明确降级：
    只给可靠性对应的基准分，不试图从自然语言里读出精度更高的数字，
    避免给一个看起来精确、实则没有依据的分。
    """
    if not obs.claim:
        return None
    return RELIABILITY_BASE.get(obs.reliability, 6.0)


def _score_shedding(obs: Observation) -> float | None:
    """受体脱落是风险项：有证据说明脱落 = 高风险。"""
    if not obs.claim:
        return None
    text = obs.claim.lower() + str(obs.context.get("quote", "")).lower()
    if any(k in text for k in ("no shedding", "not shed", "minimal shedding")):
        return 2.0
    if any(k in text for k in ("shed", "soluble", "decoy", "ectodomain")):
        return 8.5 if obs.reliability == "high" else 7.0
    return None


def _score_heterogeneity(obs: Observation) -> float | None:
    if obs.value is None:
        return None
    return round(clamp(float(obs.value) * 5.0), 1)


def _score_endosomal_escape(obs: Observation) -> float | None:
    """内体逃逸需求。默认中性 5 分，只有文献明确说明时才偏移。

    真正决定内体逃逸难度的是载荷形态（siRNA/ASO/小分子）而不是靶点本身，
    这部分应由下游分子设计判断，所以这里刻意保守。
    """
    text = (obs.claim or "").lower() + str(obs.context.get("quote", "")).lower()
    if any(k in text for k in ("efficient escape", "cytosolic delivery achieved", "endosomolytic")):
        return 3.0
    if any(k in text for k in ("entrapped", "lysosomal degradation", "poor escape")):
        return 8.0
    return 5.0 if text else None


def _score_biodistribution(obs: Observation) -> float | None:
    text = (obs.claim or "").lower()
    if not text:
        return None
    if any(k in text for k in ("tumor accumulation", "favorable biodistribution", "high tumor uptake")):
        return 3.0
    if any(k in text for k in ("liver clearance", "rapid clearance", "poor penetration", "off-target accumulation")):
        return 7.5
    return 5.0


_HANDLERS: dict[str, Callable[[Observation], float | None]] = {
    "tumor_expression": _score_tumor_expression,
    "normal_reference_expression": _score_expression_risk,
    "offtarget_expression": _score_expression_risk,
    "safety": _score_expression_risk,
    "surface_localization": _score_surface_localization,
    "extracellular_accessibility": _score_extracellular,
    "internalization": _score_internalization,
    "ligand_availability": _score_ligand,
    "clinical_precedent": _score_clinical,
    "delivery_precedent": _score_delivery_precedent,
    "shedding": _score_shedding,
    "heterogeneity": _score_heterogeneity,
    "endosomal_escape": _score_endosomal_escape,
    "biodistribution": _score_biodistribution,
}


def score_observation(obs: Observation) -> float | None:
    """给一条观测打分。返回 None 表示「这条证据不足以给分」——

    返回 None 是合法且重要的：它让缺证据保持为缺证据，
    而不是被一个编出来的默认值填上。
    """
    if obs.dimension not in DIMENSIONS:
        raise ValueError(f"未知维度 {obs.dimension}")
    handler = _HANDLERS.get(obs.dimension)
    if handler is None:
        return None
    score = handler(obs)
    return None if score is None else round(clamp(float(score)), 1)


def explain_score(obs: Observation, score: float | None) -> str:
    """一句话说明这个分是怎么来的，写进报告便于人工复核。"""
    if score is None:
        return f"{obs.dimension}: 证据不足以量化"
    unit = f" {obs.unit}" if obs.unit else ""
    raw = f"{obs.value}{unit}" if obs.value is not None else "定性证据"
    return f"{obs.dimension}: {raw} -> {score}（{obs.source_id}）"
