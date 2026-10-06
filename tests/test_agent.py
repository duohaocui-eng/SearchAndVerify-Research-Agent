"""agent 决策层测试。

重点不是「跑得通」，而是那些一旦悄悄坏掉就会让结论失真的行为：
证据不足时不许淘汰、验证在淘汰之前、消融开关真的关掉了对应部件。
"""
from __future__ import annotations

from agent.baseline import compare, run_baseline
from agent.early_kill import KillRule, classify_route, load_rules
from agent.loop import SearchAndVerifyAgent
from agent.verifier import RuleVerifier
from evidence_core.config import load_agent_config
from evidence_core.schema import EvidenceItem


def make_item(**kwargs) -> EvidenceItem:
    defaults = dict(
        evidence_id="ev_0001", target="GENE", dimension="internalization",
        value=None, unit="claim", score=7.0, claim="GENE mediates endocytosis",
        source_id="fixtures:literature", source_dataset="pubmed",
        source_ref="PMID 1", layer="functional", reliability="medium",
        independence_group="literature",
        retrieved_at="2026-01-01T00:00:00+00:00",
        verification="unverified", verifier_note="", context={},
    )
    defaults.update(kwargs)
    return EvidenceItem(**defaults)


# ---------------------------------------------------------------- early-kill

def test_kill_rule_returns_none_when_evidence_missing():
    """证据不足必须是「判不了」，不能被当成「通过检查」。"""
    rule = KillRule("no_membrane", "surface_localization_score < 4", "无膜定位")
    assert rule.evaluate({}) is None
    assert rule.evaluate({"surface_localization_score": 9.0}) is False
    assert rule.evaluate({"surface_localization_score": 2.0}) is True


def test_kill_rule_handles_conjunction():
    rule = KillRule(
        "ref_dominant",
        "normal_reference_tissue_expression_score >= 8.5 and tumor_expression_score < 6",
        "参照组织主导",
    )
    assert rule.evaluate({
        "normal_reference_tissue_expression_score": 9.0,
        "tumor_expression_score": 5.0,
    }) is True
    assert rule.evaluate({
        "normal_reference_tissue_expression_score": 9.0,
        "tumor_expression_score": 7.0,
    }) is False
    # 缺一个字段就判不了，不能只凭另一半下结论
    assert rule.evaluate({"normal_reference_tissue_expression_score": 9.0}) is None


def test_kill_rule_rejects_arbitrary_expressions():
    """条件表达式只允许「字段 运算符 数字」，杜绝配置里混入可执行代码。"""
    rule = KillRule("bad", "__import__('os').system('x')", "恶意")
    assert rule.evaluate({"anything": 1.0}) is None


def test_all_configured_kill_rules_are_parseable():
    """配置里的每条规则都必须能被解析，否则会静默失效。"""
    rules = load_rules(load_agent_config())
    assert rules
    for rule in rules:
        assert rule.evaluate({}) is None            # 空证据一律判不了
        assert rule.rule_id and rule.reason


# ---------------------------------------------------------------- Verifier

def test_verifier_rejects_evidence_without_source_ref():
    result = RuleVerifier().verify(make_item(source_ref=""))
    assert result.verdict == "rejected"


def test_verifier_rejects_literature_claim_without_quote():
    result = RuleVerifier().verify(make_item(context={}))
    assert result.verdict == "rejected"


def test_verifier_accepts_literature_claim_with_supporting_quote():
    item = make_item(context={"quote": "GENE mediates clathrin endocytosis"})
    assert RuleVerifier().verify(item).verdict == "verified"


def test_verifier_defers_instead_of_rejecting_when_it_cannot_judge():
    """跨语言蕴含规则层判断不了，必须是 needs_more 而非 rejected，避免误杀真证据。"""
    item = make_item(claim="该基因是经典内吞受体",
                     context={"quote": "unrelated english text about something else"})
    assert RuleVerifier().verify(item).verdict == "needs_more"


def test_verifier_accepts_structured_evidence_without_quote():
    """数据库数值本身即证据，不该要求原文引用。"""
    item = make_item(
        dimension="tumor_expression", source_id="fixtures:tcga",
        independence_group="tcga", layer="rna", value=6510.8, unit="RSEM",
        score=8.8, context={},
    )
    assert RuleVerifier().verify(item).verdict == "verified"


