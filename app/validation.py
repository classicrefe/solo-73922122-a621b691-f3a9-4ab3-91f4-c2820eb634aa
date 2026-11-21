"""质量规则校验引擎。

三类规则：
1. 字段约束 (field_rules)：required / type / min / max / min_length /
   max_length / in / regex，逐条记录逐字段检查；
2. 跨记录唯一性 (unique_rules)：按字段组合在整个批次内检查重复；
3. 批次级阈值 (batch_rules)：失败率、最小/最大记录数。

引擎只做纯计算，不访问数据库，便于测试与复用。
"""
from __future__ import annotations

import re
from typing import Any

from .schemas import (
    BatchFailure,
    FieldFailure,
    FieldRule,
    RecordFailure,
    RuleSet,
    UniqueFailure,
    UniqueRule,
    ValidationReport,
    ValidationSummary,
)

_MISSING = object()


def _is_blank(value: Any) -> bool:
    """required 规则对“空”的定义：None 或空白字符串。"""
    return value is None or (isinstance(value, str) and value.strip() == "")


def _numeric(value: Any) -> float | None:
    # bool 是 int 的子类，数值规则不应把 true/false 当数字
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def check_field_rule(rule: FieldRule, value: Any) -> str | None:
    """返回 None 表示通过，否则返回中文失败原因。"""
    rtype = rule.type

    if rtype == "required":
        if _is_blank(value):
            return "必填字段缺失或为空"
        return None

    # 以下规则对缺失值不生效（交由 required 负责）
    if value is None:
        return None

    if rtype == "type":
        expected = rule.value
        if expected == "string":
            if not isinstance(value, str):
                return f"期望 string 类型，实际为 {type(value).__name__}"
        elif expected == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                return f"期望 integer 类型，实际为 {type(value).__name__}"
        elif expected == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"期望 number 类型，实际为 {type(value).__name__}"
        elif expected == "boolean":
            if not isinstance(value, bool):
                return f"期望 boolean 类型，实际为 {type(value).__name__}"
        else:
            return f"未知目标类型: {expected}"
        return None

    if rtype == "min":
        num = _numeric(value)
        if num is None:
            return f"min 规则要求数值，实际为 {value!r}"
        if rule.min is None:
            return "min 规则缺少 min 参数"
        if num < rule.min:
            return f"数值 {num} 小于最小值 {rule.min}"
        return None

    if rtype == "max":
        num = _numeric(value)
        if num is None:
            return f"max 规则要求数值，实际为 {value!r}"
        if rule.max is None:
            return "max 规则缺少 max 参数"
        if num > rule.max:
            return f"数值 {num} 大于最大值 {rule.max}"
        return None

    if rtype == "min_length":
        if not isinstance(value, str):
            return f"长度规则要求 string 类型，实际为 {type(value).__name__}"
        if rule.min_length is not None and len(value) < rule.min_length:
            return f"字符串长度 {len(value)} 小于最小长度 {rule.min_length}"
        return None

    if rtype == "max_length":
        if not isinstance(value, str):
            return f"长度规则要求 string 类型，实际为 {type(value).__name__}"
        if rule.max_length is not None and len(value) > rule.max_length:
            return f"字符串长度 {len(value)} 超过最大长度 {rule.max_length}"
        return None

    if rtype == "in":
        allowed = rule.values if rule.values is not None else []
        if value not in allowed:
            return f"取值 {value!r} 不在允许集合 {allowed} 中"
        return None

    if rtype == "regex":
        if not isinstance(value, str):
            return f"regex 规则要求 string 类型，实际为 {type(value).__name__}"
        if rule.pattern is None:
            return "regex 规则缺少 pattern 参数"
        try:
            if re.search(rule.pattern, value) is None:
                return f"取值 {value!r} 不匹配正则 {rule.pattern}"
        except re.error as exc:
            return f"正则表达式无效: {exc}"
        return None

    return f"不支持的规则类型: {rtype}"


