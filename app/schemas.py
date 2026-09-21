"""请求 / 响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ExperimentCreate(BaseModel):
    """创建交叉实验的请求体。"""

    conditions: list[str] = Field(
        ..., min_length=2, description="互异的实验条件，至少 2 个"
    )
    subject_ids: list[str] = Field(
        ..., min_length=1, description="互异受试者 ID"
    )
    seed: int = Field(..., description="随机种子，相同输入与种子产生相同分配")
    washout_seconds: int = Field(
        default=0,
        ge=0,
        description=(
            "相邻周期之间的最小洗脱秒数（非负）。进入下一周期前，上一周期"
            "必须先登记完成且已过 completed_at + washout_seconds；0 表示不设门禁"
        ),
    )


class AssignmentOut(BaseModel):
    """受试者名册条目；不含条件序列，避免提前揭盲。"""

    subject_id: str


class ExperimentOut(BaseModel):
    experiment_id: str
    conditions: list[str]
    seed: int
    group_size: int
    num_subjects: int
    num_periods: int
    washout_seconds: int
    created_at: str
    assignments: list[AssignmentOut]


class ExperimentSummary(BaseModel):
    experiment_id: str
    conditions: list[str]
    seed: int
    group_size: int
    num_subjects: int
    num_periods: int
    washout_seconds: int
    created_at: str


class AdvanceRequest(BaseModel):
    """按周期受控揭盲的推进请求。"""

    subject_id: str = Field(..., min_length=1, description="受试者 ID")
    expected_period: int = Field(
        ...,
        ge=1,
        description="调用方认为该受试者所处的下一周期（从 1 开始）",
    )
    request_id: str = Field(
        ..., min_length=1, description="幂等键，相同 request_id 重试不重复推进"
    )


class AdvanceOut(BaseModel):
    experiment_id: str
    subject_id: str
    period: int
    condition: str
    request_id: str
    revealed_at: str


class CompleteRequest(BaseModel):
    """周期完成登记请求。"""

    subject_id: str = Field(..., min_length=1, description="受试者 ID")
    expected_period: int = Field(
        ...,
        ge=1,
        description="调用方要登记完成的周期；必须是当前已揭盲且尚未完成的周期",
    )
    request_id: str = Field(
        ..., min_length=1, description="幂等键，相同 request_id 重试不重复登记"
    )


class CompleteOut(BaseModel):
    experiment_id: str
    subject_id: str
    period: int
    request_id: str
    completed_at: str


class RevealOut(BaseModel):
    """一条已揭盲记录。"""

    period: int
    condition: str
    revealed_at: str


class CompletionOut(BaseModel):
    """一条已完成周期记录。"""

    period: int
    completed_at: str


class ProgressOut(BaseModel):
    """受试者进度：只含已揭盲 / 已完成记录与 next_period，不暴露后续安排。"""

    experiment_id: str
    subject_id: str
    next_period: int
    num_periods: int
    washout_seconds: int
    completed: bool
    reveals: list[RevealOut]
    completions: list[CompletionOut]
    next_available_at: str | None = None
