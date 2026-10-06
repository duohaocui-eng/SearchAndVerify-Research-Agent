"""Small deterministic demo: no network access and no LLM key required."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ["SNV_TEST_OFFLINE"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent.loop import SearchAndVerifyAgent  # noqa: E402


def main() -> None:
    result = SearchAndVerifyAgent("hcc").run()
    rows = [
        {
            "target": row["target_symbol"],
            "route": row["route_type"],
            "evidence_level": row["evidence_level"],
            "source_refs": row["source_refs"],
        }
        for row in result.contract_rows
    ]
    payload = {
        "mode": "offline-fixtures",
        "source_calls": result.state.budget.source_calls,
        "cost_units": result.state.budget.cost_units,
        "candidates": rows,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
