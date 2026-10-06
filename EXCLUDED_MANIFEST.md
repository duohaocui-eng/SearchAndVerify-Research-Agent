# Excluded manifest

The public snapshot intentionally excludes these canonical categories:

| Category | Reason |
|---|---|
| `career_context/`, task and resume files | private planning and career collaboration context |
| worklogs, todo, conversation exports, interview product folders | internal coordination and personal material |
| `outputs/`, raw traces, model-call logs, checkpoints | unnecessary raw artifacts; possible prompt/path leakage |
| `data/cache/`, downloaded full text, large historical datasets | generated/cache content, size and redistribution risk |
| historical evaluation/result trees | not needed to verify the three released claims |
| private Git history | public repository must have a fresh clean history |
| `.env`, credentials, local caches | secrets and machine-local state |

Only explicitly selected files listed in [`SOURCE_SNAPSHOT.json`](SOURCE_SNAPSHOT.json) are part of the release.
