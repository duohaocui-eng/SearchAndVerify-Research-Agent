"""Verifier：判断一条引用是否真的支持它所声称的 claim（对应 Q48）。

为什么必须独立成一层：
取证和验证如果由同一个环节完成，模型会倾向于「自己检查自己」，
把检索到的任何东西都解释成支持结论。分开之后，Verifier 的唯一职责是找茬，
拒绝掉的证据会触发重新搜索，形成 Search-and-Verify 的闭环。

三种模式：
- rule  : 确定性规则检查（默认）。不需要 LLM，可作为基线，也保证离线可跑。
- llm   : 用 LLM 判断 claim 与原文引用的蕴含关系。未配置 LLM_API_KEY 等
          环境变量时自动降级为 rule，行为在 note 里说明，不会报错。
- hybrid: 先检查出处和引用，叙述性证据（含规则不确定）再交给 LLM 复核。

未通过的证据不会被删除，而是标记为 rejected 并保留在 evidence.jsonl 里——
「我们查到过但判定它不成立」本身就是重要的可回放信息。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from evidence_core.http import DeadlineExceeded, check_deadline

from evidence_core.evidence_store import EvidenceStore
from evidence_core.schema import EvidenceItem

# 叙述性来源：返回的是自然语言 claim，必须核对原文，否则容易把「文献提到」写成「已证实」。
# 其余来源（数据库、登记库）返回机器可读字段，数值本身即证据。
# 按来源而非按 layer 判断：clinicaltrials 虽是 clinical 层，但返回的是结构化的临床阶段。
NARRATIVE_SOURCES = {"pubmed", "literature", "europepmc"}


@dataclass
class VerificationResult:
    evidence_id: str
    verdict: str            # verified / rejected / needs_more
    note: str
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"evidence_id": self.evidence_id, "verdict": self.verdict, "note": self.note, "cached": self.cached}


def _base_source(source_id: str) -> str:
    """fixtures:gtex -> gtex，让 fixture 证据走与真实源一致的验证路径。"""
    return source_id.split(":", 1)[-1] if ":" in source_id else source_id


class RuleVerifier:
    """确定性验证。检查的是「这条证据在形式上是否可被核对」。"""

    mode = "rule"

    def verify(self, item: EvidenceItem) -> VerificationResult:
        base = _base_source(item.source_id)

        # 1. 任何证据都必须有可回溯的出处
        if not item.source_ref:
            return VerificationResult(item.evidence_id, "rejected", "缺少 source_ref，无法回溯")

        # 2. 叙述性来源：必须带可核对的原文片段，且 claim 与原文有实质重合
        if base in NARRATIVE_SOURCES:
            quote = str(item.context.get("quote") or "")
            if not quote:
                return VerificationResult(
                    item.evidence_id, "rejected",
                    "文献 claim 缺少可核对的原文引用片段",
                )
            if _claim_supported_by_quote(item.claim, quote) != "supported":
                # 规则层只做词面检查，判断不了「中文 claim + 英文原文」这类跨语言蕴含。
                # 判成 rejected 会误杀真证据，判成 verified 则等于没检查——
                # 两者都不诚实。needs_more 会触发换源或 LLM 复核，
                # 同时如实记录「这条我们没能确认」，它不会被当作已验证结论使用。
                return VerificationResult(
                    item.evidence_id, "needs_more",
                    "规则层无法确认 claim 与原文的蕴含关系，需 LLM 复核或补充来源",
                )
            return VerificationResult(item.evidence_id, "verified", "claim 有原文片段支持")

        # 3. 结构化来源：必须有数值或可解析的定性字段，且能算出分数
        has_machine_field = (
            item.value is not None
            or item.context.get("location_text")
            or item.context.get("stage")
            or item.context.get("evidence_kind")
        )
        if not has_machine_field:
            return VerificationResult(
                item.evidence_id, "rejected",
                "结构化来源却没有数值或可解析的定性字段",
            )
        if item.score is None:
            return VerificationResult(
                item.evidence_id, "needs_more",
                "未能从该观测计算出分数，需换源或补充字段",
            )
        return VerificationResult(item.evidence_id, "verified", f"{base} 结构化证据，出处完整")


def _claim_supported_by_quote(claim: str, quote: str) -> str:
    """粗粒度蕴含检查。返回 supported / unknown。

    刻意只有「有正面词面证据」才判 supported，其余一律 unknown。
    规则层没有能力做语义蕴含（尤其是中文 claim 配英文原文），
    所以它不假装能做——「查不出重合」和「确实不支持」是两回事，
    把前者报成后者会误杀真证据，报成 supported 则等于没检查。
    真正的蕴含判断由 LLM 模式完成。
    """
    if not claim or not quote:
        return "unknown"
    q = quote.lower()
    tokens = [t.strip(".,;:()（）、，。").lower() for t in claim.split()]
    keywords = [t for t in tokens if len(t) >= 4 and t.isascii()]
    if not keywords:
        return "unknown"          # 纯中文 claim，规则层无从核对
    return "supported" if any(k in q for k in keywords) else "unknown"


_VALID_VERDICTS = {"verified", "rejected", "needs_more"}


class LLMVerifier:
    """用 LLM 判断 claim 与原文的蕴含关系。

    契约见 agent/llm/client.py 与 prompts.VERIFY_PROMPT：
    输入 claim + quote + source_ref + 靶点/维度/研究背景；输出 {verdict, note}。
    verdict 必须是 verified/rejected/needs_more 之一，否则和 LLM 调用失败一样，
    保留为 needs_more；每个输入最多两次调用（间隔至少 30 秒），避免故障重试风暴。
    结构化来源（数值/枚举字段）本身没有"蕴含关系"要判断，规则层已经算好了分，
    LLM 判断这类证据的边际价值很低，所以只把叙述性来源（PubMed 等）的
    claim/quote 交给 LLM 复核，结构化来源仍走规则层，省 token 也更稳妥。
    """

    mode = "llm"

    def __init__(self, client: Any | None = None) -> None:
        self.client = client
        self._fallback = RuleVerifier()
        self._cache: dict[str, tuple[str, str]] = {}
        self._failures: dict[str, tuple[int, float, str]] = {}

    def verify(self, item: EvidenceItem) -> VerificationResult:
        check_deadline()
        if self.client is None or not getattr(self.client, "is_configured", lambda: False)():
            result = self._fallback.verify(item)
            return VerificationResult(
                result.evidence_id, result.verdict,
                f"{result.note}（LLM 未接入，已降级为规则校验）",
            )

        base_source = _base_source(item.source_id)
        if base_source not in NARRATIVE_SOURCES:
            # 结构化证据没有"蕴含关系"要判断，交给规则层即可，注明这是主动跳过而非降级。
            result = self._fallback.verify(item)
            return VerificationResult(
                result.evidence_id, result.verdict,
                f"{result.note}（结构化证据，LLM 跳过，规则层已足够）",
            )

        quote = str(item.context.get("quote") or "")
        if not quote or not item.source_ref:
            return VerificationResult(item.evidence_id, "rejected", "叙述性证据缺少出处或可核对的原文引用")

        from .llm.prompts import VERIFY_PROMPT, render

        prompt = render(
            VERIFY_PROMPT,
            claim=item.claim, source_ref=item.source_ref, quote=quote,
            context_json=json.dumps({
                "target": item.target, "dimension": item.dimension,
                "source_id": item.source_id, "source_dataset": item.source_dataset,
                "value": item.value, "unit": item.unit,
                "context": item.context,
            }, ensure_ascii=False, sort_keys=True),
        )
        if prompt in self._cache:
            verdict, note = self._cache[prompt]
            return VerificationResult(item.evidence_id, verdict, note, cached=True)
        attempts, retry_at, failure_note = self._failures.get(prompt, (0, 0.0, ""))
        if attempts >= 2 or time.monotonic() < retry_at:
            return VerificationResult(item.evidence_id, "needs_more", failure_note, cached=True)
        try:
            raw = self.client.complete_json(prompt, purpose="verify")
            check_deadline()
            verdict = str((raw or {}).get("verdict") or "")
            note = str((raw or {}).get("note") or "")
            if verdict not in _VALID_VERDICTS:
                raise ValueError(f"LLM 返回了非法 verdict：{verdict!r}")
        except DeadlineExceeded:
            raise
        except Exception as exc:  # 调用失败不能把仅词面匹配的证据提升为已核证
            note = f"LLM 调用/解析失败，等待复核：{type(exc).__name__}: {exc}"
            self._failures[prompt] = (attempts + 1, time.monotonic() + 30.0, note)
            return VerificationResult(item.evidence_id, "needs_more", note)
        note = note or "LLM 判定，无附加说明"
        self._cache[prompt] = (verdict, note)
        self._failures.pop(prompt, None)
        return VerificationResult(item.evidence_id, verdict, note)


class HybridVerifier:
    """规则硬拒绝直接返回，叙述性证据的通过/不确定均交 LLM 复核。"""

    mode = "hybrid"

    def __init__(self, client: Any | None = None) -> None:
        self.rule = RuleVerifier()
        self.llm = LLMVerifier(client)

    def verify(self, item: EvidenceItem) -> VerificationResult:
        result = self.rule.verify(item)
        if result.verdict == "rejected" or _base_source(item.source_id) not in NARRATIVE_SOURCES:
            return result
        return self.llm.verify(item)


def build_verifier(agent_config: dict[str, Any], client: Any | None = None):
    mode = str((agent_config.get("verification") or {}).get("mode", "rule")).lower()
    if mode == "llm":
        return LLMVerifier(client)
    if mode == "hybrid":
        return HybridVerifier(client)
    return RuleVerifier()


def verification_enabled(agent_config: dict[str, Any]) -> bool:
    if (agent_config.get("ablation") or {}).get("disable_verifier"):
        return False
    return bool((agent_config.get("verification") or {}).get("enabled", True))


def verify_pending(
    store: EvidenceStore,
    verifier: Any,
    agent_config: dict[str, Any],
) -> list[VerificationResult]:
    """校验所有 unverified 证据。

    关掉验证时，所有证据直接标记 verified 并注明「未经验证」——
    这样消融实验里 evidence.jsonl 依然如实记录了「这批结论没被核对过」。
    """
    results: list[VerificationResult] = []
    enabled = verification_enabled(agent_config)

    for item in store.all():
        check_deadline()
        if item.verification != "unverified":
            continue
        if not enabled:
            item.verification = "verified"
            item.verifier_note = "验证已关闭（消融实验），未经核对"
            results.append(VerificationResult(item.evidence_id, "verified", item.verifier_note))
            continue
        result = verifier.verify(item)
        check_deadline()
        item.verification = "verified" if result.verdict == "verified" else (
            "rejected" if result.verdict == "rejected" else "unverified"
        )
        item.verifier_note = result.note
        results.append(result)

    return results


def rejected_dimensions(store: EvidenceStore, target: str) -> set[str]:
    """被拒证据覆盖的维度——这些维度需要重新搜索。"""
    return {i.dimension for i in store.for_target(target) if i.verification == "rejected"}
