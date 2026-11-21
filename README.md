# Data Quality Gate API（数据质量闸门）

基于 **Python + FastAPI + SQLAlchemy + SQLite** 的数据质量闸门服务，使用 **Docker Compose** 一键启动。
用户提交带 `schema_version` 的批次数据与质量规则，服务执行字段约束、跨记录唯一性与批次级阈值校验，
生成带失败原因的校验报告；**仅当批次通过时**才登记为可供下游读取的批次版本。

## 一、快速开始

```bash
docker compose up --build -d
# 服务地址 http://localhost:8000，交互式文档 http://localhost:8000/docs
curl http://localhost:8000/health
```

数据持久化在 Docker 卷 `dqg-data`（容器内 `/data/dqg.db`），容器重启后已登记批次与幂等记录不丢失。

本地无 Docker 时可直接运行：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 二、接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/batches/submit` | 提交批次进行质量校验（幂等） |
| GET | `/batches` | 列出所有**已登记**（通过）的批次版本 |
| GET | `/batches/{batch_id}` | 下游读取已登记批次明细；失败/未登记返回 404 |
| GET | `/submissions/{batch_id}` | 查询提交状态与完整校验报告（含失败原因） |
| GET | `/submissions/{batch_id}/report` | 仅查询校验报告 |
| GET | `/submissions/{batch_id}/rules` | 查询该提交实际使用的规则快照 |
| GET | `/catalog/rules` | 查询支持的规则类型目录与参数说明 |
| GET | `/catalog/samples` | 查询示例提交载荷（通过 / 失败各一个） |

## 三、规则模型

规则随每次提交内联传入，分三类：

1. **字段约束 `field_rules`**：按字段配置，支持
   `required`、`type`（string/integer/number/boolean）、`min`、`max`、
   `min_length`、`max_length`、`in`（枚举）、`regex`（正则）。
2. **跨记录唯一性 `unique_rules`**：按一个或多个字段的组合在整批内去重；
   任一组成字段为 `null` 的记录跳过唯一性判定。
3. **批次级阈值 `batch_rules`**：
   - `max_failure_rate`（必填，0~1）：失败记录占比上限，超过即整批失败；
   - `min_record_count` / `max_record_count`：批次记录数上下限。

任一规则不满足则批次状态为 `FAILED`，报告中同时返回全部字段失败、唯一性冲突与批次阈值失败及中文原因。

## 四、关键语义

- **幂等**：以 `batch_id` 为幂等键。重复提交相同请求体返回与首次完全一致的报告（`replayed=true`），
  不会重复登记；相同 `batch_id` 但请求体（schema/records/rules）不一致返回 `409`。
- **失败不留可读批次**：`FAILED` 批次只保留提交元数据与报告供排查，不写入 `batches`/`batch_records`，
  下游读取返回 404；`PASSED` 批次才在同一事务内登记批次与全部明细。
- **schema 版本**：每个批次显式携带 `schema_version`，登记与查询均按版本记录。

## 五、请求示例

```bash
curl -X POST http://localhost:8000/batches/submit \
  -H "Content-Type: application/json" \
  -d "$(curl -s http://localhost:8000/catalog/samples \
      | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["examples"][0]["payload"]))')"
```

## 六、目录结构

```
app/
  main.py        # FastAPI 入口（lifespan 建表）
  config.py      # 环境变量配置
  database.py    # SQLAlchemy 引擎/会话/SQLite PRAGMA
  models.py      # ORM：submissions / batches / batch_records
  schemas.py     # Pydantic 请求与报告模型
  validation.py  # 纯函数校验引擎（三类规则）
  services.py    # 幂等提交、事务登记、查询
  routers.py     # HTTP 路由
data/
  rule_catalog.json     # 规则目录
  sample_payloads.json  # 示例数据
tests/test_api.py       # 接口自测（pytest + TestClient）
```

## 七、自测

```bash
pip install -r requirements.txt pytest httpx
pytest tests/ -v
```
