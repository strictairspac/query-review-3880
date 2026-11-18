# gb-4101 验收产物

本目录保存本次「账期入库水位 / 迟到账单补算」的验证脚本与运行输出，不放入源码。

## 环境

- PostgreSQL 16（可用 `docker compose up -d db redis`，或本机实例）
- Redis 7
- 后端依赖：`backend/requirements.txt` + `httpx`（TestClient 需要）
- 变量：
  - `TEST_DATABASE_URL`：PostgreSQL 连接串（测试会清理 `2026-10` 之外的账期/批次/证据/迟到账单，**只用于测试库**）
  - `TEST_REDIS_URL`：Redis 连接串

## 自动化测试（覆盖迟到入账、跨客户并发、补算中断）

```bash
cd backend
TEST_DATABASE_URL=postgresql+psycopg2://app:...@127.0.0.1:17740/query_review \
TEST_REDIS_URL=redis://127.0.0.1:6379/0 \
python -m pytest tests -q | tee ../test-artifacts/gb-4101/pytest.log
```

覆盖用例：

| 用例 | 验证点 |
|---|---|
| `test_open_period_cannot_publish_pass` | 水位未稳定不得发布通过 |
| `test_late_bill_invalidates_only_affected_customers` | 迟到账单只失效受影响客户，其他客户继续可查 |
| `test_duplicate_ingestion_is_idempotent` | 重复入账幂等，不重复计入水位 |
| `test_restore_replays_evidence_watermark` | 下载/复跑还原证据采用的水位 |
| `test_viewer_cannot_write_but_can_read` | 观察员只读限制 |
| `test_cross_period_isolation` | 跨账期隔离 |
| `test_backfill_version_conflict_does_not_overwrite` | 并发更新不被覆盖 |
| `test_backfill_retry_not_double_counted` | 重试不重复计入 |
| `test_worker_recovers_interrupted_backfill` | 中断后恢复 |
| `test_concurrent_new_review_and_backfill_do_not_overwrite` | 新复核与补算并发 |
| `test_backfill_interruption_then_recovery_is_not_double_counted` | 中断恢复不重复 |
| `test_cross_customer_backfills_concurrent_keep_both_results` | 跨客户并发补算结果都保留 |
| `test_failed_backfill_can_be_retried_manually` | 失败补算人工恢复，稳定前不发布通过 |

## 端到端剧本

`e2e.sh` 依赖已启动的后端与 worker（端口通过 `API` 变量指定）：

```bash
# 终端 A
cd backend && DATABASE_URL=... REDIS_URL=... JWT_SECRET=... ARTIFACTS_DIR=/artifacts \
  uvicorn src.main:app --port 19240
# 终端 B
cd backend && DATABASE_URL=... REDIS_URL=... JWT_SECRET=... ARTIFACTS_DIR=/artifacts \
  python -m src.worker
# 终端 C
API=http://127.0.0.1:19240 bash test-artifacts/gb-4101/e2e.sh | tee test-artifacts/gb-4101/e2e.log
```

剧本步骤：开账入账 → 复核停在待等水位 → 关账发布通过 → 迟到账单仅失效受影响客户 → 补算后水位稳定 → 按水位复跑与下载 → 观察员写请求 403。

> 注：本仓库开发容器无 Docker，验证时使用本机 PostgreSQL 16 + Redis 7，端口与连接串按 `TEST_*` 变量传入。
