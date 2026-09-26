"""API 请求/响应模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Assignment(BaseModel):
    var: str
    expr: str


class ActivityIn(BaseModel):
    id: str
    name: str = ""
    pre: str = ""
    assign: list[Assignment] = Field(default_factory=list)
    effect: str = ""
    idempotent: bool = False
    can_fail: bool = True
    retryable: bool = True
    max_attempts: int = 3
    pre_crash: bool = False
    post_crash: bool = False
    comp_can_fail: bool = True
    comp_retryable: bool = True
    comp_max_attempts: int = 3
    # 并发调度安全分析（可选，缺省为纯串行）
    fork_group: str = ""
    join_group: str = ""
    locks: list[str] = Field(default_factory=list)
    reads: list[str] = Field(default_factory=list)
    writes: list[str] = Field(default_factory=list)


class EdgeIn(BaseModel):
    src: str
    dst: str
    guard: str = ""


class VariableIn(BaseModel):
    name: str
    type: str = "int"
    initial: Any = 0


class CustomInvariant(BaseModel):
    name: str
    expr: str


class InvariantConfig(BaseModel):
    at_most_once: bool = True
    undo_on_failure: bool = True
    custom: list[CustomInvariant] = Field(default_factory=list)


class ConcResourceIn(BaseModel):
    name: str


class ConcLockIn(BaseModel):
    name: str


class ConcGroupIn(BaseModel):
    id: str
    fork: str
    join: str


class ConcurrencyConfig(BaseModel):
    resources: list[ConcResourceIn] = Field(default_factory=list)
    locks: list[ConcLockIn] = Field(default_factory=list)
    groups: list[ConcGroupIn] = Field(default_factory=list)


class WorkflowSpec(BaseModel):
    start: str
    variables: list[VariableIn] = Field(default_factory=list)
    activities: list[ActivityIn]
    edges: list[EdgeIn] = Field(default_factory=list)
    invariants: InvariantConfig = Field(default_factory=InvariantConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)


class ModelIn(BaseModel):
    name: str = "未命名模型"
    spec: WorkflowSpec


class ModelPatch(BaseModel):
    name: str | None = None
    spec: WorkflowSpec | None = None


class CheckIn(BaseModel):
    bound: int = 16
    persist: bool = True


class ForkIn(BaseModel):
    # 共享前缀：在分叉点之前执行的事件（按 sig 匹配）
    prefix: list[dict[str, Any]]
    # 在分叉点改选的事件签名
    sig: list[Any]
    bound: int = 16
    persist: bool = False


class ValidateIn(BaseModel):
    spec: WorkflowSpec


# ---------------------------------------------------------------------------
# 随机故障风险分析
# ---------------------------------------------------------------------------

class RiskActivity(BaseModel):
    # 调用分支：五类基类概率之和必须为 1（未启用的结果必须为 0）
    id: str
    success: float = 1.0
    retryable: float = 0.0
    fatal: float = 0.0
    pre_crash: float = 0.0
    post_crash: float = 0.0
    # 应答丢失的条件概率
    p_ack: float = 0.5
    p_dup: float = 0.5
    # 补偿分支：概率之和必须为 1
    comp_success: float = 1.0
    comp_retryable: float = 0.0
    comp_fatal: float = 0.0


class RiskAnalyzeIn(BaseModel):
    activities: list[RiskActivity] = Field(default_factory=list)
    bound: int = 24
    top_n: int = 10
    persist: bool = True


class RiskValidateIn(BaseModel):
    spec: WorkflowSpec
    activities: list[RiskActivity] = Field(default_factory=list)


class RiskReplayIn(BaseModel):
    events: list[dict[str, Any]]


class RiskForkIn(BaseModel):
    prefix: list[dict[str, Any]]
    sig: list[Any]
    bound: int = 24
    persist: bool = False


# ---------------------------------------------------------------------------
# 并发调度安全分析
# ---------------------------------------------------------------------------

class ConcCheckIn(BaseModel):
    bound: int = 24
    persist: bool = True


class ConcReplayIn(BaseModel):
    events: list[dict[str, Any]]


class ConcForkIn(BaseModel):
    prefix: list[dict[str, Any]]
    sig: list[Any]
    bound: int = 24
    persist: bool = False


# ---------------------------------------------------------------------------
# 版本行为对比
# ---------------------------------------------------------------------------

class DiffIn(BaseModel):
    baseline_id: str
    candidate_id: str
    bound: int = 16
    persist: bool = True


class DiffForkIn(BaseModel):
    # 分叉所基于的对比记录（从持久化记录打开时模型快照取自该记录）
    diff_run_id: str | None = None
    # 直接提供快照时使用（与 *_id 二选一；记录打开场景两者都给，快照优先）
    baseline_id: str | None = None
    candidate_id: str | None = None
    baseline_spec: WorkflowSpec | None = None
    candidate_spec: WorkflowSpec | None = None
    # 在哪一侧、沿对齐前缀走到分叉点后改选
    side: str = "baseline"        # baseline | candidate
    prefix: list[dict[str, Any]] = Field(default_factory=list)
    sig: list[Any]
    bound: int = 16
