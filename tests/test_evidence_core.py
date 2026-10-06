"""evidence_core 的确定性保证测试。

这一层的价值全在「相同输入 -> 相同输出」，所以测试重点是不变量而非覆盖率。
"""
from __future__ import annotations

import pytest

from evidence_core.adjustments import apply_all
from evidence_core.config import load_yaml
from evidence_core.evidence_store import EvidenceStore
from evidence_core.schema import EvidenceItem, Observation, validate_contract_row
from evidence_core.scorer import score_observation
from evidence_core.scoring_maps import (
    compute_evidence_level,
    merge_dimension_scores,
    selectivity_contrast,
    tpm_to_score,
)


def make_obs(**kwargs) -> Observation:
    defaults = dict(
        target="GENE", dimension="tumor_expression", value=100.0, unit="TPM",
        claim="claim", source_id="src", source_dataset="ds", source_ref="ref",
        layer="rna", reliability="high", independence_group="grp", context={},
    )
    defaults.update(kwargs)
    return Observation(**defaults)


def make_item(**kwargs) -> EvidenceItem:
    defaults = dict(
        evidence_id="ev_0001", target="GENE", dimension="tumor_expression",
        value=100.0, unit="TPM", score=7.0, claim="c", source_id="src",
        source_dataset="ds", source_ref="ref", layer="rna", reliability="high",
        independence_group="grp", retrieved_at="2026-01-01T00:00:00+00:00",
        verification="verified", verifier_note="", context={},
    )
    defaults.update(kwargs)
    return EvidenceItem(**defaults)


# ---------------------------------------------------------------- 打分确定性

def test_scoring_is_deterministic():
    obs = make_obs()
    assert score_observation(obs) == score_observation(obs)


def test_tpm_matches_legacy_calibration():
    """与老模型 route2.tpm_to_score 口径一致，保证 HCC 回归可比。"""
    assert tpm_to_score(0.94) == 0.9
    assert tpm_to_score(408.77) == 8.4
    assert tpm_to_score(12.37) == 3.6
    assert tpm_to_score(0) == 0.0


def test_score_returns_none_when_evidence_insufficient():
    """缺证据必须返回 None，不能补默认值——否则缺证据会被伪装成中等质量证据。"""
    assert score_observation(make_obs(value=None, context={})) is None
    assert score_observation(make_obs(dimension="surface_localization",
                                      value=None, claim="", context={})) is None


def test_unknown_dimension_raises():
    with pytest.raises(ValueError):
        score_observation(make_obs(dimension="not_a_dimension"))


# ---------------------------------------------------------------- 合并策略

def test_risk_dimension_takes_max_not_average():
    """风险维度必须取最保守值：一条高风险证据不能被多条低风险证据稀释。"""
    scored = [(9.0, "high"), (1.0, "high"), (1.0, "high")]
    assert merge_dimension_scores(scored, "lower_better") == 9.0


def test_benefit_dimension_weighted_by_reliability():
    high_only = merge_dimension_scores([(9.0, "high")], "higher_better")
    with_low = merge_dimension_scores([(9.0, "high"), (3.0, "low")], "higher_better")
    assert with_low is not None and high_only is not None
    assert with_low < high_only          # 低可靠性证据会拉低，但权重更小
    assert with_low > 6.0


def test_selectivity_contrast_is_scale_free():
    """对比度只看倍数关系，与绝对量纲无关——这是跨癌种可比的基础。"""
    assert selectivity_contrast(1000, 10) == selectivity_contrast(100, 1)


# ---------------------------------------------------------------- 证据库

def test_duplicate_evidence_is_idempotent():
    store = EvidenceStore()
    obs = make_obs()
    assert store.add(obs, 7.0) is not None
    assert store.add(obs, 7.0) is None
    assert len(store.all()) == 1


def test_same_dataset_different_portals_count_as_one_source():
    """cBioPortal 与 GDC 是同一份 TCGA 数据的两个门户，不构成独立印证。"""
    store = EvidenceStore()
    store.add(make_obs(source_id="cbioportal", independence_group="tcga",
                       claim="a"), 8.0)
    store.add(make_obs(source_id="tcga_gdc", independence_group="tcga",
                       claim="b"), 8.0)
    assert len(store.independent_groups("GENE", "tumor_expression")) == 1
    assert not store.has_independent_support("GENE", "tumor_expression", minimum=2)


def test_value_spread_conflict_detected():
    store = EvidenceStore(conflict_tolerance=2.0)
    store.add(make_obs(source_id="a", independence_group="a", claim="x"), 9.0)
    store.add(make_obs(source_id="b", independence_group="b", claim="y"), 2.0)
    kinds = {c.kind for c in store.conflicts}
    assert "value_spread" in kinds


