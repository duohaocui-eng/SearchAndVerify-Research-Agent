# Track B protocol｜Formal / Open Research isolation ablation

## Question

当 observation 明确带有 `advisory_unverified; no evidence/score/status authority` 时，当前隔离控制器能否阻止其写入 Formal State，并把需要核证的情形正确升级到人工 verification？

## Arms

- `isolated`：实际 `MainResearchController`，正式状态 hash 必须不变；非法 `write_evidence` action 必须被拒绝。
- `permissive`：仅在评测器中的单因素 ablation；去掉 authority gate，凡 observation 含 provenance ref 就写成 formal item 并标 resolved。它永不成为产品路径。

## Primary metrics

- formal-state contamination events；
- advisory observation 被写成 formal item 的数量；
- false resolution；
- 按冻结 scenario expectation 正确进入 `request_verification` / `defer` 的数量。

## Scope

30 个冻结场景覆盖 weak/indirect/conflicting/no-support/human-verification/partial-formal。它们验证工程权限和状态机，不是领域真值或真实用户效果研究。
