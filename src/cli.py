"""命令行入口。

    python src/cli.py run hcc                  跑一次 Agent
    python src/cli.py baseline hcc             跑固定工作流基线
    python src/cli.py compare hcc              Agent vs 基线 对照
    python src/cli.py ablation hcc             跑全部消融组合
    python src/cli.py sources                  查看数据源接入状态
    python src/cli.py cancers                  查看已配置癌种
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.baseline import compare, run_baseline                    # noqa: E402
from agent.gate_driver import DEFAULT_GATE_ROOT, drive              # noqa: E402
from agent.loop import RunResult, SearchAndVerifyAgent              # noqa: E402
from agent.report import write_report                               # noqa: E402
from evidence_core.config import CONFIG_DIR, PROJECT_ROOT, load_agent_config  # noqa: E402
from evidence_core.contract_writer import write_contract            # noqa: E402
from evidence_core.io import write_json                             # noqa: E402
from evidence_core.sources import build_registry                    # noqa: E402

OUTPUT_ROOT = PROJECT_ROOT / "outputs"


def _persist(result: RunResult, label: str, *, run_gate: bool) -> Path:
    """把一次运行的全部产物落盘到 outputs/<label>/<run_id>/。"""
    out_dir = OUTPUT_ROOT / label / result.state.run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    contract_csv = out_dir / "target_evidence.csv"
    emit_legacy = bool((result.state.agent_config.get("output") or {}).get("emit_legacy_columns", True))
    write_contract(result.contract_rows, contract_csv, emit_legacy=emit_legacy)

    result.state.store.write_jsonl(out_dir / "evidence.jsonl")
    result.trace.write(out_dir / "trace.jsonl")
    write_json(out_dir / "summary.json", result.summary())
    write_json(out_dir / "state_snapshot.json", result.state.snapshot())
    write_json(out_dir / "probe.json", result.state.probe_results)
    if result.llm_calls:
        # LLM 未接入或未调用时这个列表是空的，不额外产出文件。
        write_json(out_dir / "llm_calls.json", result.llm_calls)

    ranking = []
    if run_gate:
        gate = drive(contract_csv, result.state.cancer_config)
        write_json(out_dir / "gate_result.json", gate)
        ranking = gate.get("ranking") or []

    write_report(result, out_dir / "report.md", ranking)
    return out_dir


def cmd_run(args: argparse.Namespace) -> int:
    agent = SearchAndVerifyAgent(args.cancer)
    result = agent.run()
    out_dir = _persist(result, "agent", run_gate=args.gate)

    print(json.dumps(result.summary(), ensure_ascii=False, indent=2))
    print(f"\n产物目录：{out_dir}")
    if not args.gate:
        print("提示：加 --gate 可把契约表写入 delivery-gate-model 并跑评分（会先备份原文件）")
    return 0


def cmd_baseline(args: argparse.Namespace) -> int:
    result = run_baseline(args.cancer)
    out_dir = _persist(result, "baseline", run_gate=args.gate)
    print(json.dumps(result.summary(), ensure_ascii=False, indent=2))
    print(f"\n产物目录：{out_dir}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    agent_result = SearchAndVerifyAgent(args.cancer).run()
    baseline_result = run_baseline(args.cancer)

    _persist(agent_result, "agent", run_gate=False)
    _persist(baseline_result, "baseline", run_gate=False)

    report = compare(agent_result, baseline_result)
    out = OUTPUT_ROOT / "compare" / f"{args.cancer}_compare.json"
    write_json(out, report)

    print("=== Agent vs 固定工作流 ===")
    print(f"  数据源调用   Agent {report['agent']['source_calls']:4d}  |  基线 {report['baseline']['source_calls']:4d}")
    print(f"  成本单位     Agent {report['agent']['cost_units']:4d}  |  基线 {report['baseline']['cost_units']:4d}")
    print(f"  已验证证据   Agent {report['agent']['evidence_verified']:4d}  |  基线 {report['baseline']['evidence_verified']:4d}")
    print(f"  节省成本     {report['cost_saved_units']} 单位（{report['cost_saved_ratio']:.1%}）")
    print(f"  短名单一致   {report['same_shortlist']}  交集 {report['shortlist_overlap']}")
    print(f"\n已写入 {out}")
    return 0


def cmd_ablation(args: argparse.Namespace) -> int:
    """跑全部消融组合，回答「哪个部件贡献了什么」。"""
    base = load_agent_config()
    variants = {
        "full": {},
        "no_verifier": {"disable_verifier": True},
        "no_replanning": {"disable_dynamic_replanning": True},
        "no_early_kill": {"disable_early_kill": True},
        "structured_only": {"structured_only": True},
    }

    rows = []
    for name, overrides in variants.items():
        cfg = {**base, "ablation": {**(base.get("ablation") or {}), **overrides}}
        result = SearchAndVerifyAgent(args.cancer, agent_config=cfg).run()
        summary = result.state.store.summary()
        rows.append({
            "variant": name,
            "source_calls": result.state.budget.source_calls,
            "cost_units": result.state.budget.cost_units,
            "evidence_total": summary["total"],
            "evidence_verified": summary["verified"],
            "evidence_rejected": summary["rejected"],
            "killed": [c.symbol for c in result.state.killed()],
            "shortlist": [c.symbol for c in result.state.shortlisted_ever()],
            "open_conflicts": summary["open_conflicts"],
            "stop_reason": result.stop_reason,
        })

    out = OUTPUT_ROOT / "ablation" / f"{args.cancer}_ablation.json"
    write_json(out, rows)

    print(f"{'variant':16s} {'calls':>6s} {'cost':>6s} {'ev':>5s} {'verif':>6s} {'rej':>5s} {'killed'}")
    for row in rows:
        print(f"{row['variant']:16s} {row['source_calls']:6d} {row['cost_units']:6d} "
              f"{row['evidence_total']:5d} {row['evidence_verified']:6d} "
              f"{row['evidence_rejected']:5d} {row['killed']}")
    print(f"\n已写入 {out}")
    return 0


def cmd_sources(_: argparse.Namespace) -> int:
    registry = build_registry()
    print(f"{'source':16s} {'可用':6s} {'可靠性':8s} {'成本':>4s}  说明")
    print("-" * 92)
    for row in registry.availability_report():
        mark = "是" if row["available"] else "否"
        print(f"{row['source_id']:16s} {mark:6s} {row['reliability']:8s} "
              f"{row['cost_weight']:>4d}  {row['reason'] or '已接入'}")
    print()
    print("提供的证据维度：")
    for row in registry.availability_report():
        print(f"  {row['source_id']:16s} {', '.join(row['provides'])}")
    return 0


def cmd_cancers(_: argparse.Namespace) -> int:
    from evidence_core.config import load_cancer_config

    folder = CONFIG_DIR / "cancer_types"
    ids = sorted(p.stem for p in folder.glob("*.yaml") if not p.stem.startswith("_"))
    if not ids:
        print("尚未配置任何癌种。复制 configs/cancer_types/_template.yaml 新增。")
        return 0
    print(f"{'cancer_id':10s} {'简称':8s} {'参照正常组织':16s} 队列")
    print("-" * 76)
    for cancer_id in ids:
        cfg = load_cancer_config(cancer_id)
        cohort = ((cfg.get("sources") or {}).get("cbioportal") or {}).get("study_id", "—")
        print(f"{cancer_id:10s} {str(cfg.get('cancer_type_short','')):8s} "
              f"{str(cfg.get('reference_normal_tissue','')):16s} {cohort}")
    return 0


def cmd_gate_check(_: argparse.Namespace) -> int:
    exists = DEFAULT_GATE_ROOT.exists()
    print(f"闸门模型目录：{DEFAULT_GATE_ROOT}")
    print(f"存在：{exists}")
    if exists:
        script = DEFAULT_GATE_ROOT / "scripts" / "score_targets.py"
        print(f"评分脚本：{script} 存在={script.exists()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="search-and-verify",
        description="通用递送靶点 搜索-验证 Agent",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="跑一次 Agent")
    p_run.add_argument("cancer", help="癌种 id，如 hcc")
    p_run.add_argument("--gate", action="store_true",
                       help="把契约表写入 delivery-gate-model 并跑评分（会先备份被覆盖的文件）")
    p_run.set_defaults(func=cmd_run)

    p_base = sub.add_parser("baseline", help="跑固定工作流基线")
    p_base.add_argument("cancer")
    p_base.add_argument("--gate", action="store_true")
    p_base.set_defaults(func=cmd_baseline)

    p_cmp = sub.add_parser("compare", help="Agent vs 基线 对照")
    p_cmp.add_argument("cancer")
    p_cmp.set_defaults(func=cmd_compare)

    p_abl = sub.add_parser("ablation", help="消融实验")
    p_abl.add_argument("cancer")
    p_abl.set_defaults(func=cmd_ablation)

    sub.add_parser("sources", help="数据源接入状态").set_defaults(func=cmd_sources)
    sub.add_parser("cancers", help="已配置癌种").set_defaults(func=cmd_cancers)
    sub.add_parser("gate-check", help="检查闸门模型连接").set_defaults(func=cmd_gate_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
