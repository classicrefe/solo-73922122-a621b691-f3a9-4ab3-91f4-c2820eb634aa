"""HTTP 路由。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import __version__
from .config import settings
from .database import get_db
from .schemas import (
    BatchSummaryOut,
    HealthOut,
    RegisteredBatchOut,
    SubmissionRequest,
    SubmissionResponse,
    ValidationReport,
)
from .services import (
    IdempotencyConflict,
    get_registered_batch,
    get_report,
    get_submission,
    list_registered_batches,
    ruleset_snapshot,
    submit_batch,
)

router = APIRouter()


@router.get("/health", response_model=HealthOut, tags=["meta"])
def health() -> HealthOut:
    return HealthOut(status="ok", version=__version__)


@router.post(
    "/batches/submit",
    response_model=SubmissionResponse,
    tags=["batches"],
    status_code=status.HTTP_200_OK,
    summary="提交带 schema 版本的批次数据进行质量校验",
)
def submit(req: SubmissionRequest, db: Session = Depends(get_db)) -> SubmissionResponse:
    try:
        return submit_batch(db, req)
    except IdempotencyConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"batch_id={req.batch_id} 已存在，但本次提交的 schema_version/records/rules "
                "与首次提交不一致；同一批次版本的请求体必须保持一致"
            ),
        )


@router.get(
    "/batches",
    response_model=list[BatchSummaryOut],
    tags=["batches"],
    summary="列出所有已登记（校验通过）的批次版本",
)
def list_batches(db: Session = Depends(get_db)) -> list[BatchSummaryOut]:
    return list_registered_batches(db)


@router.get(
    "/batches/{batch_id}",
    response_model=RegisteredBatchOut,
    tags=["batches"],
    summary="下游读取已登记批次的明细数据（校验失败的批次返回 404）",
)
def read_batch(batch_id: str, db: Session = Depends(get_db)) -> RegisteredBatchOut:
    batch = get_registered_batch(db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"批次 {batch_id} 未登记或未通过质量闸门")
    return batch


@router.get(
    "/submissions/{batch_id}",
    response_model=SubmissionResponse,
    tags=["submissions"],
    summary="查询某次提交的状态与完整校验报告（含失败原因）",
)
def read_submission(batch_id: str, db: Session = Depends(get_db)) -> SubmissionResponse:
    result = get_submission(db, batch_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"未找到提交 {batch_id}")
    return result


@router.get(
    "/submissions/{batch_id}/report",
    response_model=ValidationReport,
    tags=["submissions"],
    summary="仅查询校验报告",
)
def read_report(batch_id: str, db: Session = Depends(get_db)) -> ValidationReport:
    report = get_report(db, batch_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"未找到提交 {batch_id}")
    return report


@router.get(
    "/submissions/{batch_id}/rules",
    tags=["submissions"],
    summary="查询某次提交实际使用的规则快照",
)
def read_rules(batch_id: str, db: Session = Depends(get_db)) -> dict:
    snapshot = ruleset_snapshot(db, batch_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"未找到提交 {batch_id}")
    return snapshot


def _load_json_file(path: str) -> object:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@router.get(
    "/catalog/rules",
    tags=["catalog"],
    summary="查询支持的质量规则类型目录及参数说明",
)
def rule_catalog() -> object:
    try:
        return _load_json_file(settings.catalog_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="规则目录文件缺失")


@router.get(
    "/catalog/samples",
    tags=["catalog"],
    summary="查询示例提交数据（含通过与失败两个样例）",
)
def samples() -> object:
    try:
        return _load_json_file(settings.sample_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="示例数据文件缺失")
