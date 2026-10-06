"""agent：搜索-验证决策层。

依赖方向：agent -> evidence_core（单向，不可反向）。
本层可以调用 LLM，但不能自己产生进入契约表的数字——
所有分数必须经过 evidence_core.scorer。
"""
from .actions import Action, ActionResult
from .baseline import FixedWorkflowBaseline, compare, run_baseline
from .loop import RunResult, SearchAndVerifyAgent, run_agent
from .state import Budget, Candidate, RunState
from .trace import TraceRecorder

__all__ = [
    "Action",
    "ActionResult",
    "Budget",
    "Candidate",
    "FixedWorkflowBaseline",
    "RunResult",
    "RunState",
    "SearchAndVerifyAgent",
    "TraceRecorder",
    "compare",
    "run_agent",
    "run_baseline",
]
