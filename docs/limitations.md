# Limitations

This repository demonstrates engineering mechanisms and traceability. It does not establish medical validity.

- **Not medical truth.** Gold-record or passage localization is not a clinical conclusion, expert judgment, or proof that a claim is medically sufficient.
- **Not open-corpus recall.** The OA experiment starts from bound public records, and the holdout was selected for usable public OA evidence.
- **No human semantic labels.** Quote/hash audits establish mechanical provenance only. The 15 holdout review packets have not been independently adjudicated by domain experts.
- **Small, selected evaluations.** Thirty authority scenarios, 38 document cases, and 15 holdout targets support narrow engineering statements, not population estimates.
- **Observational cost/time.** Strategy arms shared caches and ran in a fixed order, so measured latency and cost are not strict same-quality causal estimates.
- **Known failures are retained.** Two holdout runs were unavailable; compact had more failed observations; two no-support OA controls produced lexical candidates.
- **Prototype operations.** There is no production SLA, multi-tenant security review, clinical deployment validation, or regulated-use claim.
- **Optional external dependencies.** Live search depends on public services and optional LLM planning requires user-supplied credentials. Offline tests and the demo require neither.

The stable product default remains **stepwise**. The compact strategy is an evidence-backed candidate for future evaluation, not a promoted default.
