# Architecture

## Evidence Loop

The Evidence Loop is the formal, bounded path:

1. source adapters return typed observations with provenance;
2. normalizers preserve target identity, units, layers, and source independence;
3. a verifier rejects missing or unsupported citations;
4. an evidence store de-duplicates records and records conflicts;
5. deterministic scoring and policy produce candidate status and a traceable contract.

Missing evidence stays missing. A failed source call or an unverified narrative claim cannot silently become a neutral score or a successful finding. Risk dimensions use conservative merge rules, and evidence from mirrors of the same underlying dataset does not count as independent confirmation.

Key entry points:

- [`src/agent/loop.py`](../src/agent/loop.py) — bounded Search-and-Verify control loop
- [`src/evidence_core/evidence_store.py`](../src/evidence_core/evidence_store.py) — evidence admission, de-duplication, conflicts
- [`src/evidence_core/scorer.py`](../src/evidence_core/scorer.py) — deterministic scoring
- [`src/agent/verifier.py`](../src/agent/verifier.py) — citation and claim verification boundary

## Open Research Loop

The Open Research Loop handles questions that structured sources alone cannot answer. It searches public literature, discovers records, reads OA full text, locates literal passages, and can recall previously omitted material. Each released finding binds to record identity plus passage/section/offset and document hash.

The loop is intentionally lower-authority: its observations are advisory until a formal capability verifies and admits them. It cannot directly change scores, candidate status, or the formal evidence store.

Key entry points:

- [`src/literature_agent/agent.py`](../src/literature_agent/agent.py) — nested research planning and source binding
- [`src/research_v4/source.py`](../src/research_v4/source.py) — OA document identity, parsing, passage location
- [`src/research_v4/agent.py`](../src/research_v4/agent.py) — search/read/locate/recall orchestration
- [`src/research_product/runtime.py`](../src/research_product/runtime.py) — advisory-to-formal capability boundary

## Authority boundary

```text
public sources -> Open Research observation (advisory)
                           |
                           v explicit escalation + verification
                 Formal EvidenceStore -> scorer -> candidate state
```

The permissive arm in the public ablation is evaluation-only. It deliberately removes this gate to demonstrate the failure mode; it is not a selectable production configuration.

## Reproducibility choices

- fixture-only tests are the default and disable live source access;
- checkpoints bind to configuration and state identifiers;
- literal quote and document-identity checks are separate from semantic correctness;
- budgets bound source/model operations and preserve failures in the trace;
- the public evidence package retains negative results and unavailable runs.
