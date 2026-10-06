# Evaluation

Run `python evals/verify_claims.py` to recompute the headline totals directly from the released row-level CSV files. The verifier performs no network or model calls.

## 1. Formal / Research authority isolation

Thirty frozen state-machine scenarios compare the real isolated controller with an evaluation-only permissive adapter.

| Arm | Formal contamination | False resolution | Correct escalation |
|---|---:|---:|---:|
| isolated controller | 0/30 | 0/30 | 30/30 |
| permissive ablation | 25/30 | 25/30 | 5/30 |

All 30 illegal formal-write attempts in the isolated arm were rejected. This demonstrates the engineering permission boundary; it does not estimate real-user or medical error rates.

Evidence: [protocol](../evidence/authority_isolation/PROTOCOL.md) · [frozen cases](../evidence/authority_isolation/CASES.json) · [row-level results](../evidence/authority_isolation/RESULTS.csv) · [metrics](../evidence/authority_isolation/METRICS.json)

## 2. OA full-text passage localization

The frozen set contains 38 within-document cases: 18 fulltext-primary, 10 abstract-sufficient, 6 no-support controls, and 4 fulltext-unavailable controls. Among the 28 pre-labeled positive passages:

| Profile | Gold passage localized |
|---|---:|
| abstract only | 10/28 (35.7%) |
| OA full text | 28/28 (100%) |

OA full text produced lexical candidates in two no-support controls. They were retained as negative outcomes and were not admitted as formal evidence. This evaluation is conditional on an already-bound public OA source; it does not measure open-search recall.

Evidence: [protocol](../evidence/oa_fulltext/PROTOCOL.md) · [frozen cases](../evidence/oa_fulltext/CASES.json) · [row-level results](../evidence/oa_fulltext/RESULTS.csv) · [metrics](../evidence/oa_fulltext/METRICS.json)

## 3. Independent strategy holdout

Fifteen new public-OA targets were frozen before tested-model output. Stepwise and compact each ran twice per target, for 60 fresh logical runs.

| Metric | Stepwise | Compact |
|---|---:|---:|
| completed | 29/30 | 29/30 |
| mechanical quote/hash audit | 30/30 | 30/30 |
| gold record localized | 26/30 | 27/30 |
| record Jaccard across repeats | 0.867 | 0.933 |
| passage Jaccard across repeats | 0.493 | 0.658 |

Paired gold outcomes were one stepwise win, two compact wins, and 27 ties. Compact remains a candidate, not the default: the net gold difference is one, it had more failed observations, and blinded human semantic review is still pending.

Evidence: [protocol](../evidence/strategy_holdout/PROTOCOL.md) · [frozen cases](../evidence/strategy_holdout/CASES.json) · [arm definitions](../evidence/strategy_holdout/ARMS.json) · [60 runs](../evidence/strategy_holdout/RUNS.csv) · [metrics](../evidence/strategy_holdout/METRICS.json) · [stability](../evidence/strategy_holdout/STABILITY.json) · [source provenance](../evidence/strategy_holdout/SOURCE_PROVENANCE.json)

## Software validation

The curated public package carries 111 offline tests covering evidence invariants, authority gates, checkpoint binding, fixture-only end-to-end behavior, OA document identity, quote positions, recall, and budget/failure behavior. The larger private engineering suite is not copied into this showcase; this repository claims only the tests that are present and runnable here.
