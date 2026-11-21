"""并发竞态自测：同一 batch_id 并发提交，必须只登记一次且结果一致。"""
from __future__ import annotations

import json
import os
import tempfile
import threading

_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="dqg-conc-"), "conc.db")
os.environ["DQG_DB_PATH"] = _TMP_DB

from app.database import SessionLocal, init_db  # noqa: E402
from app.models import Batch, BatchRecord  # noqa: E402
from app.schemas import SubmissionRequest  # noqa: E402
from app.services import IdempotencyConflict, submit_batch  # noqa: E402

init_db()

with open(os.path.join(os.path.dirname(__file__), "..", "data", "sample_payloads.json"), encoding="utf-8") as fh:
    PAYLOAD = json.load(fh)["examples"][0]["payload"]

results: list[object] = []
errors: list[Exception] = []


def worker() -> None:
    try:
        db = SessionLocal()
        try:
            results.append(submit_batch(db, SubmissionRequest.model_validate(PAYLOAD)))
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)


threads = [threading.Thread(target=worker) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()

assert not errors, errors
assert len(results) == 8
statuses = {r.status for r in results}
assert statuses == {"PASSED"}
assert sum(1 for r in results if not r.replayed) == 1, "恰有一次真实登记"
assert sum(1 for r in results if r.replayed) == 7, "其余全部幂等重放"
reports = {r.report.model_dump_json() for r in results}
assert len(reports) == 1, "所有响应报告必须完全一致"

with SessionLocal() as db:
    assert db.get(Batch, PAYLOAD["batch_id"]) is not None
    assert db.query(BatchRecord).filter_by(batch_id=PAYLOAD["batch_id"]).count() == len(
        PAYLOAD["records"]
    )

print("并发测试通过：8 线程同批次提交 -> 1 次登记 + 7 次重放，明细无重复")
