# Public release audit

This repository is a curated clean snapshot, not a fork or mirror of the private canonical repository.

## Included

- core `src/` implementation;
- public source/policy configuration;
- two small synthetic/deterministic fixture files;
- 111 selected offline tests;
- three frozen evidence packages with cases, protocols, row-level results, and aggregate metrics;
- recruiter-oriented architecture, evaluation, and limitation pages.

## Excluded by design

- private Git history and canonical collaboration metadata;
- career context, task files, worklogs, interview materials, and internal notes;
- raw model traces, call dumps, checkpoints, caches, generated outputs, and downloaded full text;
- historical exploration directories and superseded result trees;
- local environment files, credentials, machine paths, and editor/test caches;
- raw/reference datasets not required for the three released claims.

## Scan coverage

Before publication, the curated file set was scanned for common API-key/token/password forms, authorization headers, private-network addresses, local absolute paths, `.env` files, and generated/cache patterns. Public URLs and empty environment-variable names were retained. A second scan was performed after assembling the snapshot.

The package was then validated from the staging root by running the offline demo, all included tests, claim recomputation, `evals/verify_links.py`, file-size checks, and a fresh-history check.

No `LICENSE` is included because the canonical project did not contain an explicit licensing decision. Public visibility is not itself a grant of reuse rights.
