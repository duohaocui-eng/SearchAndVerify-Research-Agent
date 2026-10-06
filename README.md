# Search-and-Verify Research Agent

A research-agent prototype that turns an open-ended biomedical target question into traceable evidence: it searches public sources, reads open-access full text, locates supporting passages, and keeps exploratory research observations outside the formal evidence state until they pass an explicit authority gate.

The system has two cooperating loops:

- **Evidence Loop** — deterministic source adapters, evidence normalization, verification, scoring, conflict handling, and bounded candidate decisions.
- **Open Research Loop** — search → read → locate → recall over public literature, with quotes, sections, offsets, document hashes, and a strict advisory-only boundary.

## Three claims, with inspectable evidence

| Engineering claim | Released result | Inspect it |
|---|---:|---|
| Formal / Research authority isolation | isolated controller: **0** Formal State contamination and **30/30** correct escalations; evaluation-only permissive ablation: 25 contaminations / false resolutions | [summary](docs/evaluation.md#1-formal--research-authority-isolation) · [frozen cases](evidence/authority_isolation/CASES.json) · [row-level results](evidence/authority_isolation/RESULTS.csv) |
| OA full-text passage localization | across 28 pre-labeled positive passages: abstract-only **10/28**, OA full text **28/28** | [summary](docs/evaluation.md#2-oa-full-text-passage-localization) · [frozen cases](evidence/oa_fulltext/CASES.json) · [row-level results](evidence/oa_fulltext/RESULTS.csv) |
| Independent strategy holdout | 15 targets / 60 runs; stepwise **26/30** gold vs compact **27/30**, both 29/30 completed and 30/30 mechanically audited | [summary](docs/evaluation.md#3-independent-strategy-holdout) · [protocol](evidence/strategy_holdout/PROTOCOL.md) · [runs](evidence/strategy_holdout/RUNS.csv) |

The holdout result does **not** replace the product default: stepwise remains the default because the net difference is one gold record, compact produced more trace errors, and no human semantic labels are available. This is an engineering showcase, not a medical-accuracy claim.

## Quick start (offline, no API key)

Requires Python 3.10+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[dev]"
python examples/offline_demo.py
python -m pytest -q
python evals/verify_claims.py
python evals/verify_links.py
```

The demo and tests force fixture-only operation and make no network or model calls. Optional live LLM planning uses the three empty variables documented in [.env.example](.env.example); never commit real credentials.

## Repository map

```text
src/                       core Evidence and Open Research loops
configs/                   source capabilities and bounded policies
data/fixtures/             small deterministic offline fixtures
tests/                     111 focused offline regression tests
examples/offline_demo.py   no-key end-to-end evidence-loop demo
evals/verify_claims.py     recomputes the three public claim summaries
evidence/                  frozen cases, row-level results, metrics, protocols
docs/                      architecture, evaluation, limitations, release audit
```

Start with [architecture](docs/architecture.md), then follow each claim through [evaluation](docs/evaluation.md) to its frozen inputs and row-level outputs. The evidence package is a curated snapshot mapped to canonical source commit `b548fd4f7fa924396a69217bb822c72af3c75890` in [SOURCE_SNAPSHOT.json](SOURCE_SNAPSHOT.json).

## Boundaries

- Passage traceability is not medical truth or clinical validity.
- The 38-case OA evaluation measures localization **after a public OA source is bound**; it is not open-corpus recall.
- Mechanical quote/hash audits do not substitute for expert semantic review.
- Holdout cost and time are observational because arms shared caches and ran in a fixed order.
- No production SLA, security certification, or clinical-use claim is made.

See [limitations](docs/limitations.md) for the full scope. This snapshot intentionally contains no private development history, internal worklogs, raw model traces, downloaded full text, credentials, or generated caches. No license is asserted in this release; source availability should not be interpreted as a grant of reuse rights.
