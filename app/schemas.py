"""请求 / 响应 Pydantic 模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# 规则定义
# ---------------------------------------------------------------------------

FieldRuleType = Literal[
    "required",
    "type",
    "min",
    "max",
    "min_length",
    "max_length",
    "in",
    "regex",
]


class FieldRule(BaseModel):
    type: FieldRuleType
    # 规则参数；不同 type 使用不同参数，未使用的参数保持 None
    value: Any | None = Field(default=None, description="type 规则的目标类型: string/integer/number/boolean")
    min: float | None = Field(default=None, description="min 规则下界")
    max: float | None = Field(default=None, description="max 规则上界")
    min_length: int | None = None
    max_length: int | None = None
    values: list[Any] | None = Field(default=None, description="in 规则允许的取值集合")
    pattern: str | None = Field(default=None, description="regex 规则的正则表达式")


class UniqueRule(BaseModel):
    fields: list[str] = Field(min_length=1)


class BatchRule(BaseModel):
    max_failure_rate: float = Field(ge=0.0, le=1.0)
    min_record_count: int = Field(default=0, ge=0)
    max_record_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check_bounds(self) -> "BatchRule":
        if self.max_record_count is not None and self.max_record_count < self.min_record_count:
            raise ValueError("max_record_count 不能小于 min_record_count")
        return self


class RuleSet(BaseModel):
    field_rules: dict[str, list[FieldRule]] = Field(default_factory=dict)
    unique_rules: list[UniqueRule] = Field(default_factory=list)
    batch_rules: BatchRule


# ---------------------------------------------------------------------------
# 提交
# ---------------------------------------------------------------------------


class SubmissionRequest(BaseModel):
    batch_id: str = Field(min_length=1, max_length=128)
    schema_version: str = Field(min_length=1, max_length=64)
    records: list[dict[str, Any]] = Field(default_factory=list)
    rules: RuleSet


# ---------------------------------------------------------------------------
# 校验报告
# ---------------------------------------------------------------------------


class FieldFailure(BaseModel):
    rule: str
    message: str
    value: Any | None = None


class RecordFailure(BaseModel):
    record_index: int
    field_failures: dict[str, list[FieldFailure]] = Field(default_factory=dict)


class UniqueFailure(BaseModel):
    fields: list[str]
    values: list[dict[str, Any]]
    record_indexes: list[int]
    message: str


class BatchFailure(BaseModel):
    rule: str
    message: str


class ValidationSummary(BaseModel):
    total_records: int
    passed_records: int
    failed_records: int
    failure_rate: float
    field_failure_count: int
    unique_failure_count: int
    batch_failure_count: int


class ValidationReport(BaseModel):
    batch_id: str
    schema_version: str
    status: Literal["PASSED", "FAILED"]
    summary: ValidationSummary
    record_failures: list[RecordFailure] = Field(default_factory=list)
    unique_failures: list[UniqueFailure] = Field(default_factory=list)
    batch_failures: list[BatchFailure] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 接口响应
# ---------------------------------------------------------------------------


class SubmissionResponse(BaseModel):
    batch_id: str
    schema_version: str
    status: Literal["PASSED", "FAILED"]
    replayed: bool = Field(description="是否为重复提交的幂等重放结果")
    registered: bool
    report: ValidationReport


class BatchRecordOut(BaseModel):
    record_index: int
    record: dict[str, Any]


class RegisteredBatchOut(BaseModel):
    batch_id: str
    schema_version: str
    record_count: int
    registered_at: str
    records: list[BatchRecordOut] = Field(default_factory=list)


class BatchSummaryOut(BaseModel):
    batch_id: str
    schema_version: str
    record_count: int
    registered_at: str


class HealthOut(BaseModel):
    status: str
    version: str