def test_rna_protein_disagreement_detected_separately():
    """同一对证据可同时构成数值分歧与层级分歧，两者处理方式不同，不能合并。"""
    store = EvidenceStore(conflict_tolerance=2.0)
    store.add(make_obs(source_id="tcga", layer="rna",
                       independence_group="tcga", claim="rna high"), 8.0)
    store.add(make_obs(source_id="hpa", layer="protein",
                       independence_group="hpa", claim="protein low"), 2.0)
    kinds = {c.kind for c in store.conflicts}
    assert kinds == {"value_spread", "layer_disagreement"}


# ---------------------------------------------------------------- 跨维度校正

def test_bulk_confounding_discounts_unattributable_tumor_signal():
    """参照正常组织极高时，bulk 肿瘤信号无法归因给肿瘤细胞，必须降级。"""
    scores = {
        "tumor_expression_score": 9.2,
        "normal_reference_tissue_expression_score": 9.8,
    }
    items = [make_item(independence_group="tcga", score=9.2)]
    adjusted, log = apply_all(scores, items)
    assert adjusted["tumor_expression_score"] < 9.2
    assert any(a.rule_id == "bulk_confounding" for a in log)


def test_bulk_confounding_not_triggered_when_normal_is_low():
    """正常组织表达低时 bulk 信号可归因，不应触发校正。"""
    scores = {
        "tumor_expression_score": 8.8,
        "normal_reference_tissue_expression_score": 2.8,
    }
    items = [make_item(independence_group="tcga", score=8.8)]
    adjusted, log = apply_all(scores, items)
    assert adjusted["tumor_expression_score"] == 8.8
    assert not log


def test_bulk_confounding_prefers_cell_resolved_evidence():
    """有细胞类型可分辨的证据时，用它重算而不是给中性分。"""
    scores = {
        "tumor_expression_score": 9.0,
        "normal_reference_tissue_expression_score": 9.5,
    }
    items = [
        make_item(evidence_id="ev_1", independence_group="tcga", score=9.0),
        make_item(evidence_id="ev_2", independence_group="depmap",
                  source_id="depmap", score=3.0),
    ]
    adjusted, log = apply_all(scores, items)
    assert adjusted["tumor_expression_score"] == 3.0
    assert "重算" in log[0].reason


# ---------------------------------------------------------------- 契约校验

def test_contract_allows_empty_scores():
    """查不到就是查不到，空值合法——不允许用默认值伪装成已知。"""
    row = {
        "target_symbol": "GENE", "alias": "G", "route_type": "main",
        "protein_name": "P", "tumor_expression_score": "", "evidence_level": "2",
    }
    assert validate_contract_row(row) == []


def test_contract_rejects_out_of_range_score():
    row = {
        "target_symbol": "GENE", "alias": "G", "route_type": "main",
        "protein_name": "P", "tumor_expression_score": "12", "evidence_level": "2",
    }
    assert any("0-10" in e for e in validate_contract_row(row))


def test_contract_rejects_unknown_route_type():
    row = {
        "target_symbol": "GENE", "alias": "G", "route_type": "wat",
        "protein_name": "P", "evidence_level": "2",
    }
    assert any("route_type" in e for e in validate_contract_row(row))


# ---------------------------------------------------------------- 证据级别

def test_evidence_level_requires_independent_sources():
    single = compute_evidence_level(["a", "b", "c"], ["grp"], False, False)
    multi = compute_evidence_level(["a", "b", "c"], ["g1", "g2"], False, False)
    assert multi > single


def test_evidence_level_caps_at_five():
    assert compute_evidence_level(["a", "b", "c"], ["g1", "g2"], True, True) == 5


# ---------------------------------------------------------------- YAML 解析

def test_yaml_parser_handles_multiline_inline_list(tmp_path):
    """配置里跨行的行内列表必须能解析，否则数据源能力声明会被静默截断。"""
    p = tmp_path / "t.yaml"
    p.write_text(
        "key:\n"
        "  provides: [a, b,\n"
        "             c, d]\n"
        "  nested:\n"
        "    x: 1\n",
        encoding="utf-8",
    )
    data = load_yaml(p)
    assert data["key"]["provides"] == ["a", "b", "c", "d"]
    assert data["key"]["nested"]["x"] == 1


def test_yaml_parser_handles_block_list_of_maps(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text(
        "rules:\n"
        "  - id: r1\n"
        "    when: \"a < 4\"\n"
        "  - id: r2\n"
        "    when: \"b >= 8\"\n",
        encoding="utf-8",
    )
    data = load_yaml(p)
    assert [r["id"] for r in data["rules"]] == ["r1", "r2"]
