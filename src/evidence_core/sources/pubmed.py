"""PubMed / Europe PMC：功能性证据（内吞、配体、脱落、生物分布、内体逃逸）。

这是唯一「非结构化」的源，也是 Verifier 存在的主要原因：
数据库返回的是数值，对不对是数据本身的问题；
文献返回的是 claim，模型很容易把「某文献提到 X」写成「X 已被证实」。
所以本源返回的每条 Observation 都必须带 source_ref（PMID/DOI）和可核对的原文片段，
交给 agent/verifier.py 判断「这条引用是否真的支持这个 claim」。

claim 抽取策略（LLM 未接入前的确定性替代方案）：
不用 LLM 做摘要或改写，而是直接从摘要里挑出"看起来最相关"的一整句作为
claim，同时把这句话原样也放进 quote。claim 与 quote 逐字相同，
Verifier 的词面重合检查必然通过，不会因为规则层判断不了语义蕴含而卡在
needs_more（这类 claim 的可信度上限就是"这句话确实来自这篇摘要"，
不承诺更多，也不少说）。LLM 接入后应替换成真正的摘要+蕴含判断，
详见 docs/03 第 5 节与 09 llm/prompts.py 的 GUARDRAIL。
"""
from __future__ import annotations

import re
from typing import Any

from ..http import SourceUnavailable, build_url, request_json
from ..schema import Observation
from .base import DataSource, FetchRequest

EUROPEPMC_BASE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest"

# 各维度用于挑句子的关键词（英文，因为摘要绝大多数是英文）。
_DIMENSION_KEYWORDS: dict[str, list[str]] = {
    "internalization": ["internaliz", "endocytos", "uptake", "intracellular delivery"],
    "ligand_availability": ["ligand", "aptamer", "binding affinity", "kd ", "peptide"],
    "clinical_precedent": ["phase i", "phase ii", "phase iii", "clinical trial",
                            "approved", "nct0"],
    "delivery_precedent": ["adc", "antibody-drug conjugate", "galnac", "car-t",
                           "ligand conjugate", "payload delivery", "siRNA conjugate"],
    "shedding": ["shed", "soluble", "ectodomain", "decoy"],
    "biodistribution": ["biodistribution", "tumor uptake", "tumor accumulation",
                         "in vivo", "pharmacokinetic"],
    "endosomal_escape": ["endosomal escape", "cytosolic delivery", "lysosomal degradation"],
}

# 内吞证据强度分类关键词，越靠前优先级越高。必须与
# scoring_maps.INTERNALIZATION_EVIDENCE_SCORES 的键保持一致。
_INTERNALIZATION_KIND_KEYWORDS: list[tuple[str, list[str]]] = [
    ("quantitative_uptake", ["flow cytometry", "quantitative uptake", "internalization assay",
                              "confocal microscopy quantif", "radiolabel"]),
    ("adc_validated", ["antibody-drug conjugate", "adc ", "payload delivery"]),
    ("imaging", ["imaging", "pet imaging", "spect", "fluorescence imaging", "confocal"]),
    ("receptor_class_inferred", ["receptor-mediated endocytosis", "clathrin", "endocytosis"]),
]


def _split_sentences(text: str) -> list[str]:
    """粗粒度断句：够用即可，不需要真正的 NLP 分句器。"""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def _pick_sentence(sentences: list[str], gene: str, dimension: str) -> str | None:
    """按"含基因名 +2 分、每命中一个维度关键词 +1 分"挑分数最高的句子。"""
    keywords = _DIMENSION_KEYWORDS.get(dimension, [])
    best, best_score = None, 0
    for s in sentences:
        low = s.lower()
        score = (2 if gene and gene.lower() in low else 0)
        score += sum(1 for k in keywords if k in low)
        if score > best_score:
            best, best_score = s, score
    return best


def _classify_internalization_kind(text: str) -> str:
    low = text.lower()
    for kind, keywords in _INTERNALIZATION_KIND_KEYWORDS:
        if any(k in low for k in keywords):
            return kind
    return "unknown"

# 各维度的检索词模板。癌种词由癌种配置 sources.literature.query_terms 注入。
QUERY_TEMPLATES: dict[str, list[str]] = {
    "internalization": [
        "{gene} receptor-mediated endocytosis",
        "{gene} internalization antibody drug conjugate",
        "{gene} uptake {cancer}",
    ],
    "ligand_availability": [
        "{gene} ligand binding affinity",
        "{gene} aptamer OR peptide ligand",
    ],
    "clinical_precedent": [
        "{gene} targeted therapy clinical trial {cancer}",
        "{gene} CAR-T OR ADC OR conjugate clinical",
    ],
    "delivery_precedent": [
        "{gene} antibody-drug conjugate OR ADC OR GalNAc OR CAR-T",
        "{gene} ligand conjugate payload delivery {cancer}",
    ],
    "shedding": [
        "{gene} ectodomain shedding soluble receptor",
    ],
    "biodistribution": [
        "{gene} targeted delivery biodistribution in vivo",
    ],
    "endosomal_escape": [
        "{gene} endosomal escape cytosolic delivery",
    ],
}


