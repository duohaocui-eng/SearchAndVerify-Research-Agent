"""evidence_core：确定性证据层。

本层的不变量（invariants），改动时请务必保持：
1. 不 import agent 包 —— 依赖方向永远是 agent -> evidence_core，不可反向。
2. 不调用 LLM —— 相同输入必须得到相同输出。
3. 不做决策 —— 「查不查、查哪个」是 agent 的事，这里只负责「查到什么、值多少分」。

破坏其中任何一条，固定工作流基线与消融实验就失去意义。
"""
from .config import (
    PROJECT_ROOT,
    load_agent_config,
    load_cancer_config,
    load_source_registry,
    load_yaml,
)
from .contract_writer import build_row, write_contract
from .evidence_store import Conflict, EvidenceStore, coverage_ratio, missing_dimensions
from .http import SourceUnavailable
from .normalize import GeneIdentity, normalize_cancer_type, normalize_gene
from .registry import SourceRegistry, SourceSpec
from .schema import (
    CONTRACT_FIELDS,
    DIMENSIONS,
    REQUIRED_DIMENSIONS,
    EvidenceItem,
    Observation,
)
from .scorer import score_observation

__all__ = [
    "CONTRACT_FIELDS",
    "DIMENSIONS",
    "PROJECT_ROOT",
    "REQUIRED_DIMENSIONS",
    "Conflict",
    "EvidenceItem",
    "EvidenceStore",
    "GeneIdentity",
    "Observation",
    "SourceRegistry",
    "SourceSpec",
    "SourceUnavailable",
    "build_row",
    "coverage_ratio",
    "load_agent_config",
    "load_cancer_config",
    "load_source_registry",
    "load_yaml",
    "missing_dimensions",
    "normalize_cancer_type",
    "normalize_gene",
    "score_observation",
    "write_contract",
]
