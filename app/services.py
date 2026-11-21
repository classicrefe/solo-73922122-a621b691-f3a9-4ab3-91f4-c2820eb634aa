"""业务服务层：幂等提交、批次登记与查询。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import canonical_json
from .models import Batch, BatchRecord, Submission
from .schemas import (
    BatchSummaryOut,
    RegisteredBatchOut,
    BatchRecordOut,
    SubmissionRequest,
    SubmissionResponse,
    ValidationReport,
)
from .validation import validate_batch


class IdempotencyConflict(Exception):
    """同一 batch_id 重复提交但请求体与首次不一致。"""


def _hash_payload(req: SubmissionRequest) -> str:
    body = {
        "schema_version": req.schema_version,
        "records": req.records,
        "rules": req.rules.model_dump(),
    }
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def submit_batch(db: Session, req: SubmissionRequest) -> SubmissionResponse:
    """提交批次；同 batch_id + 同请求体的重复调用返回完全一致的结果。"""
    payload_hash = _hash_payload(req)

    existing = db.get(Submission, req.batch_id)
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise IdempotencyConflict(req.batch_id)
        # 幂等重放：直接返回首次生成的报告，不重新登记
        return _replay(existing)

    # 并发下可能有多个线程同时进入此处；flush/commit 都可能触发 UNIQUE 冲突，
    # 冲突后回滚并重放先提交者的结果，最多重试 3 次（SQLite busy_timeout 之外的兜底）
    for attempt in range(3):
        try:
            return _persist_new_submission(db, req, payload_hash)
        except IntegrityError:
            db.rollback()
            existing = db.get(Submission, req.batch_id)
            if existing is None:
                # 极端情况下先提交者的事务仍未可见，短暂等待后重试
                if attempt < 2:
                    continue
                raise
            if existing.payload_hash != payload_hash:
                raise IdempotencyConflict(req.batch_id)
            return _replay(existing)
    raise RuntimeError("unreachable")  # pragma: no cover


def _replay(existing: Submission) -> SubmissionResponse:
    report = ValidationReport.model_validate_json(existing.report_json)
    return SubmissionResponse(
        batch_id=existing.batch_id,
        schema_version=existing.schema_version,
        status=existing.status,
        replayed=True,
        registered=existing.batch is not None,
        report=report,
    )


def _persist_new_submission(
    db: Session, req: SubmissionRequest, payload_hash: str
) -> SubmissionResponse:
    report = validate_batch(req.batch_id, req.schema_version, req.records, req.rules)

    submission = Submission(
        batch_id=req.batch_id,
        schema_version=req.schema_version,
        payload_hash=payload_hash,
        status=report.status,
        record_count=len(req.records),
        report_json=report.model_dump_json(),
        rule_set_json=canonical_json(req.rules.model_dump()),
    )
    db.add(submission)

    # 关键不变量：只有 PASSED 才登记可读批次，FAILED 不写任何批次数据
    if report.status == "PASSED":
        batch = Batch(
            batch_id=req.batch_id,
            schema_version=req.schema_version,
            record_count=len(req.records),
            payload_hash=payload_hash,
        )
        db.add(batch)
        db.flush()  # 先建立批次，再写明细
        for idx, record in enumerate(req.records):
            db.add(
                BatchRecord(
                    batch_id=req.batch_id,
                    record_index=idx,
                    record_json=record,
                )
            )

    db.commit()

    return SubmissionResponse(
        batch_id=req.batch_id,
        schema_version=req.schema_version,
        status=report.status,
        replayed=False,
        registered=report.status == "PASSED",
        report=report,
    )


def get_submission(db: Session, batch_id: str) -> SubmissionResponse | None:
    existing = db.get(Submission, batch_id)
    if existing is None:
        return None
    report = ValidationReport.model_validate_json(existing.report_json)
    return SubmissionResponse(
        batch_id=existing.batch_id,
        schema_version=existing.schema_version,
        status=existing.status,
        replayed=True,
        registered=existing.batch is not None,
        report=report,
    )


def get_registered_batch(db: Session, batch_id: str) -> RegisteredBatchOut | None:
    batch = db.get(Batch, batch_id)
    if batch is None:
        return None
    return RegisteredBatchOut(
        batch_id=batch.batch_id,
        schema_version=batch.schema_version,
        record_count=batch.record_count,
        registered_at=_iso(batch.registered_at),
        records=[
            BatchRecordOut(record_index=r.record_index, record=r.record_json)
            for r in batch.records
        ],
    )


def list_registered_batches(db: Session) -> list[BatchSummaryOut]:
    rows = db.scalars(select(Batch).order_by(Batch.registered_at.desc())).all()
    return [
        BatchSummaryOut(
            batch_id=b.batch_id,
            schema_version=b.schema_version,
            record_count=b.record_count,
            registered_at=_iso(b.registered_at),
        )
        for b in rows
    ]


def get_report(db: Session, batch_id: str) -> ValidationReport | None:
    existing = db.get(Submission, batch_id)
    if existing is None:
        return None
    return ValidationReport.model_validate_json(existing.report_json)


def ruleset_snapshot(db: Session, batch_id: str) -> dict | None:
    existing = db.get(Submission, batch_id)
    if existing is None:
        return None
    return json.loads(existing.rule_set_json)
