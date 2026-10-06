"""实体归一化：癌种名、基因别名、蛋白名、数据库 ID 的统一。

为什么这是通用化的前提（对应 Q19/Q45）：
老模型把 ASGPR->ASGR1、entrez 432、gencode ENSG00000141505 这三层映射硬编码在
route2.py 里，只覆盖三个靶点。换癌种就会立刻遇到「候选叫什么、各数据库怎么查」的问题。
这里把映射变成运行时可解析 + 本地缓存 + 可扩展别名表。

在线解析器（HGNC / MyGene / UniProt）当前是 stub，未接入时自动降级到本地别名表，
并在结果里标记 resolved_by='local'，让报告能说清 ID 是怎么来的。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .http import SourceUnavailable, build_url, request_json

ALIAS_TABLE_PATH = PROJECT_ROOT / "data" / "alias_table.json"
MYGENE_BASE_URL = "https://mygene.info/v3"


@dataclass
class GeneIdentity:
    """一个靶点在各数据库里的身份。查不到的 ID 留空，不猜。"""

    query: str                      # 用户/候选生成器给的原始名
    symbol: str = ""                # HGNC 官方 symbol
    aliases: list[str] = field(default_factory=list)
    protein_name: str = ""
    entrez_id: str = ""
    ensembl_id: str = ""
    uniprot_id: str = ""
    gencode_id: str = ""
    resolved_by: str = "unresolved"  # hgnc / mygene / local / unresolved
    warnings: list[str] = field(default_factory=list)

    @property
    def is_resolved(self) -> bool:
        return bool(self.symbol)

    def db_id(self, source_id: str) -> str:
        """按数据源取它需要的 ID 形式。新增数据源时在这里加一条。"""
        return {
            "cbioportal": self.entrez_id,
            "tcga_gdc": self.ensembl_id,
            "gtex": self.gencode_id or self.ensembl_id,
            "hpa": self.ensembl_id,
            "uniprot": self.uniprot_id,
            "depmap": self.symbol,
            "pubmed": self.symbol,
            "clinicaltrials": self.symbol,
            "fixtures": self.symbol,
        }.get(source_id, self.symbol)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "symbol": self.symbol,
            "aliases": self.aliases,
            "protein_name": self.protein_name,
            "entrez_id": self.entrez_id,
            "ensembl_id": self.ensembl_id,
            "uniprot_id": self.uniprot_id,
            "gencode_id": self.gencode_id,
            "resolved_by": self.resolved_by,
            "warnings": self.warnings,
        }


# ----------------------------------------------------------------------------
# 本地别名表（离线兜底 + 在线解析结果的缓存）
# ----------------------------------------------------------------------------

def _load_alias_table() -> dict[str, dict[str, Any]]:
    if not ALIAS_TABLE_PATH.exists():
        return {}
    try:
        return json.loads(ALIAS_TABLE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_alias_table(table: dict[str, dict[str, Any]]) -> None:
    ALIAS_TABLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALIAS_TABLE_PATH.write_text(
        json.dumps(table, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def _lookup_local(query: str, table: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    key = query.strip().upper()
    if key in table:
        return table[key]
    # 反查别名
    for entry in table.values():
        if key in [a.upper() for a in entry.get("aliases", [])]:
            return entry
    return None


# ----------------------------------------------------------------------------
# 在线解析（TODO: 待接入）
# ----------------------------------------------------------------------------

_MYGENE_FIELDS = "entrezgene,ensembl.gene,uniprot.Swiss-Prot,symbol,name,alias"


def _mygene_hit_to_identity(hit: dict[str, Any]) -> dict[str, Any]:
    ensembl = hit.get("ensembl") or {}
    if isinstance(ensembl, list):
        ensembl = ensembl[0] if ensembl else {}
    uniprot = hit.get("uniprot") or {}
    sp = uniprot.get("Swiss-Prot") if isinstance(uniprot, dict) else None
    if isinstance(sp, list):
        sp = sp[0] if sp else ""
    aliases = hit.get("alias") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    return {
        "symbol": str(hit.get("symbol") or ""),
        "aliases": list(aliases),
        "protein_name": str(hit.get("name") or ""),
        "entrez_id": str(hit.get("entrezgene") or ""),
        "ensembl_id": str(ensembl.get("gene") or "") if isinstance(ensembl, dict) else "",
        "uniprot_id": str(sp or ""),
        "resolved_by": "mygene",
    }


def _resolve_online(query: str) -> dict[str, Any] | None:
    """通过 MyGene.info 解析基因身份（symbol / entrez / ensembl / uniprot 一次拿全）。

    无需 API key。策略分两步，避免模糊匹配把别名解析到错误基因上：
      1) 先按 symbol 精确查，命中即返回（symbol 在人类基因组里唯一）；
      2) 精确查不到时按别名模糊查，但只接受「候选项的 alias 列表里真的包含这个
         查询词」的命中——而不是简单取相关性最高的第一条。
         例：查 "ASGPR" 时相关性最高的是 CLEC10A（别名也含糊类似词），
         但真正含精确别名 "ASGPR" 的是 ASGR1，必须按精确别名过滤而不是按分数取。
    找不到就返回 None，调用方会自动降级到本地别名表，绝不编造 ID。
    """
    try:
        exact_url = build_url(MYGENE_BASE_URL, "query", {
            "q": f"symbol:{query}", "species": "human", "fields": _MYGENE_FIELDS,
        })
        exact = request_json(exact_url, source_id="mygene")
        hits = (exact or {}).get("hits") or []
        if hits:
            return _mygene_hit_to_identity(hits[0])

        fuzzy_url = build_url(MYGENE_BASE_URL, "query", {
            "q": query, "species": "human", "fields": _MYGENE_FIELDS,
        })
        fuzzy = request_json(fuzzy_url, source_id="mygene")
        hits = (fuzzy or {}).get("hits") or []
        key = query.strip().upper()
        for hit in hits:
            symbol = str(hit.get("symbol") or "").upper()
            aliases_upper = [str(a).upper() for a in (hit.get("alias") or [])]
            if key == symbol or key in aliases_upper:
                identity = _mygene_hit_to_identity(hit)
                identity["warnings"] = [f"'{query}' 未精确匹配 symbol，按别名列表命中 {identity['symbol']}"]
                return identity
        return None
    except SourceUnavailable:
        return None


# ----------------------------------------------------------------------------
# 对外入口
# ----------------------------------------------------------------------------

def normalize_gene(query: str, use_online: bool = True, cache: bool = True) -> GeneIdentity:
    """把任意写法的靶点名解析成统一身份。

    SNV_TEST_OFFLINE 环境变量（由 tests/conftest.py 自动设置）会强制 use_online=False，
    保证单元测试不依赖真实网络；生产环境不设它，行为不受影响。
    """
    q = query.strip()
    if not q:
        return GeneIdentity(query=query, warnings=["空的靶点名"])

    table = _load_alias_table()

    if use_online and not os.environ.get("SNV_TEST_OFFLINE"):
        online = _resolve_online(q)
        if online:
            identity = GeneIdentity(query=q, **{k: v for k, v in online.items() if k != "query"})
            if cache and identity.symbol:
                table[identity.symbol.upper()] = identity.to_dict()
                _save_alias_table(table)
            return identity

    local = _lookup_local(q, table)
    if local:
        data = {k: v for k, v in local.items() if k not in ("query", "resolved_by", "warnings")}
        return GeneIdentity(query=q, resolved_by="local", **data)

    # 完全查不到：保留原名，标记未解析。绝不编造 ID。
    return GeneIdentity(
        query=q,
        symbol=q.upper() if re.fullmatch(r"[A-Za-z0-9\-]+", q) else "",
        resolved_by="unresolved",
        warnings=[f"未能解析 '{q}' 的数据库 ID；在线解析未接入且本地别名表无记录"],
    )


def normalize_cancer_type(name: str) -> str:
    """把癌种写法归一化为 cancer_id（配置文件名）。

    只做最基本的规整；真正的癌种->队列映射在癌种配置里显式声明，
    这样新增癌种时不需要改这段代码。
    """
    key = name.strip().lower().replace(" ", "_").replace("-", "_")
    common = {
        "hepatocellular_carcinoma": "hcc",
        "liver_cancer": "hcc",
        "肝细胞癌": "hcc",
        "肝癌": "hcc",
        "lung_adenocarcinoma": "luad",
        "breast_cancer": "brca",
        "肺腺癌": "luad",
        "乳腺癌": "brca",
    }
    return common.get(key, key)


def normalize_tissue(name: str) -> str:
    """组织名归一化为 GTEx 风格（首字母大写下划线连接）。"""
    cleaned = re.sub(r"[\s\-]+", "_", name.strip())
    return "_".join(part.capitalize() for part in cleaned.split("_") if part)
