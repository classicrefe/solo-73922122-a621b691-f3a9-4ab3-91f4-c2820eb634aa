"""SQLAlchemy 引擎与会话管理。"""
from __future__ import annotations

import json
import os
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def _ensure_data_dir() -> None:
    db_dir = os.path.dirname(settings.db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


_ensure_data_dir()

engine = create_engine(
    settings.db_url,
    connect_args={"check_same_thread": False, "timeout": 30},
    future=True,
)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """建表（幂等）。"""
    from . import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def canonical_json(payload: object) -> str:
    """用于幂等比对的规范化 JSON 字符串。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