# ---------------------------------------------------------------- 端到端

def test_agent_run_is_reproducible():
    """同一份 fixture 跑两次必须完全一致，否则评测无意义。"""
    a = SearchAndVerifyAgent("hcc").run()
    b = SearchAndVerifyAgent("hcc").run()
    assert a.state.budget.source_calls == b.state.budget.source_calls
    assert {c.symbol for c in a.state.killed()} == {c.symbol for c in b.state.killed()}
    assert [r["target_symbol"] for r in a.contract_rows] == \
           [r["target_symbol"] for r in b.contract_rows]


def test_secreted_protein_is_killed_before_deep_search():
    """AFP 肿瘤表达极高但是分泌型，必须在花掉深搜成本前被淘汰。"""
    result = SearchAndVerifyAgent("hcc").run()
    afp = result.state.candidates["AFP"]
    assert afp.status == "killed"
    assert any("no_membrane" in r for r in afp.kill_reasons)
    assert afp.source_calls <= 4


def test_reference_tissue_dominant_target_classified_as_benchmark():
    """正常肝极高表达的 ASGR1 必须自动判为 benchmark，而不是肿瘤主线。"""
    result = SearchAndVerifyAgent("hcc").run()
    assert classify_route(result.state, result.state.candidates["ASGR1"]) == "benchmark"


def test_known_hcc_answer_is_reproduced():
    """HCC 回归：GPC3 必须是 main 路线，这是通用化没做偏的判据。"""
    result = SearchAndVerifyAgent("hcc").run()
    rows = {r["target_symbol"]: r for r in result.contract_rows}
    assert rows["GPC3"]["route_type"] == "main"
    assert float(rows["GPC3"]["tumor_expression_score"]) >= 7.0


def test_no_score_without_evidence():
    """契约表里的每个分数都必须有对应证据，不允许凭空出现。"""
    result = SearchAndVerifyAgent("hcc").run()
    for row in result.contract_rows:
        symbol = row["target_symbol"]
        if row["tumor_expression_score"]:
            assert result.state.store.for_target(symbol, "tumor_expression")


def test_every_row_carries_citations():
    result = SearchAndVerifyAgent("hcc").run()
    for row in result.contract_rows:
        assert row["source_refs"].strip(), f"{row['target_symbol']} 缺少来源"


# ---------------------------------------------------------------- 消融

def test_disabling_early_kill_costs_more():
    """early-kill 的价值必须能被量化，否则说不清它值不值得存在。"""
    base = load_agent_config()
    with_kill = SearchAndVerifyAgent("hcc", agent_config=base).run()
    without = SearchAndVerifyAgent("hcc", agent_config={
        **base, "ablation": {**(base.get("ablation") or {}), "disable_early_kill": True},
    }).run()
    assert without.state.budget.cost_units > with_kill.state.budget.cost_units
    assert not without.state.killed()


def test_disabling_verifier_marks_evidence_as_unchecked():
    """关掉验证后，证据仍要如实记录『未经核对』，不能假装通过了验证。"""
    base = load_agent_config()
    result = SearchAndVerifyAgent("hcc", agent_config={
        **base, "ablation": {**(base.get("ablation") or {}), "disable_verifier": True},
    }).run()
    notes = [i.verifier_note for i in result.state.store.all()]
    assert any("未经核对" in n for n in notes)


def test_agent_costs_less_than_fixed_workflow():
    """Agent 相对固定工作流的成本优势是核心卖点，必须持续可验证。"""
    agent_result = SearchAndVerifyAgent("hcc").run()
    baseline_result = run_baseline("hcc")
    report = compare(agent_result, baseline_result)
    assert report["cost_saved_units"] > 0
    # 省钱不能以漏掉候选为代价：Agent 的短名单必须被基线短名单包含
    assert set(report["agent"]["shortlist"]).issubset(set(report["baseline"]["shortlist"]))


def test_baseline_completes_within_budget():
    """基线必须能在预算内跑完，否则对照实验的成本数字是被截断的。"""
    result = run_baseline("hcc")
    assert not result.state.budget.exhausted()
