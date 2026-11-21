"""运行期配置，全部支持通过环境变量覆盖。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings:
    # SQLite 数据库文件位置（容器内默认 /data/dqg.db）
    db_path: str = os.getenv("DQG_DB_PATH", str(BASE_DIR / "data" / "dqg.db"))
    # 规则目录（规则类型说明）
    catalog_path: str = os.getenv(
        "DQG_CATALOG_PATH", str(BASE_DIR / "data" / "rule_catalog.json")
    )
    # 示例批次数据
    sample_path: str = os.getenv(
        "DQG_SAMPLE_PATH", str(BASE_DIR / "data" / "sample_payloads.json")
    )

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"


settings = Settings()
