"""运行报告生成。

报告的原则是「可核对优先于好看」：
每个结论后面带 evidence_id，每个缺口和冲突都写出来，
被淘汰的候选写清楚为什么被淘汰、以及淘汰时还剩多少证据没查。

不写「本项目发现了新靶点」这类话。系统做的是候选筛选与证据整理，
真正的发现需要湿实验，报告里必须把这条边界写明（对应 Q6）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from evidence_core.schema import DIMENSIONS

from .loop import RunResult


def _fmt(value: Any, dash: str = "—") -> str:
    return dash if value in (None, "", "-") else str(value)


def render_markdown(result: RunResult, gate_ranking: list[dict[str, str]] | None = None) -> str:
    state = result.state
    cfg = state.cancer_config
    lines: list[str] = []

    lines.append(f"# {cfg.get('cancer_type_full', state.cancer_id)} 递送靶点筛选报告")
    lines.append("")
    lines.append(f"- run_id：`{state.run_id}`")
    lines.append(f"- 轨迹类型：`{result.trace_class}`")
    lines.append(f"- 停止原因：{result.stop_reason}")
    lines.append(f"- 数据源调用：{state.budget.source_calls} 次，成本 {state.budget.cost_units} 单位，"
                 f"共 {state.budget.steps} 步")
    lines.append("")

    # ---- 边界声明放在最前面，避免结论被过度解读 ----
    lines.append("## 结论边界")
    lines.append("")
    lines.append("本报告是**候选筛选与证据整理**的结果，不构成新生物学靶点的发现。")
    lines.append("所有分数由确定性规则从公开数据计算得出，用于排序与风险提示；")
    lines.append("任何候选进入实际研发前，仍需湿实验验证结合、内吞与安全窗。")
    fixture_used = any(i.source_id.startswith("fixtures") for i in state.store.all())
    if fixture_used:
        lines.append("")
        lines.append("> **注意**：本次运行使用了离线 fixture 数据（真实数据源 API 尚未接入），"
                     "结论仅用于验证流程，不可用于研发决策。")
    for note in state.notes:
        lines.append(f"- {note}")
    lines.append("")

    # ---- 排序 ----
    lines.append("## 候选排序")
    lines.append("")
    if gate_ranking:
        lines.append("下表来自下游可行性闸门模型（delivery-gate-model）的 gate 评分。")
        lines.append("")
        lines.append("| 排名 | 靶点 | 路线 | 总分 | 建议 | 主要风险 |")
        lines.append("|:---:|:---|:---|---:|:---|:---|")
        for row in gate_ranking:
            lines.append(
                f"| {_fmt(row.get('priority_rank'))} | {_fmt(row.get('target_symbol'))} "
                f"| {_fmt(row.get('route_type'))} | {_fmt(row.get('overall_score'))} "
                f"| {_fmt(row.get('recommendation'))} | {_fmt(row.get('main_risks'))} |"
            )
    else:
        lines.append("_闸门模型未运行，以下为 Agent 侧证据摘要。_")
        lines.append("")
        lines.append("| 靶点 | 路线 | 状态 | 覆盖率 | 证据级别 |")
        lines.append("|:---|:---|:---|---:|---:|")
        for row in result.contract_rows:
            symbol = row["target_symbol"]
            candidate = state.candidates.get(symbol)
            lines.append(
                f"| {symbol} | {row['route_type']} "
                f"| {candidate.status if candidate else '?'} "
                f"| {state.coverage(symbol):.0%} | {_fmt(row.get('evidence_level'))} |"
            )
    lines.append("")

    # ---- 淘汰 ----
    killed = state.killed()
    if killed:
        lines.append("## 提前淘汰（early-kill）")
        lines.append("")
        lines.append("这些候选在完成全部取证前就被规则否决，省下了后续深度搜索的成本。")
        lines.append("")
        for candidate in killed:
            lines.append(f"### {candidate.symbol}")
            for reason in candidate.kill_reasons:
                lines.append(f"- {reason}")
            unqueried = state.gaps_for(candidate.symbol)
            if unqueried:
                labels = [DIMENSIONS[d]["label"] for d in unqueried if d in DIMENSIONS]
                lines.append(f"- 淘汰时尚未查询：{'、'.join(labels)}（因此不再产生调用）")
            lines.append("")

    # ---- 冲突 ----
    conflicts = state.store.conflicts
    if conflicts:
        lines.append("## 证据冲突")
        lines.append("")
        lines.append("| 靶点 | 维度 | 类型 | 详情 | 是否已解决 |")
        lines.append("|:---|:---|:---|:---|:---:|")
        for c in conflicts:
            status = "是" if c.resolved else "否"
            lines.append(f"| {c.target} | {DIMENSIONS.get(c.dimension, {}).get('label', c.dimension)} "
                         f"| {c.kind} | {c.detail} | {status} |")
        lines.append("")

    # ---- 逐候选证据 ----
    lines.append("## 逐候选证据明细")
    lines.append("")
    for row in result.contract_rows:
        symbol = row["target_symbol"]
        candidate = state.candidates.get(symbol)
        lines.append(f"### {symbol}"
                     f"{f'（{candidate.identity.protein_name}）' if candidate and candidate.identity.protein_name else ''}")
        lines.append("")
        lines.append(f"- 路线角色：`{row['route_type']}`（由肿瘤/正常组织对比度自动判定）")
        lines.append(f"- 状态：`{candidate.status if candidate else '?'}`")
        lines.append(f"- 证据级别：{_fmt(row.get('evidence_level'))} / 5")
        lines.append("")
        items = state.store.for_target(symbol)
        if items:
            lines.append("| 维度 | 值 | 分数 | 来源 | 验证 | evidence_id |")
            lines.append("|:---|:---|---:|:---|:---|:---|")
            for item in items:
                label = DIMENSIONS.get(item.dimension, {}).get("label", item.dimension)
                value = f"{item.value} {item.unit}".strip() if item.value is not None else "定性"
                lines.append(
                    f"| {label} | {value} | {_fmt(item.score)} | {item.source_id} "
                    f"| {item.verification} | `{item.evidence_id}` |"
                )
            lines.append("")
        if row.get("notes"):
            lines.append(f"**备注**：{row['notes']}")
            lines.append("")

    # ---- 下一步数据需求 ----
    lines.append("## 下一步病人数据 / 实验需求")
    lines.append("")
    lines.append("由未关闭的风险与缺失证据倒推得出：")
    lines.append("")
    any_need = False
    for candidate in state.shortlisted_ever():
        gaps = state.gaps_for(candidate.symbol)
        conflicts_here = state.store.open_conflicts(candidate.symbol)
        if not gaps and not conflicts_here:
            continue
        any_need = True
        lines.append(f"- **{candidate.symbol}**：")
        for gap in gaps:
            lines.append(f"  - 补充 {DIMENSIONS.get(gap, {}).get('label', gap)} 证据")
        for c in conflicts_here:
            if c.kind == "layer_disagreement":
                lines.append(f"  - 用蛋白层方法（IHC / 流式）仲裁 {c.dimension} 的 RNA-蛋白分歧")
            else:
                lines.append(f"  - 引入第三方独立来源仲裁 {c.dimension} 的数值分歧")
    if not any_need:
        lines.append("- 短名单候选的必需维度均已覆盖且无未决冲突。")
    lines.append("")

    return "\n".join(lines)


def write_report(result: RunResult, path: str | Path,
                 gate_ranking: list[dict[str, str]] | None = None) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_markdown(result, gate_ranking), encoding="utf-8")
    return p
