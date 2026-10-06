# Track A protocol｜OA Full Text design-value

## Question

在来源文档已经绑定的条件下，`abstract-only` 与 `OA full text + locate` 对预先冻结的 gold passage 定位能力有何差异？

## Scope and primary metrics

- 这是 **within-document evidence localization**，不测全库搜索 recall，不测医学真值。
- Gold pointer 来自本机已缓存的公开 Europe PMC OA XML，绑定 PMCID、document SHA-256、section、passage ID 与不超过 20 词的核查片段。
- 正例主指标：gold passage 是否进入 BM25 top-6。
- no-support / unavailable 主指标：是否保持 unknown，且不接纳 retrieval score 为 0 的段落。
- 配对比较固定为 `abstract_only` 与 `oa_fulltext`；两臂使用同一 query、同一文档绑定和同一 ranker。

## Freeze and independence

case 由固定 method-term screen 生成，每篇正文最多两个 case；选择阶段不调用 ranker。`FROZEN_MANIFEST.json` 在首次 evaluation 前记录 case 文件与所有 XML hash。被测 Agent 的历史答案不作为 gold。

## Boundaries

结果只支持“在这些冻结、已知来源的文档内定位任务上，正文能否补到摘要不可见段落”。它不能支持搜索覆盖、语义/临床正确率或全库 recall。
