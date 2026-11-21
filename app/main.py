"""FastAPI 应用入口。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__
from .database import init_db
from .routers import router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Data Quality Gate API",
    description=(
        "数据质量闸门：接收带 schema 版本的批次数据与质量规则，"
        "执行字段约束、跨记录唯一性与批次级阈值校验，"
        "仅在校验通过时登记可供下游读取的批次版本。"
    ),
    version=__version__,
    lifespan=lifespan,
)

app.include_router(router)
