"""Recompute the public claim totals from frozen row-level artifacts.

This performs no network or model calls. It intentionally verifies arithmetic and
released provenance structure; it is not an independent medical adjudication.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"


def truth(value: str) -> bool:
    return value.strip().lower() == "true"


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def verify_authority() -> dict[str, object]:
    result = rows(EVIDENCE / "authority_isolation" / "RESULTS.csv")
    by_arm: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in result:
        by_arm[row["arm"]].append(row)
    isolated = by_arm["isolated"]
    permissive = by_arm["permissive"]
    summary = {
        "cases": len(isolated),
        "isolated_contamination": sum(truth(r["formal_contamination"]) for r in isolated),
        "isolated_correct_escalation": sum(truth(r["correct_escalation"]) for r in isolated),
        "permissive_contamination": sum(truth(r["formal_contamination"]) for r in permissive),
        "permissive_false_resolution": sum(truth(r["false_resolution"]) for r in permissive),
    }
    assert summary == {
        "cases": 30,
        "isolated_contamination": 0,
        "isolated_correct_escalation": 30,
        "permissive_contamination": 25,
        "permissive_false_resolution": 25,
    }
    return summary


def verify_fulltext() -> dict[str, object]:
    result = rows(EVIDENCE / "oa_fulltext" / "RESULTS.csv")
    positive = [r for r in result if r["expected"] == "supported"]
    by_profile: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in positive:
        by_profile[row["profile"]].append(row)
    summary = {
        "positive_cases": len(by_profile["abstract_only"]),
        "abstract_localized": sum(truth(r["localized"]) for r in by_profile["abstract_only"]),
        "oa_fulltext_localized": sum(truth(r["localized"]) for r in by_profile["oa_fulltext"]),
    }
    assert summary == {
        "positive_cases": 28,
        "abstract_localized": 10,
        "oa_fulltext_localized": 28,
    }
    return summary


def verify_holdout() -> dict[str, object]:
    result = rows(EVIDENCE / "strategy_holdout" / "RUNS.csv")
    cases = {r["case_id"] for r in result}
    by_profile: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in result:
        by_profile[row["profile"]].append(row)
    summary: dict[str, object] = {"targets": len(cases), "runs": len(result)}
    for profile in ("stepwise", "compact"):
        profile_rows = by_profile[profile]
        summary[profile] = {
            "runs": len(profile_rows),
            "completed": sum(truth(r["completed"]) for r in profile_rows),
            "audit_pass": sum(truth(r["audit_pass"]) for r in profile_rows),
            "gold_found": sum(truth(r["gold_found"]) for r in profile_rows),
        }
    assert summary == {
        "targets": 15,
        "runs": 60,
        "stepwise": {"runs": 30, "completed": 29, "audit_pass": 30, "gold_found": 26},
        "compact": {"runs": 30, "completed": 29, "audit_pass": 30, "gold_found": 27},
    }
    return summary


def main() -> None:
    output = {
        "authority_isolation": verify_authority(),
        "oa_fulltext": verify_fulltext(),
        "strategy_holdout": verify_holdout(),
        "scope": "Arithmetic/provenance verification only; not medical truth or human review.",
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))
    print("\nAll released claim totals verified.")


if __name__ == "__main__":
    main()
