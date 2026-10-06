# C3-20260930-003｜Independent holdout protocol

Frozen before the first tested-model call. This holdout contains 15 public-OA positive cases on 15 targets absent from the GPC3/ASGR1 development history. Case selection used public Europe PMC metadata/full text only and did not inspect stepwise or compact outputs.

- Arms: stepwise and compact only; two fresh repeats per case; 60 logical runs.
- Primary quality: predeclared gold-record localization, terminal status, mechanical provenance audit.
- Stability: record/passage Jaccard, same gold outcome, same completion.
- Efficiency: calls, input/output/cache-hit tokens, conservative CNY upper bound, wall time.
- Failures: all rejected actions, source/full-text errors, invalid responses, quote rejections, unavailable and budget stops remain in the denominator.
- Gold PMID/PMCID/title and evidence locators are scoring metadata only; none enter tested-model prompts.
- The holdout is evaluation-only. No behavior tuning or result-driven case/gold changes are allowed on this set.
- Gold localization and quote audit are not medical truth, open-search recall, or independent expert review.
- Blinded packets hide stepwise/compact identity behind per-case A/B labels and leave semantic labels for a future human reviewer.

Decision rule: report paired evidence without requiring a predetermined winner. stable_v4 is not modified by this task.
