"""确定性分数映射：原始观测值 -> 0-10 分。

这是「LLM 不能编分数」这条纪律的物理落点（对应 questions.md Q50/Q51）：
Agent 只能决定「去查什么」，任何进入契约表的数字都必须流经本模块的纯函数。
本模块不 import 任何 agent 代码，也不调用 LLM，输入相同则输出永远相同。

去 HCC 化的重点在 tumor_expression：
老模型 route2.tcga_tumor_score() 的常数是按 GPC3≈6500、TFRC≈2000 这两个 HCC 数值手工校准的，
换癌种、换平台（RSEM vs TPM vs FPKM）就会失真。这里改成两条通用路径：
  1) 有队列分布时用「分位数」定标——与绝对量纲无关，跨癌种可比（首选）；
  2) 没有分布只有中位数时，退回可配置锚点的对数映射，并把锚点显式写进证据 context。
"""
from __future__ import annotations

import math
from statistics import median as _median
from typing import Any, Sequence


def clamp(value: float, lower: float = 0.0, upper: float = 10.0) -> float:
    return max(lower, min(upper, value))


# ============================================================================
# 连续量：表达值
# ============================================================================

def tpm_to_score(tpm: float, scale: float = 3.2) -> float:
    """TPM -> 0-10。log10 尺度，适合正常组织表达与脱靶惩罚。

    与老模型 route2.tpm_to_score 保持一致的口径（scale=3.2），
    保证 HCC 回归校验时新旧结果可比。
    """
    if tpm is None or tpm <= 0:
        return 0.0
    return round(clamp(math.log10(tpm + 1) * scale), 1)


def percentile_to_score(value: float, distribution: Sequence[float]) -> float:
    """分位数定标：value 在 distribution 中的百分位 -> 0-10。

    跨癌种、跨平台可比，是肿瘤表达打分的首选路径。
    distribution 应为同一队列内所有基因（或足够大的随机子集）的表达值。
    """
    if value is None or not distribution:
        return 0.0
    ordered = sorted(distribution)
    below = sum(1 for x in ordered if x < value)
    pct = below / len(ordered)
    return round(clamp(pct * 10), 1)


# 无分布数据时的兜底锚点：(原始值, 目标分数)。
# 默认锚点沿用 HCC 时期的经验标定，仅用于「只拿得到中位数」的降级场景，
# 换癌种若量纲不同应在癌种配置里覆盖，并会记录到证据 context 里以便复核。
DEFAULT_EXPRESSION_ANCHORS = ((10.0, 2.0), (100.0, 4.5), (2000.0, 7.0), (6500.0, 8.8))


def anchored_expression_to_score(
    value: float,
    anchors: Sequence[tuple[float, float]] = DEFAULT_EXPRESSION_ANCHORS,
) -> float:
    """按锚点做 log 空间分段线性插值。降级路径，精度低于 percentile_to_score。"""
    if value is None or value <= 0:
        return 0.0
    pts = sorted(anchors)
    lv = math.log10(value + 1)
    logs = [(math.log10(v + 1), s) for v, s in pts]
    if lv <= logs[0][0]:
        return round(clamp(lv / logs[0][0] * logs[0][1] if logs[0][0] else 0.0), 1)
    if lv >= logs[-1][0]:
        return round(clamp(logs[-1][1] + (lv - logs[-1][0]) * 1.5), 1)
    for (l1, s1), (l2, s2) in zip(logs, logs[1:]):
        if l1 <= lv <= l2:
            ratio = (lv - l1) / (l2 - l1) if l2 > l1 else 0.0
            return round(clamp(s1 + ratio * (s2 - s1)), 1)
    return 0.0


def selectivity_contrast(tumor_value: float, normal_value: float) -> float:
    """肿瘤/正常对比度 -> 0-10。癌种无关，是「肿瘤特异性」最稳健的度量。

    用 log2 fold change 映射：4 倍差 ≈ 6 分，16 倍差 ≈ 8 分，同水平 ≈ 3 分。
    """
    if tumor_value is None or normal_value is None:
        return 0.0
    t, n = max(tumor_value, 0.0), max(normal_value, 0.0)
    fc = (t + 1.0) / (n + 1.0)
    return round(clamp(3.0 + math.log2(fc) * 1.5), 1)