class PubmedSource(DataSource):
    source_id = "pubmed"

    def _queries(self, request: FetchRequest) -> list[str]:
        gene = request.identity.symbol or request.identity.query
        terms = list((request.cancer_config.get("sources") or {})
                     .get("literature", {}).get("query_terms") or [])
        cancer = terms[0] if terms else request.cancer_config.get("cancer_type_short", "")
        templates = QUERY_TEMPLATES.get(request.dimension, ["{gene} {cancer}"])
        # Agent 可以通过 hints 注入改写后的查询（对应 Q12「改写查询」动作）
        rewritten = request.hints.get("rewritten_queries")
        # 所有 OR 主题（包括模型改写）必须置于靶点 AND 的右侧。
        names = list(dict.fromkeys([gene, *request.identity.aliases]))
        entity = " OR ".join('"' + name.replace('"', '').strip() + '"'
                             for name in names if name.strip())
        topics = list(rewritten) if rewritten else [
            t.format(gene="", cancer=cancer).strip() for t in templates
        ]
        return [f"({entity}) AND ({topic})" for topic in topics]

    def fetch(self, request: FetchRequest) -> list[Observation]:
        self.require_configured()

        gene = request.identity.symbol or request.identity.query
        seen_pmids: set[str] = set()
        observations: list[Observation] = []
        for query in self._queries(request):
            params: dict[str, Any] = {"db": "pubmed", "retmode": "json", "retmax": 5, "term": query}
            api_key = self.api_key()
            if api_key:
                params["api_key"] = api_key   # 提升限速：3 req/s -> 10 req/s
            url = build_url(self.spec.base_url, "esearch.fcgi", params)
            try:
                data = request_json(url, source_id=self.source_id)
            except SourceUnavailable:
                continue
            ids: list[str] = ((data or {}).get("esearchresult") or {}).get("idlist") or []
            for pmid in ids:
                if pmid in seen_pmids:
                    continue
                seen_pmids.add(pmid)
                summary = self._summarize(pmid, gene=gene, dimension=request.dimension)
                if not summary:
                    continue
                context = {
                    "pmid": pmid,
                    "query": query,
                    "title": summary.get("title", ""),
                    # Verifier 用它核对 claim 是否真被引用支持
                    "quote": summary.get("quote", ""),
                    "extraction_method": "literal_sentence_no_llm",
                }
                if request.dimension == "internalization":
                    context["evidence_kind"] = _classify_internalization_kind(
                        summary.get("title", "") + " " + summary.get("quote", "")
                    )
                observations.append(self.make_observation(
                    request,
                    value=None,
                    unit="claim",
                    claim=summary["claim"],
                    source_ref=f"PMID {pmid}",
                    dataset="pubmed",
                    context=context,
                ))

        if not observations:
            raise SourceUnavailable(self.source_id, f"文献检索未命中 {request.dimension} 相关结果")
        return observations

    def _summarize(self, pmid: str, *, gene: str, dimension: str) -> dict[str, Any] | None:
        """取标题+摘要，挑一句最相关的话同时作 claim 与 quote。

        标题/摘要走 Europe PMC（免鉴权、无需 XML 解析，比 NCBI efetch 简单），
        检索本身仍走 NCBI esearch（配置 NCBI_API_KEY 后限速更高，也更贴合
        "PubMed 检索词模板"这个设计）。两个服务对同一批 PMID 通用。
        """
        try:
            url = build_url(EUROPEPMC_BASE_URL, "search", {
                "query": f"EXT_ID:{pmid} AND SRC:MED",
                "resultType": "core",
                "format": "json",
            })
            data = request_json(url, source_id="pubmed")
        except SourceUnavailable:
            return None

        results = ((data or {}).get("resultList") or {}).get("result") or []
        if not results:
            return None
        result = results[0]
        title = str(result.get("title") or "")
        abstract = str(result.get("abstractText") or "")

        sentences = _split_sentences(abstract) or _split_sentences(title)
        picked = _pick_sentence(sentences, gene, dimension) or (sentences[0] if sentences else title)
        if not picked:
            return None
        return {"claim": picked, "quote": picked, "title": title}
