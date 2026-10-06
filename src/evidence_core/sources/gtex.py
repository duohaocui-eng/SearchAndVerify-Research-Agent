"""GTEx：正常组织表达。提供 normal_reference_expression / offtarget_expression / safety。

去 HCC 化要点：
老模型写死 GTEX_TISSUES = ["Liver", "Whole_Blood", "Spleen"]，其中 Liver 是 HCC 的参照组织。
这里改成从癌种配置读：
  reference_normal_tissue  -> normal_reference_expression 维度
  offtarget_scan_tissues   -> offtarget_expression 维度（留空则扫全部）
  critical_risk_tissues    -> safety 维度
换癌种时这三个列表跟着癌种配置走，代码不动。
"""
from __future__ import annotations

from typing import Any

from ..http import SourceUnavailable, build_url, request_json
from ..schema import Observation
from .base import DataSource, FetchRequest

# GTEx v2 API 端点（接入时使用）
_ENDPOINT_MEDIAN = "expression/medianGeneExpression"
_ENDPOINT_GENE = "reference/gene"


class GtexSource(DataSource):
    source_id = "gtex"

    def _tissues_for(self, request: FetchRequest) -> list[str]:
        cfg = request.cancer_config
        if request.dimension == "normal_reference_expression":
            ref = cfg.get("reference_normal_tissue")
            return [ref] if ref else []
        if request.dimension == "safety":
            return list(cfg.get("critical_risk_tissues") or [])
        if request.dimension == "offtarget_expression":
            scan = list(cfg.get("offtarget_scan_tissues") or [])
            if scan:
                return scan
            # 留空 = 扫全部；接入后由 _all_tissues() 拉取组织清单
            return self._all_tissues()
        return []

    def _all_tissues(self) -> list[str]:
        """拉取 GTEx v8 全部组织清单（54 个 tissueSiteDetailId）。

        结果按 24 小时缓存（http.request_json 默认 7 天，这里单独缩短，
        因为组织清单几乎不变，用长缓存也无妨，但显式写出便于以后调整）。
        拉取失败时返回空列表——调用方会因此拿不到 offtarget 证据并走 fallback，
        而不是让整次运行崩掉。
        """
        try:
            url = build_url(self.spec.base_url, "dataset/tissueSiteDetail", {
                "datasetId": "gtex_v8", "page": 0, "itemsPerPage": 300,
            })
            data = request_json(url, source_id=self.source_id, cache_ttl=24 * 3600)
        except SourceUnavailable:
            return []
        rows: list[dict[str, Any]] = (data or {}).get("data") or []
        return [str(r["tissueSiteDetailId"]) for r in rows if r.get("tissueSiteDetailId")]

    def _resolve_gencode(self, symbol: str) -> str:
        """symbol -> GTEx 用的带版本号 gencodeId（如 ENSG00000147257.13）。

        GTEx v2 的 medianGeneExpression 端点要求精确匹配它自己参照集里的
        版本号，MyGene.info 给的无版本号 ensembl_id 直接传会查不到数据，
        所以必须走 GTEx 自己的 reference/gene 端点单独解析一次。
        """
        if not symbol:
            raise SourceUnavailable(self.source_id, "缺少基因 symbol，无法解析 gencodeId")
        url = build_url(self.spec.base_url, "reference/gene", {
            "geneId": symbol, "gencodeVersion": "v26", "genomeBuild": "GRCh38/hg38",
        })
        data = request_json(url, source_id=self.source_id)
        rows: list[dict[str, Any]] = (data or {}).get("data") or []
        if not rows:
            raise SourceUnavailable(self.source_id, f"GTEx reference/gene 未找到 {symbol}")
        return str(rows[0]["gencodeId"])

    def fetch(self, request: FetchRequest) -> list[Observation]:
        self.require_configured()

        tissues = self._tissues_for(request)
        if not tissues:
            raise SourceUnavailable(
                self.source_id,
                f"癌种配置未指定 {request.dimension} 对应的组织列表",
            )

        gencode = self._resolve_gencode(request.identity.symbol or request.identity.query)

        observations: list[Observation] = []
        skipped: list[str] = []
        for tissue in tissues:
            url = build_url(self.spec.base_url, _ENDPOINT_MEDIAN, {
                "gencodeId": gencode,
                "datasetId": "gtex_v8",
                "tissueSiteDetailId": tissue,
                "itemsPerPage": 10,
            })
            try:
                data = request_json(url, source_id=self.source_id)
            except SourceUnavailable:
                # 单个组织查询失败（如配置里的组织名在 GTEx v8 里不存在）不该拖垮
                # 整个维度：其余组织仍可正常出证据，这里只记一条跳过说明。
                skipped.append(tissue)
                continue
            rows: list[dict[str, Any]] = data.get("data", []) if isinstance(data, dict) else []
            if not rows:
                skipped.append(tissue)
                continue
            tpm = float(rows[0]["median"])
            observations.append(self.make_observation(
                request,
                value=tpm,
                unit="TPM",
                claim=f"GTEx v8 {request.identity.symbol} 在 {tissue} 的 median 表达为 {tpm:.2f} TPM",
                source_ref=f"GTEx v8 {gencode} {tissue}",
                dataset="gtex_v8",
                context={"tissue": tissue, "gencode_id": gencode,
                         "skipped_tissues": skipped} if skipped else {"tissue": tissue, "gencode_id": gencode},
            ))

        if not observations:
            raise SourceUnavailable(
                self.source_id,
                f"GTEx 未返回 {tissues} 的表达数据" + (f"（跳过：{skipped}）" if skipped else ""),
            )
        return observations
