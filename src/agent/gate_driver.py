"""驱动下游 delivery-gate-model：写契约、生成癌种配置、跑评分。

与闸门模型的连接方式沿用既有约定：单向数据契约 + 子进程调用，两边代码互不导入。
这么做的理由是隔离——闸门模型有自己的 conda 环境和依赖，
用子进程调用可以让它保持独立演进，Agent 侧升级不会波及它。

「自动生成 indication.yaml」是通用化的最后一块拼图：
闸门引擎本身已经癌种无关，但它需要知道参照正常组织是什么。
以前这个文件靠人手写，现在由癌种配置自动生成。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from evidence_core.config import PROJECT_ROOT

# 闸门模型在同一个仓库里，位于兄弟目录
DEFAULT_GATE_ROOT = PROJECT_ROOT.parent / "delivery-gate-model"


def render_indication_yaml(cancer_config: dict[str, Any]) -> str:
    """把癌种配置翻译成闸门模型的 indication.yaml。"""
    expected = cancer_config.get("expected_order") or []
    benchmarks = cancer_config.get("benchmark_targets") or []
    return "\n".join([
        "# 本文件由 SearchAndVerifyCycle_agents 自动生成，请勿手改。",
        f"# 来源：configs/cancer_types/{cancer_config.get('cancer_id', '')}.yaml",
        "",
        f"cancer_type_short: {cancer_config.get('cancer_type_short', '')}",
        f"cancer_type_full: {cancer_config.get('cancer_type_full', '')}",
        f"reference_normal_tissue: {str(cancer_config.get('reference_normal_tissue', '')).lower()}",
        f"reference_normal_tissue_label: {cancer_config.get('reference_normal_tissue_label', '')}",
        f"expected_order: [{', '.join(expected)}]",
        f"benchmark_targets: [{', '.join(benchmarks)}]",
        "",
    ])


def _backup(path: Path) -> str | None:
    """覆盖闸门模型的现有输入前先备份。

    闸门模型里躺着人工核对过的 HCC 数据，Agent 一次误跑就把它冲掉是不可接受的。
    备份只做一次（.orig 已存在就不再覆盖），保证最初的人工版本永远留得住。
    """
    if not path.exists():
        return None
    backup = path.with_suffix(path.suffix + ".orig")
    if not backup.exists():
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        return str(backup)
    return str(backup)


def sync_to_gate(
    contract_csv: Path,
    cancer_config: dict[str, Any],
    gate_root: Path | None = None,
    *,
    write_indication: bool = True,
) -> dict[str, Any]:
    """把契约表与癌种配置同步到闸门模型目录。会先备份被覆盖的文件。"""
    root = gate_root or DEFAULT_GATE_ROOT
    if not root.exists():
        return {"ok": False, "detail": f"未找到闸门模型目录 {root}"}

    target_csv = root / "data" / "inputs" / "target_evidence.csv"
    target_csv.parent.mkdir(parents=True, exist_ok=True)
    backups = [b for b in [_backup(target_csv)] if b]
    target_csv.write_text(Path(contract_csv).read_text(encoding="utf-8"), encoding="utf-8")

    written: list[str] = [str(target_csv)]
    if write_indication:
        indication = root / "configs" / "indication.yaml"
        indication.parent.mkdir(parents=True, exist_ok=True)
        b = _backup(indication)
        if b:
            backups.append(b)
        indication.write_text(render_indication_yaml(cancer_config), encoding="utf-8")
        written.append(str(indication))

    return {"ok": True, "written": written, "backups": backups}


def run_gate_scoring(gate_root: Path | None = None, timeout: int = 120) -> dict[str, Any]:
    """跑闸门模型的评分脚本。"""
    root = gate_root or DEFAULT_GATE_ROOT
    script = root / "scripts" / "score_targets.py"
    if not script.exists():
        return {"ok": False, "detail": f"未找到 {script}"}

    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(root), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": f"闸门模型评分超时（{timeout}s）"}

    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "scores_csv": str(root / "data" / "curated" / "target_scores.csv"),
        "report_md": str(root / "reports" / "hcc_delivery_gate_report.md"),
    }


def read_gate_scores(gate_root: Path | None = None) -> list[dict[str, str]]:
    from evidence_core.io import read_csv

    root = gate_root or DEFAULT_GATE_ROOT
    path = root / "data" / "curated" / "target_scores.csv"
    return read_csv(path) if path.exists() else []


def drive(
    contract_csv: Path,
    cancer_config: dict[str, Any],
    gate_root: Path | None = None,
    *,
    execute: bool = True,
) -> dict[str, Any]:
    """一步完成：同步 -> 评分 -> 读回排序。"""
    sync = sync_to_gate(contract_csv, cancer_config, gate_root)
    if not sync.get("ok") or not execute:
        return {"sync": sync, "scoring": None, "ranking": []}

    scoring = run_gate_scoring(gate_root)
    ranking = read_gate_scores(gate_root) if scoring.get("ok") else []
    return {"sync": sync, "scoring": scoring, "ranking": ranking}
