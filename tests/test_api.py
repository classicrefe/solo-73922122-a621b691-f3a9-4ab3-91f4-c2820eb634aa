"""端到端接口自测：通过 FastAPI TestClient 覆盖关键业务路径。"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

# 使用独立临时数据库，避免污染开发库
_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="dqg-test-"), "test.db")
os.environ["DQG_DB_PATH"] = _TMP_DB

from fastapi.testclient import TestClient  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Batch, BatchRecord  # noqa: E402

init_db()
client = TestClient(app)

VALID_PAYLOAD = {
    "batch_id": "test-valid-1",
    "schema_version": "order-schema/v2.1",
    "records": [
        {"order_id": "ORD-1001", "email": "a@example.com", "status": "active", "amount": 10.5},
        {"order_id": "ORD-1002", "email": "b@example.com", "status": "pending", "amount": 20},
    ],
    "rules": {
        "field_rules": {
            "order_id": [{"type": "required"}, {"type": "regex", "pattern": "^ORD-\\d{4}$"}],
            "email": [{"type": "required"}, {"type": "max_length", "max_length": 128}],
            "status": [{"type": "in", "values": ["active", "pending", "inactive"]}],
            "amount": [{"type": "type", "value": "number"}, {"type": "min", "min": 0}],
        },
        "unique_rules": [{"fields": ["order_id"]}],
        "batch_rules": {"max_failure_rate": 0.0, "min_record_count": 1, "max_record_count": 100},
    },
}


def test_health_and_catalog() -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    r = client.get("/catalog/rules")
    assert r.status_code == 200
    cats = {c["category"] for c in r.json()["rule_categories"]}
    assert cats == {"field_rules", "unique_rules", "batch_rules"}

    r = client.get("/catalog/samples")
    assert r.status_code == 200
    assert len(r.json()["examples"]) == 2


def test_valid_batch_passes_and_registers() -> None:
    r = client.post("/batches/submit", json=VALID_PAYLOAD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "PASSED"
    assert body["replayed"] is False
    assert body["registered"] is True
    assert body["report"]["summary"]["total_records"] == 2
    assert body["report"]["summary"]["failed_records"] == 0

    # 下游可读
    r = client.get("/batches/test-valid-1")
    assert r.status_code == 200
    assert r.json()["record_count"] == 2
    assert len(r.json()["records"]) == 2

    # 出现在登记列表中
    r = client.get("/batches")
    ids = [b["batch_id"] for b in r.json()]
    assert "test-valid-1" in ids


def test_idempotent_resubmission_returns_identical_result() -> None:
    r1 = client.post("/batches/submit", json=VALID_PAYLOAD)
    r2 = client.post("/batches/submit", json=VALID_PAYLOAD)
    assert r1.status_code == r2.status_code == 200
    b1, b2 = r1.json(), r2.json()
    assert b2["replayed"] is True
    assert b1["status"] == b2["status"] == "PASSED"
    assert b1["report"] == b2["report"]
    assert b1["registered"] == b2["registered"] is True

    # 数据库中只有一条登记批次与两条明细（未重复写入）
    with SessionLocal() as db:
        assert db.get(Batch, "test-valid-1") is not None
        assert db.query(BatchRecord).filter_by(batch_id="test-valid-1").count() == 2


def test_conflicting_resubmission_returns_409() -> None:
    changed = json.loads(json.dumps(VALID_PAYLOAD))
    changed["records"][0]["amount"] = 999.99
    r = client.post("/batches/submit", json=changed)
    assert r.status_code == 409


def test_failed_batch_has_reasons_and_is_not_readable() -> None:
    payload = {
        "batch_id": "test-invalid-1",
        "schema_version": "order-schema/v2.1",
        "records": [
            # 记录0：status 枚举失败 + amount 下界失败
            {"order_id": "ORD-2001", "email": "x@example.com", "status": "bogus", "amount": -1},
            # 记录1 与记录2 order_id 重复（唯一性失败）
            {"order_id": "ORD-2002", "email": "y@example.com", "status": "active", "amount": 5},
            {"order_id": "ORD-2002", "email": "z@example.com", "status": "active", "amount": 6},
            # 记录3：缺必填 email
            {"order_id": "ORD-2003", "status": "active", "amount": 7},
        ],
        "rules": {
            "field_rules": {
                "order_id": [{"type": "required"}],
                "email": [{"type": "required"}],
                "status": [{"type": "in", "values": ["active", "pending"]}],
                "amount": [{"type": "min", "min": 0}],
            },
            "unique_rules": [{"fields": ["order_id"]}],
            "batch_rules": {"max_failure_rate": 0.0, "min_record_count": 10},
        },
    }
    r = client.post("/batches/submit", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "FAILED"
    assert body["registered"] is False
    report = body["report"]

    # 字段失败原因
    field_indexes = {rf["record_index"] for rf in report["record_failures"]}
    assert field_indexes == {0, 3}
    all_rules = [
        f["rule"]
        for rf in report["record_failures"]
        for fl in rf["field_failures"].values()
        for f in fl
    ]
    assert "in" in all_rules and "min" in all_rules and "required" in all_rules

    # 唯一性失败
    assert len(report["unique_failures"]) == 1
    assert report["unique_failures"][0]["record_indexes"] == [1, 2]

    # 批次级失败（min_record_count）
    batch_rules = {bf["rule"] for bf in report["batch_failures"]}
    assert "min_record_count" in batch_rules
    assert report["summary"]["failed_records"] == 4

    # 关键不变量：失败批次不可读、不在登记列表、无明细落库
    assert client.get("/batches/test-invalid-1").status_code == 404
    ids = [b["batch_id"] for b in client.get("/batches").json()]
    assert "test-invalid-1" not in ids
    with SessionLocal() as db:
        assert db.get(Batch, "test-invalid-1") is None
        assert db.query(BatchRecord).filter_by(batch_id="test-invalid-1").count() == 0

    # 但提交报告仍可查询（用于排查失败原因），且重放结果一致
    r = client.get("/submissions/test-invalid-1")
    assert r.status_code == 200
    assert r.json()["report"] == report
    r2 = client.post("/batches/submit", json=payload)
    assert r2.json()["replayed"] is True
    assert r2.json()["report"] == report

    r = client.get("/submissions/test-invalid-1/rules")
    assert r.status_code == 200
    assert "field_rules" in r.json()


def test_batch_threshold_failure_rate() -> None:
    payload = {
        "batch_id": "test-threshold-1",
        "schema_version": "order-schema/v2.1",
        "records": [
            {"order_id": "ORD-3001", "amount": 1},
            {"order_id": "ORD-3002", "amount": -1},
        ],
        "rules": {
            "field_rules": {"amount": [{"type": "min", "min": 0}]},
            "unique_rules": [],
            "batch_rules": {"max_failure_rate": 0.2, "min_record_count": 1},
        },
    }
    r = client.post("/batches/submit", json=payload)
    body = r.json()
    assert body["status"] == "FAILED"
    assert body["report"]["summary"]["failure_rate"] == 0.5
    assert {bf["rule"] for bf in body["report"]["batch_failures"]} == {"max_failure_rate"}
    assert client.get("/batches/test-threshold-1").status_code == 404


def test_unknown_submission_404() -> None:
    assert client.get("/submissions/nope").status_code == 404
    assert client.get("/batches/nope").status_code == 404


def test_bad_rule_params_rejected_by_pydantic() -> None:
    bad = json.loads(json.dumps(VALID_PAYLOAD))
    bad["batch_id"] = "test-bad-rules"
    bad["rules"]["batch_rules"] = {"max_failure_rate": 1.5}
    r = client.post("/batches/submit", json=bad)
    assert r.status_code == 422


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
