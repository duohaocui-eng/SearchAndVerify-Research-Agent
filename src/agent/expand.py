"""候选扩展：从已有存活候选出发，找通路/家族邻居加入候选池。

优先读本地邻接表（离线确定、可评测）；未设置 SNV_TEST_OFFLINE 时
再尝试 STRING API（无需 key）。扩展失败必须诚实返回空，绝不编造基因。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from evidence_core.config import PROJECT_ROOT
from evidence_core.http import SourceUnavailable, build_url, request_json

NEIGHBORS_PATH = PROJECT_ROOT / "data" / "expansion_neighbors.json"
STRING_BASE = "https://string-db.org/api"


def _load_local_neighbors() -> dict[str, list[str]]:
    if not NEIGHBORS_PATH.is_file():
        return {}
    data = json.loads(NEIGHBORS_PATH.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for key, values in data.items():
        if key.startswith("_") or not isinstance(values, list):
            continue
        out[str(key).upper()] = [str(v).strip().upper() for v in values if str(v).strip()]
    return out


def _string_partners(symbol: str, limit: int = 5) -> list[str]:
    if os.environ.get("SNV_TEST_OFFLINE"):
        return []
    try:
        url = build_url(STRING_BASE, "json/interaction_partners", {
            "identifiers": symbol,
            "species": 9606,
            "limit": limit,
        })
        data = request_json(url, source_id="string", cache_ttl=24 * 3600)
    except SourceUnavailable:
        return []
    if not isinstance(data, list):
        return []
    partners: list[str] = []
    for row in data:
        name = str(row.get("preferredName_B") or "").strip().upper()
        if name and name != symbol.upper():
            partners.append(name)
    return partners


def suggest_expansions(
    seed_symbols: list[str],
    *,
    already: set[str],
    max_new: int = 5,
) -> list[dict[str, Any]]:
    """返回待加入的候选条目（symbol / discovered_by）。"""
    local = _load_local_neighbors()
    found: list[dict[str, Any]] = []
    seen = {s.upper() for s in already}

    for seed in seed_symbols:
        key = seed.upper()
        candidates = list(local.get(key) or [])
        for partner in _string_partners(seed):
            if partner not in candidates:
                candidates.append(partner)
        for symbol in candidates:
            if symbol in seen:
                continue
            seen.add(symbol)
            source = "local_neighbors" if symbol in (local.get(key) or []) else "string"
            found.append({
                "symbol": symbol,
                "aliases": [symbol],
                "protein_name": "",
                "discovered_by": f"expand:{source}:{seed}",
            })
            if len(found) >= max_new:
                return found
    return found
