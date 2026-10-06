"""证据库：登记、去重、按维度检索、独立来源判定、冲突检测。

三个不显眼但关键的能力：

1) 独立来源判定（Q49）
   cBioPortal 和 GDC 都能给 TCGA 表达值，但它们是同一份原始数据的两种门户。
   如果把它们算成「两个独立来源互相印证」，就是虚假的多源支持。
   这里用 independence_group 做判定，同组只算一票。

2) 内容级去重
   同一条证据被反复抓取（重试、换路径）不应重复计票。
   用 (target, dimension, source_dataset, value, claim) 的哈希做幂等。

3) 冲突检测（Q14）
   同维度不同来源分差过大、或 RNA 层与蛋白层结论相反，都要显式记为 conflict，
   而不是悄悄取平均——取平均会把「我们其实不知道」变成一个看起来确定的数字。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .schema import DIMENSIONS, EvidenceItem, Observation


@dataclass
class Conflict:
    target: str
    dimension: str
    kind: str                       # value_spread / layer_disagreement
    evidence_ids: list[str]
    detail: str
    resolved: bool = False
    resolution: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "dimension": self.dimension,
            "kind": self.kind,
            "evidence_ids": self.evidence_ids,
            "detail": self.detail,
            "resolved": self.resolved,
            "resolution": self.resolution,
        }


class EvidenceStore:
    """所有证据的唯一入口。Agent 不允许绕过它自己记数值。"""

    def __init__(self, conflict_tolerance: float = 2.0) -> None:
        self._items: dict[str, EvidenceItem] = {}
        self._hashes: set[str] = set()
        self._counter = 0
        self.conflict_tolerance = conflict_tolerance
        self.conflicts: list[Conflict] = []

    # ---------------- 写入 ----------------

    @staticmethod
    def _content_hash(obs: Observation) -> str:
        raw = f"{obs.target}|{obs.dimension}|{obs.source_dataset}|{obs.value}|{obs.claim}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def add(self, obs: Observation, score: float | None) -> EvidenceItem | None:
        """登记一条证据。重复内容返回 None（幂等）。

        score 必须由 scoring_maps 算好后传入——本类不自己打分，
        以保证「打分逻辑只有一处」这个不变量。
        """
        h = self._content_hash(obs)
        if h in self._hashes:
            return None
        self._hashes.add(h)
        self._counter += 1
        item = EvidenceItem(
            evidence_id=f"ev_{self._counter:04d}",
            target=obs.target,
            dimension=obs.dimension,
            value=obs.value,
            unit=obs.unit,
            score=score,
            claim=obs.claim,
            source_id=obs.source_id,
            source_dataset=obs.source_dataset,
            source_ref=obs.source_ref,
            layer=obs.layer,
            reliability=obs.reliability,
            independence_group=obs.independence_group or obs.source_id,
            retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            context=dict(obs.context),
        )
        self._items[item.evidence_id] = item
        self._detect_conflicts(item)
        return item

    # ---------------- 读取 ----------------

    def get(self, evidence_id: str) -> EvidenceItem | None:
        return self._items.get(evidence_id)

    def all(self) -> list[EvidenceItem]:
        return list(self._items.values())

    def for_target(self, target: str, dimension: str | None = None,
                   verified_only: bool = False) -> list[EvidenceItem]:
        out = [i for i in self._items.values() if i.target == target]
        if dimension:
            out = [i for i in out if i.dimension == dimension]
        if verified_only:
            out = [i for i in out if i.verification == "verified"]
        return out

    def dimensions_covered(self, target: str, verified_only: bool = False) -> set[str]:
        return {i.dimension for i in self.for_target(target, verified_only=verified_only)}

    def independent_groups(self, target: str, dimension: str | None = None) -> set[str]:
        """真正独立的来源分组数——同一原始数据的多个门户只算一个。"""
        items = self.for_target(target, dimension)
        return {i.independence_group for i in items if i.verification != "rejected"}

    def has_independent_support(self, target: str, dimension: str, minimum: int = 2) -> bool:
        return len(self.independent_groups(target, dimension)) >= minimum

    # ---------------- 冲突 ----------------

    @staticmethod
    def _same_source_different_tissue(a: EvidenceItem, b: EvidenceItem) -> bool:
        """判断两条证据是否只是「同一个源对不同组织的多次合法取值」。

        offtarget_expression / safety 这两个维度会对同一个源（如 GTEx）里的
        多个组织各出一条 Observation（脱靶扫描、关键器官逐个查）。
        它们分数不同是预期行为（不同组织表达本来就不同），不是「同一个问题
        两个源给出矛盾答案」，不该被记成 value_spread 冲突——真正需要仲裁的
        冲突是跨源（不同 independence_group）的分歧。
        """
        if a.independence_group != b.independence_group:
            return False
        tissue_a, tissue_b = a.context.get("tissue"), b.context.get("tissue")
        return bool(tissue_a and tissue_b and tissue_a != tissue_b)

    def _detect_conflicts(self, new_item: EvidenceItem) -> None:
        peers = [
            i for i in self.for_target(new_item.target, new_item.dimension)
            if i.evidence_id != new_item.evidence_id and i.score is not None
            and not self._same_source_different_tissue(i, new_item)
        ]
        if not peers or new_item.score is None:
            return

        # 数值分歧
        for peer in peers:
            if abs((peer.score or 0) - new_item.score) > self.conflict_tolerance:
                self._record_conflict(Conflict(
                    target=new_item.target,
                    dimension=new_item.dimension,
                    kind="value_spread",
                    evidence_ids=sorted([peer.evidence_id, new_item.evidence_id]),
                    detail=(
                        f"{peer.source_id}({peer.score}) 与 "
                        f"{new_item.source_id}({new_item.score}) 分差超过阈值 "
                        f"{self.conflict_tolerance}"
                    ),
                ))

        # 层级分歧：RNA 说高、蛋白说低（或反之）——这是递送靶点最常见的坑
        for peer in peers:
            if peer.layer == new_item.layer or not peer.layer or not new_item.layer:
                continue
            if {peer.layer, new_item.layer} != {"rna", "protein"}:
                continue
            high, low = sorted([peer, new_item], key=lambda i: i.score or 0, reverse=True)
            if (high.score or 0) >= 6.5 and (low.score or 0) <= 3.5:
                self._record_conflict(Conflict(
                    target=new_item.target,
                    dimension=new_item.dimension,
                    kind="layer_disagreement",
                    evidence_ids=sorted([peer.evidence_id, new_item.evidence_id]),
                    detail=(
                        f"{high.layer} 层 {high.source_id} 给 {high.score}，"
                        f"{low.layer} 层 {low.source_id} 给 {low.score}；"
                        f"递送靶点以蛋白层为准，需补蛋白证据"
                    ),
                ))

    def _record_conflict(self, conflict: Conflict) -> None:
        # 去重键必须含 kind：同一对证据可能同时构成「数值分歧」和「层级分歧」，
        # 两者的处理方式不同（前者补第三方仲裁，后者以蛋白层为准），不能合并。
        for existing in self.conflicts:
            if (existing.target == conflict.target
                    and existing.dimension == conflict.dimension
                    and existing.kind == conflict.kind
                    and existing.evidence_ids == conflict.evidence_ids):
                return
        self.conflicts.append(conflict)

    def open_conflicts(self, target: str | None = None) -> list[Conflict]:
        out = [c for c in self.conflicts if not c.resolved]
        return [c for c in out if c.target == target] if target else out

    # ---------------- 持久化 ----------------

    def write_jsonl(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as fh:
            for item in self._items.values():
                fh.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")

    @classmethod
    def read_jsonl(cls, path: str | Path) -> "EvidenceStore":
        store = cls()
        p = Path(path)
        if not p.exists():
            return store
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            item = EvidenceItem(**data)
            store._items[item.evidence_id] = item
            store._counter = max(store._counter, int(item.evidence_id.split("_")[1]))
        return store

    # ---------------- 统计 ----------------

    def summary(self) -> dict[str, Any]:
        items = self.all()
        return {
            "total": len(items),
            "verified": sum(1 for i in items if i.verification == "verified"),
            "rejected": sum(1 for i in items if i.verification == "rejected"),
            "unverified": sum(1 for i in items if i.verification == "unverified"),
            "targets": len({i.target for i in items}),
            "dimensions": len({i.dimension for i in items}),
            "open_conflicts": len(self.open_conflicts()),
        }


def coverage_ratio(store: EvidenceStore, target: str, required: Iterable[str],
                   verified_only: bool = True) -> float:
    """必需维度的覆盖率，用于判断「这个候选查够了没有」。"""
    required = list(required)
    if not required:
        return 1.0
    covered = store.dimensions_covered(target, verified_only=verified_only)
    return sum(1 for d in required if d in covered) / len(required)


def missing_dimensions(store: EvidenceStore, target: str, required: Iterable[str],
                       verified_only: bool = True) -> list[str]:
    covered = store.dimensions_covered(target, verified_only=verified_only)
    return [d for d in required if d not in covered]


def dimension_label(dimension: str) -> str:
    return DIMENSIONS.get(dimension, {}).get("label", dimension)
