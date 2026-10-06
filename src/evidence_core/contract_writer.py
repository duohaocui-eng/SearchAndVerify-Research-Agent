"""把证据库折算成下游闸门模型能吃的契约 CSV。

这是 Agent 与 delivery-gate-model 之间唯一的接口面。保持它「窄」很重要：
Agent 怎么改、数据源怎么增删，只要这张表的列不变，闸门模型就完全不用动。

一个刻意的设计：查不到的维度写空字符串，不写默认值。
老模型里 seed 表是人工填满的，每个格子都有数；Agent 跑出来则可能真的缺证据。
如果这里补一个「中性 5 分」，缺证据就会被伪装成中等质量证据，
下游排序会把没查过的候选排到查过的前面。写空 + 在 notes 里说明，才是诚实的做法。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .adjustments import Adjustment, apply_all
from .evidence_store import EvidenceStore, missing_dimensions
from .io import write_csv
from .schema import (
    CONTRACT_FIELDS,
    DIMENSION_TO_FIELD,
    DIMENSIONS,
    LEGACY_ALIASES,
    REQUIRED_DIMENSIONS,
    validate_contract_row,
)
from .scoring_maps import compute_evidence_level, merge_dimension_scores

# 需要「反向」的维度：证据分越高代表风险越大，契约列语义已是风险分，直接写入。
# 需要注意的是 offtarget_expression -> normal_tissue_penalty 也是风险分，同理。


def merged_scores(
    store: EvidenceStore,
    target: str,
    *,
    verified_only: bool = True,
    with_adjustments: bool = True,
) -> dict[str, float]:
    """把一个候选各维度的证据合并成 契约列名 -> 分数。

    early-kill 规则、路线分类、契约输出三处都要用同一份合并结果，
    抽出来共用可以保证「报告里看到的分」和「触发否决的分」永远是同一个数。
    """
    return merged_scores_with_log(
        store, target, verified_only=verified_only, with_adjustments=with_adjustments
    )[0]


def merged_scores_with_log(
    store: EvidenceStore,
    target: str,
    *,
    verified_only: bool = True,
    with_adjustments: bool = True,
) -> tuple[dict[str, float], list[Adjustment]]:
    """同 merged_scores，但一并返回跨维度校正记录，供报告说明分数为何被改动。"""
    items = store.for_target(target, verified_only=verified_only)
    out: dict[str, float] = {}
    for dimension, field_name in DIMENSION_TO_FIELD.items():
        dim_items = [i for i in items if i.dimension == dimension and i.score is not None]
        # live 与 fixture 同时存在时，fixture 只是降级兜底，不能把分泌蛋白
        # 的膜分拉上去，也不能稀释 live 表达。
        live = [i for i in dim_items if not str(i.source_id).startswith("fixtures")]
        chosen = live if live else dim_items
        scored = [(i.score, i.reliability) for i in chosen]
        if not scored:
            continue
        merged = merge_dimension_scores(scored, DIMENSIONS[dimension]["direction"])
        if merged is not None:
            out[field_name] = merged

    if not with_adjustments:
        return out, []
    return apply_all(out, items)


def build_row(
    store: EvidenceStore,
    target: str,
    *,
    route_type: str = "exploratory",
    aliases: Iterable[str] = (),
    protein_name: str = "",
    extra_notes: Iterable[str] = (),
    verified_only: bool = True,
) -> dict[str, str]:
    """把一个候选的所有证据折算成一行契约。"""
    items = store.for_target(target, verified_only=verified_only)

    row: dict[str, str] = {f: "" for f in CONTRACT_FIELDS}
    row["target_symbol"] = target
    row["alias"] = "/".join(a for a in aliases if a) or target
    row["route_type"] = route_type
    row["protein_name"] = protein_name or target

    scores, adjustments = merged_scores_with_log(store, target, verified_only=verified_only)
    for field_name, value in scores.items():
        row[field_name] = f"{value}"

    # tumor_expression 的自然语言证据串
    tumor_items = [i for i in items if i.dimension == "tumor_expression"]
    if tumor_items:
        row["tumor_expression_evidence"] = "；".join(
            f"{i.claim}（{i.source_id}）" for i in tumor_items[:3]
        )

    # 证据充分度
    dims = [i.dimension for i in items]
    groups = [i.independence_group for i in items]
    row["evidence_level"] = str(compute_evidence_level(
        dimensions_covered=dims,
        independent_groups=groups,
        has_protein_layer=any(i.layer == "protein" for i in items),
        has_functional_layer=any(i.layer in ("functional", "clinical") for i in items),
    ))

    # notes：把「缺什么、冲突、降级、被改过的分」写清楚，这是报告可信度的来源
    notes: list[str] = list(extra_notes)
    for adj in adjustments:
        notes.append(
            f"跨维度校正[{adj.rule_id}]：{adj.field} {adj.before}→{adj.after}，{adj.reason}"
        )
    gaps = missing_dimensions(store, target, REQUIRED_DIMENSIONS, verified_only=verified_only)
    if gaps:
        labels = [DIMENSIONS[g]["label"] for g in gaps]
        notes.append(f"未覆盖必需维度：{'、'.join(labels)}")
    for conflict in store.open_conflicts(target):
        notes.append(f"未解决冲突[{conflict.dimension}]：{conflict.detail}")
    fixture_only = items and all(i.source_id == "fixtures" for i in items)
    if fixture_only:
        notes.append("全部证据来自离线 fixture，非真实数据源，结论仅供流程演示")
    heterogeneity = [i for i in items if i.dimension == "heterogeneity"]
    if heterogeneity:
        notes.append("表达异质性：" + "；".join(i.claim for i in heterogeneity[:2]))
    shedding = [i for i in items if i.dimension == "shedding"]
    if shedding:
        notes.append("受体脱落：" + "；".join(i.claim for i in shedding[:2]))
    row["notes"] = "；".join(notes)

    # source_refs：去重保序，保证每个结论可回溯
    refs: list[str] = []
    for i in items:
        ref = i.source_ref or f"{i.source_id}:{i.source_dataset}"
        if ref and ref not in refs:
            refs.append(ref)
    row["source_refs"] = "; ".join(refs)

    return row


def add_legacy_columns(row: dict[str, str]) -> dict[str, str]:
    """补一份 HCC 时期的旧列名，让既有脚本与报告零改动继续可用。"""
    out = dict(row)
    for neutral, legacy in LEGACY_ALIASES.items():
        out[legacy] = row.get(neutral, "")
    return out


def write_contract(
    rows: list[dict[str, str]],
    path: str | Path,
    *,
    emit_legacy: bool = True,
    strict: bool = False,
) -> dict[str, Any]:
    """写出契约 CSV。返回校验摘要。

    strict=True 时校验失败直接抛错（评测与 CI 用）；
    默认 False，把问题收集进 warnings，让一次运行不会因为单个候选写坏而全废。
    """
    all_errors: list[str] = []
    for idx, row in enumerate(rows, start=1):
        errors = validate_contract_row(row)
        all_errors.extend(f"row {idx} ({row.get('target_symbol', '?')}): {e}" for e in errors)

    if all_errors and strict:
        raise ValueError("契约校验失败：\n" + "\n".join(all_errors))

    fields = list(CONTRACT_FIELDS)
    out_rows = rows
    if emit_legacy:
        out_rows = [add_legacy_columns(r) for r in rows]
        for legacy in LEGACY_ALIASES.values():
            if legacy not in fields:
                fields.append(legacy)

    write_csv(path, out_rows, fields)
    return {"rows": len(rows), "path": str(path), "warnings": all_errors}
