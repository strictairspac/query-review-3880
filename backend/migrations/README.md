Schema v2.

v1 由 SQLAlchemy metadata 创建 users/samples/batches/evidence/audit；seed_demo 初始化独立 fixture 表 invoices 与 NOLOGIN query_reader。

v2 在 `src/config/schema.py` 的 `migrate()` 中幂等补列，应用启动时先于 seed 执行，升级旧库无需手工迁移：

- invoices：新增 settlement_period、ingest_seq（对应结算账期与入库水位序号；基线账单 ingest_seq 为 NULL，视为始终纳入）。
- batches：新增 period、status_reason、version、updated_at。
- evidence：新增 period、watermark_seq、version、state（valid/stale/failed）、affected_customers、invalidated_seq、reason。
- 新表 settlement_periods、bill_ingestions、backfill_batches 由 metadata 创建；bill_ingestions 对 (period, external_ref)、(period, invoice_id)、(period, seq) 唯一。

旧数据没有账期归属时，seed_demo 把 invoices 归入已关闭的演示基线账期 2026-10，行为与升级前一致。

`invoices` 仍是唯一允许 SQL 访问的业务表，query_reader 只保留该表 SELECT 权限。执行用户 SQL 时在每处 invoices 引用上注入 settlement_period 与 ingest_seq 条件，账期隔离对相关子查询同样生效。

Run `python -m scripts.seed_demo` for repeatable initialization; natural keys preserve user edits.