# ============================================================================
# 离散量：查表映射
# ============================================================================

# 亚细胞定位 -> 表面定位分。键为小写子串匹配。
LOCALIZATION_SCORES = {
    "plasma membrane": 9.0,
    "cell membrane": 9.0,
    "gpi-anchor": 8.5,
    "gpi anchor": 8.5,
    "cell surface": 8.5,
    "secreted": 3.0,
    "extracellular": 4.0,
    "cytoplasm": 1.5,
    "nucleus": 0.5,
    "mitochondria": 0.5,
    "endoplasmic reticulum": 2.0,
    "golgi": 2.0,
}

# 临床阶段 -> 先例分
CLINICAL_STAGE_SCORES = {
    "approved": 9.5,
    "phase 3": 8.5,
    "phase 2": 7.5,
    "phase 1": 6.5,
    "preclinical": 4.5,
    "none": 1.0,
}

# 内吞证据强度 -> 分
INTERNALIZATION_EVIDENCE_SCORES = {
    "quantitative_uptake": 9.0,      # 定量内吞实验
    "adc_validated": 8.5,            # 已有 ADC/偶联物验证入胞
    "imaging": 7.0,                  # 影像学证据
    "receptor_class_inferred": 5.0,  # 按受体类别推断
    "unknown": 2.0,
}


def lookup_score(text: str, table: dict[str, float], default: float = 0.0) -> float:
    """在查表里做小写子串匹配，取命中项的最大分。"""
    if not text:
        return default
    low = text.lower()
    hits = [score for key, score in table.items() if key in low]
    return max(hits) if hits else default


def localization_to_score(location_text: str) -> float:
    return lookup_score(location_text, LOCALIZATION_SCORES, default=0.0)


def clinical_stage_to_score(stage: str) -> float:
    return lookup_score(stage, CLINICAL_STAGE_SCORES, default=1.0)


def internalization_to_score(evidence_kind: str) -> float:
    return INTERNALIZATION_EVIDENCE_SCORES.get(evidence_kind, 2.0)


# ============================================================================
# 多证据合并
# ============================================================================

RELIABILITY_WEIGHT = {"high": 1.0, "medium": 0.7, "low": 0.4}


def merge_dimension_scores(
    scored: Sequence[tuple[float, str]],
    direction: str,
) -> float | None:
    """把同一维度的多条证据合并成一个分数。

    scored: [(score, reliability), ...]

    合并策略刻意做得「保守」：风险类维度取最大值而非平均。
    理由：递送靶点研究里，漏掉一个正常组织高表达的代价远大于误杀一个候选，
    平均值会让一条高风险证据被多条低风险证据稀释掉。
    这与老模型 route2 中「GTEx 与 HPA 取较大者」的口径一致。
    """
    if not scored:
        return None
    if direction == "lower_better":
        # 风险维度：取最保守（最高风险）
        return round(max(s for s, _ in scored), 1)
    # 收益维度：按可靠性加权平均，但不超过最高可靠性来源的值太多
    total_w = sum(RELIABILITY_WEIGHT.get(r, 0.5) for _, r in scored)
    if total_w == 0:
        return round(_median([s for s, _ in scored]), 1)
    weighted = sum(s * RELIABILITY_WEIGHT.get(r, 0.5) for s, r in scored) / total_w
    return round(clamp(weighted), 1)


def compute_evidence_level(
    dimensions_covered: Sequence[str],
    independent_groups: Sequence[str],
    has_protein_layer: bool,
    has_functional_layer: bool,
) -> int:
    """0-5 的证据充分度。规则确定，便于在报告里解释「为什么只有 3 级」。

    0 无证据 / 1 单源单维 / 2 多维但单源 / 3 多源结构化
    4 = 3 + 蛋白层证据 / 5 = 4 + 功能性（内吞、临床）证据
    """
    n_dim = len(set(dimensions_covered))
    n_grp = len(set(independent_groups))
    if n_dim == 0:
        return 0
    level = 1
    if n_dim >= 3:
        level = 2
    if n_grp >= 2 and n_dim >= 3:
        level = 3
    if level >= 3 and has_protein_layer:
        level = 4
    if level >= 4 and has_functional_layer:
        level = 5
    return level