def _check_field_rules(
    rules: RuleSet, records: list[dict[str, Any]]
) -> list[RecordFailure]:
    record_failures: list[RecordFailure] = []
    for idx, record in enumerate(records):
        field_failures: dict[str, list[FieldFailure]] = {}
        for field_name, field_rules in rules.field_rules.items():
            value = record.get(field_name, _MISSING)
            if value is _MISSING:
                value = None
            failures: list[FieldFailure] = []
            for rule in field_rules:
                message = check_field_rule(rule, value)
                if message is not None:
                    failures.append(
                        FieldFailure(rule=rule.type, message=message, value=value)
                    )
            if failures:
                field_failures[field_name] = failures
        if field_failures:
            record_failures.append(
                RecordFailure(record_index=idx, field_failures=field_failures)
            )
    return record_failures


def _check_unique_rules(
    rules: RuleSet, records: list[dict[str, Any]]
) -> list[UniqueFailure]:
    unique_failures: list[UniqueFailure] = []
    for rule in rules.unique_rules:
        seen: dict[tuple[Any, ...], list[int]] = {}
        for idx, record in enumerate(records):
            key = tuple(record.get(field) for field in rule.fields)
            # 任一组成字段为 None 时不参与唯一性判定（SQL 语义）
            if any(v is None for v in key):
                continue
            seen.setdefault(key, []).append(idx)
        for key, indexes in seen.items():
            if len(indexes) > 1:
                values = {field: record_value for field, record_value in zip(rule.fields, key)}
                unique_failures.append(
                    UniqueFailure(
                        fields=rule.fields,
                        values=[values],
                        record_indexes=indexes,
                        message=(
                            f"字段组合 {rule.fields}={list(key)} 出现 {len(indexes)} 次，"
                            "违反跨记录唯一性"
                        ),
                    )
                )
    return unique_failures


def _check_batch_rules(
    rules: RuleSet,
    total: int,
    failed_records: int,
    field_failure_count: int,
    unique_failure_count: int,
) -> list[BatchFailure]:
    failures: list[BatchFailure] = []
    br = rules.batch_rules
    failure_rate = (failed_records / total) if total else 0.0

    if total < br.min_record_count:
        failures.append(
            BatchFailure(
                rule="min_record_count",
                message=f"批次记录数 {total} 小于最小要求 {br.min_record_count}",
            )
        )
    if br.max_record_count is not None and total > br.max_record_count:
        failures.append(
            BatchFailure(
                rule="max_record_count",
                message=f"批次记录数 {total} 超过最大限制 {br.max_record_count}",
            )
        )
    if failure_rate > br.max_failure_rate:
        failures.append(
            BatchFailure(
                rule="max_failure_rate",
                message=(
                    f"失败记录占比 {failure_rate:.4f} 超过阈值 {br.max_failure_rate}"
                ),
            )
        )
    return failures


def validate_batch(
    batch_id: str, schema_version: str, records: list[dict[str, Any]], rules: RuleSet
) -> ValidationReport:
    record_failures = _check_field_rules(rules, records)
    unique_failures = _check_unique_rules(rules, records)

    # 涉及唯一性冲突的记录同样计入失败记录
    unique_record_indexes = {
        idx for uf in unique_failures for idx in uf.record_indexes
    }
    failed_record_indexes = {rf.record_index for rf in record_failures} | unique_record_indexes

    total = len(records)
    failed_records = len(failed_record_indexes)
    passed_records = total - failed_records
    failure_rate = (failed_records / total) if total else 0.0
    field_failure_count = sum(
        len(fl) for rf in record_failures for fl in rf.field_failures.values()
    )

    batch_failures = _check_batch_rules(
        rules, total, failed_records, field_failure_count, len(unique_failures)
    )

    status = "PASSED" if not (
        record_failures or unique_failures or batch_failures
    ) else "FAILED"

    summary = ValidationSummary(
        total_records=total,
        passed_records=passed_records,
        failed_records=failed_records,
        failure_rate=round(failure_rate, 6),
        field_failure_count=field_failure_count,
        unique_failure_count=len(unique_failures),
        batch_failure_count=len(batch_failures),
    )

    return ValidationReport(
        batch_id=batch_id,
        schema_version=schema_version,
        status=status,
        summary=summary,
        record_failures=record_failures,
        unique_failures=unique_failures,
        batch_failures=batch_failures,
    )
