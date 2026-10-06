"""跨维度校正：单看一个维度合理、放在一起就不合理的情况。

这里只放**通用的领域规则**，不放任何具体靶点或癌种的特判。
每条规则都要能用一句生物学理由解释，并且换任何癌种都成立。

背景：老 HCC 模型是靠人工规避这类陷阱的。
它的 source_registry.md 里写着一条手工约定——
「ASGPR 的 TCGA bulk median 高不自动上调 HCC 主线分，因为 bulk 可能混入正常肝信号」，
并且人工把 ASGPR 的肿瘤表达分从 9 压到 4。
换癌种就没有人来做这个判断了，所以必须把它变成规则。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .schema import EvidenceItem

# 这些来源提供的是「整块组织」的 bulk 测序：肿瘤样本里混有基质、免疫和邻近正常细胞。
BULK_TISSUE_GROUPS = {"tcga", "cbioportal", "gdc"}

# 参照正常组织表达高到这个程度，bulk 信号就无法归因给肿瘤细胞
BULK_CONFOUND_THRESHOLD = 8.0

# 无法归因时给的中性分。不给 0（那等于断言它不表达），也不保留高分（那是被污染的信号）。
NEUTRAL_WHEN_UNRESOLVED = 5.0


@dataclass
class Adjustment:
    rule_id: str
    field: str
    before: float | None
    after: float | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "field": self.field,
            "before": self.before,
            "after": self.after,
            "reason": self.reason,
        }


def _is_bulk(item: EvidenceItem) -> bool:
    if str(item.context.get("assay", "")).lower() in ("bulk_rna", "bulk"):
        return True
    return item.independence_group in BULK_TISSUE_GROUPS


def bulk_confounding(
    scores: dict[str, float],
    items: Sequence[EvidenceItem],
) -> tuple[dict[str, float], list[Adjustment]]:
    """bulk RNA 与正常组织混淆校正。

    生物学理由：肿瘤组织的 bulk 测序信号 = 肿瘤细胞 + 基质 + 浸润的正常组织。
    当这个靶点在参照正常组织里本来就极高表达时，bulk 里测到的高值
    无法区分「肿瘤细胞高表达」和「样本里混了正常组织」这两种情况。
    此时正确的结论是「不知道」，而不是「高」。

    这条规则对任何癌种都成立：肺癌的正常肺、前列腺癌的正常前列腺，同理。
    """
    adjustments: list[Adjustment] = []
    normal_ref = scores.get("normal_reference_tissue_expression_score")
    tumor = scores.get("tumor_expression_score")

    if normal_ref is None or tumor is None or normal_ref < BULK_CONFOUND_THRESHOLD:
        return scores, adjustments

    tumor_items = [i for i in items if i.dimension == "tumor_expression" and i.score is not None]
    if not tumor_items or not any(_is_bulk(i) for i in tumor_items):
        return scores, adjustments

    # 优先用非 bulk 的证据（细胞系、IHC、单细胞）重算，它们能分辨细胞类型
    clean = [i for i in tumor_items if not _is_bulk(i)]
    if clean:
        new_value = round(sum(i.score for i in clean) / len(clean), 1)
        reason = (
            f"参照正常组织表达达 {normal_ref}，bulk 肿瘤 RNA 无法归因到肿瘤细胞；"
            f"改用 {len(clean)} 条细胞类型可分辨的证据（"
            + "、".join(sorted({i.source_id for i in clean})) + "）重算"
        )
    else:
        new_value = NEUTRAL_WHEN_UNRESOLVED
        reason = (
            f"参照正常组织表达达 {normal_ref}，唯一的肿瘤表达证据来自 bulk RNA，"
            f"无法区分肿瘤细胞与混入的正常组织；降为中性分并要求补充"
            f"单细胞 / IHC / 流式等细胞类型可分辨的证据"
        )

    if abs(new_value - tumor) < 0.05:
        return scores, adjustments

    out = dict(scores)
    out["tumor_expression_score"] = new_value
    adjustments.append(Adjustment(
        rule_id="bulk_confounding",
        field="tumor_expression_score",
        before=tumor,
        after=new_value,
        reason=reason,
    ))
    return out, adjustments


# 发育期 / 妊娠相关组织：高表达是真实生物学信号，但不该与心/脑/血液等
# 成人剂量限制性器官同等对待（GPC3 胎盘即典型）。见 docs/08 亮点 10。
DEVELOPMENTAL_TISSUE_MARKERS = (
    "placenta", "placental", "chorion", "fetal", "embryo", "embryonic",
    "umbilical", "amnion", "trophoblast",
)


def _tissue_blob(item: EvidenceItem) -> str:
    tissue = str(item.context.get("tissue") or "")
    return (tissue + " " + (item.claim or "")).lower().replace("_", " ")


def _is_developmental_item(item: EvidenceItem) -> bool:
    blob = _tissue_blob(item)
    return any(m in blob for m in DEVELOPMENTAL_TISSUE_MARKERS)


def developmental_tissue_softening(
    scores: dict[str, float],
    items: Sequence[EvidenceItem],
) -> tuple[dict[str, float], list[Adjustment]]:
    """发育期组织高表达不单独触发「成人安全窗否决」。

    生物学理由：oncofetal 抗原（如 GPC3）在胎盘等发育期组织高表达，
    是其作为肿瘤标志物的生物学基础，也是已知副作用来源——应当写入报告
    供人工复核，但不该与心/脑/骨髓高表达一样直接 early-kill。
    若成人组织脱靶同样危险，则不软化，照常否决。
    """
    adjustments: list[Adjustment] = []
    penalty = scores.get("normal_tissue_penalty")
    if penalty is None or penalty < 8.5:
        return scores, adjustments

    offtarget = [
        i for i in items
        if i.dimension == "offtarget_expression" and i.score is not None
    ]
    if not offtarget:
        return scores, adjustments

    devel = [i for i in offtarget if _is_developmental_item(i)]
    adult = [i for i in offtarget if not _is_developmental_item(i)]
    if not devel:
        return scores, adjustments

    adult_max = max((i.score for i in adult), default=0.0)
    if adult_max >= 8.5:
        return scores, adjustments

    # 契约分与 early-kill 只看成人组织脱靶；发育期信号进 notes 供人工复核，
    # 不抬高 penalty（否则会错误拖垮 oncofetal 抗原如 GPC3 的排序）。
    new_value = round(adult_max, 1)
    if abs(new_value - penalty) < 0.05:
        return scores, adjustments

    tissues = sorted({
        str(i.context.get("tissue") or "developmental") for i in devel
    })
    devel_max = max(i.score for i in devel)
    out = dict(scores)
    out["normal_tissue_penalty"] = new_value
    adjustments.append(Adjustment(
        rule_id="developmental_tissue_softening",
        field="normal_tissue_penalty",
        before=penalty,
        after=new_value,
        reason=(
            f"脱靶峰值 {devel_max} 来自发育期组织（{', '.join(tissues)}），"
            f"已从否决/排序用的 penalty 中剥离；成人组织最高风险分 {adult_max}。"
            f"发育期高表达证据仍保留，需人工复核"
        ),
    ))
    return out, adjustments


def shedding_penalty(
    scores: dict[str, float],
    items: Sequence[EvidenceItem],
) -> tuple[dict[str, float], list[Adjustment]]:
    """受体脱落校正。

    生物学理由：胞外域大量脱落进入血循环会形成可溶性「诱饵」，
    在药物到达肿瘤前就把它消耗掉。这直接削弱细胞外可及性的实际价值，
    单看膜定位分是看不出来的。
    """
    adjustments: list[Adjustment] = []
    shedding_items = [i for i in items if i.dimension == "shedding" and i.score is not None]
    if not shedding_items:
        return scores, adjustments

    shedding_risk = max(i.score for i in shedding_items)
    if shedding_risk < 7.0:
        return scores, adjustments

    field = "extracellular_accessibility_score"
    current = scores.get(field)
    if current is None:
        return scores, adjustments

    new_value = round(max(0.0, current - (shedding_risk - 5.0) * 0.6), 1)
    if abs(new_value - current) < 0.05:
        return scores, adjustments

    out = dict(scores)
    out[field] = new_value
    adjustments.append(Adjustment(
        rule_id="shedding_penalty",
        field=field,
        before=current,
        after=new_value,
        reason=(f"受体脱落风险 {shedding_risk}，可溶性胞外域会作为诱饵消耗药物，"
                f"下调实际可及性"),
    ))
    return out, adjustments


# 注册表：新增跨维度规则时加到这里。
# developmental_tissue_softening 必须在 early-kill 读分之前生效（通过 merged_scores）。
RULES = [bulk_confounding, developmental_tissue_softening, shedding_penalty]


def apply_all(
    scores: dict[str, float],
    items: Sequence[EvidenceItem],
) -> tuple[dict[str, float], list[Adjustment]]:
    """依次应用所有跨维度规则。返回校正后的分数与校正记录。

    校正记录必须写进报告——被改过的分如果不说明，就变成了黑箱。
    """
    current = dict(scores)
    all_adjustments: list[Adjustment] = []
    for rule in RULES:
        current, adjustments = rule(current, items)
        all_adjustments.extend(adjustments)
    return current, all_adjustments
