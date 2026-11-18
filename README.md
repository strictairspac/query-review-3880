# 冷链结算 SQL 复核台

为澄港冷链结算组保存 SQL 基准样本、独立候选改写和实库执行证据。原生 HTML/CSS/JS 前端，Python 后端执行解析、只读查询和多重集合比对。

## 真实工作场景

澄港冷链结算组共 18 人，每天从运费账单中按客户、路线、温控合格条件汇总结算。目前通过邮件传递 `coldchain-settlement.sql` 和表格复核，三个痛点是版本难定位、口径变动容易漏查、单次查询截图无法还原执行计划。上游是冷链计费账单，本站替代 SQL 改写复核和证据归档，下游是结算复核人。演示数据固定 600 条账单、3 客户、2 路线，字段与页面说明一致。

## 业务模块

1. 样本库：登记名称、口径说明与原始 SELECT，防止重名；提供温控合格、路线费用、异常温控三个起始样本。
2. SQL 复核：选择基准与结算账期，编辑候选，校验后创建独立批次并入 Redis 队列。
3. 运行历史：跟踪 queued/running/waiting_watermark/passed/different/failed/backfilling，失败也保留证据；待等水位与补算均显示原因。
4. 证据详情：真实 EXPLAIN ANALYZE、两侧结果行、SHA-256 摘要、遗漏/新增行、JSON 下载与按水位复跑核对。
5. 账期水位：按账期记录入库水位，登记迟到账单，跟进补算批次；水位未稳定时不会发布通过。
6. 管理员操作审计：记录写请求、执行账号和 HTTP 结果，包括拒绝请求。

### 结算账期、水位与迟到账单

账期关闭后仍可能收到迟到的冷链账单。系统按账期维护单调递增的入库水位 `ingest_seq`：

- 每笔新账单在同一账期的行锁内分配序号，重复的外部单号或账单号幂等跳过，不重复计入水位。
- 水位稳定需同时满足：账期已关闭、没有排队/执行中/中断待恢复的补算、没有失败的补算、所有证据都采用当前入库水位。
- 水位未稳定时，等价结果只停在 `waiting_watermark`（页面显示“待等水位”），**不发布 `passed`**。
- 关账后的迟到账单让该账期证据失效，只对**受影响客户**登记补算；证据中未受影响客户的行保留、补算期间继续可查，页面标注“补算”及原因。
- 补算按证据水位（`watermark_seq`）与客户范围增量重算，其他客户不重算。

并发与恢复：

- 复核与补算写证据都用乐观版本（`evidence.version`）条件提交；版本被并发更新抢先时保留对方结果并重试，不覆盖。
- 同一账期的补算与入库按账期行加锁、证据按 id 排序处理，避免交叉死锁；死锁/序列化冲突会回滚并重新排队。
- 补算按 `(evidence_id, target_seq)` 幂等去重，重试或中断恢复不会重复计入；`interrupted` 的补算由 worker 恢复循环重新排队。
- 证据记录实际采用的 `watermark_seq`；下载 JSON 与“按水位复跑”都还原该水位，不纳入水位之后到达的账单。
- 账期隔离在 SQL 的每处 `invoices` 引用上生效（含相关子查询），其他账期或水位之后的账单不会混入。

补算优先按客户增量合并；样本输出不含 `customer` 列或子查询无法按客户裁剪时退回全量重算，并在证据中标注方式。证据为 `stale` 且输出无 `customer` 列时，保守地整份结果标为待补算。

| 角色 | 主要页面 | 权限与典型动作 |
|---|---|---|
| admin 管理员 | 全部页面、审计 | 创建样本/批次/账期、登记迟到账单、查看/导出结果、查看写操作审计 |
| operator 操作员 | 五个业务页面 | 登记 SQL、提交后台复核、登记迟到账单与关账、查看/导出结果；不可查看管理员审计 |
| viewer 观察员 | 四个业务页面 | 只读与导出；API 层拒绝所有业务写请求 |

## 快速启动

```bash
cp .env.example .env
docker compose config --quiet
docker compose up -d --build
# 启动自动 seed；手动重跑不新增且不覆盖用户修改
docker compose exec -T backend python -m scripts.seed_demo
```

访问 [前端](http://localhost:18240)、[后端 OpenAPI](http://localhost:19240/docs)。数据库映射端口 17740。项目的日志、截图、测试脚本在批次根目录 `test-artifacts/gb-4101/`，不放入源码。

演示账号邮箱为 `admin@chenggang.test`、`operator@chenggang.test`、`viewer@chenggang.test`，统一密码 `ColdChain!17`。这些是本地固定演示凭据，上线前应修改 `.env` 和账号密码。

```bash
# 仅清理本项目及其演示数据
docker compose down -v --remove-orphans
```

## 一天工作剧本

1. 09:00 operator 登录，打开“低温结算汇总”样本。
2. 09:05 查看原始 SQL：按客户执行两处相关 SUM 子查询，过滤温度不高于 8 度的运费。
3. 09:10 在账期水位页选择账期（演示基线为已关账的 `2026-10`），创建 `shift-20261117` 批次，候选改写为下面的单次分组查询。
4. 09:15 提交到 worker，在运行历史刷新查看状态。账期开账中时状态为“待等水位”，原因写明“账期尚未关闭”。
5. 09:20 关闭账期，水位稳定后批次发布“结果一致”；打开证据确认两侧均 3 行、摘要相同，展开真实计划比较循环次数与代价。
6. 09:25 把候选条件改为 `temperature > 8`，用新批次执行并查看新增/遗漏行。
7. 09:30 下载 `review-<批次号>.json` 给结算复核人，文件记录证据采用的入库水位与还原说明。
8. 09:40 账期满后收到迟到账单：在账期水位页登记一笔（客户“澄港海产”），批次转为“补算中”；证据里其他两位客户仍可查，受影响客户标为待补算。
9. 09:45 worker 完成补算后水位追平，证据水位前进、批次回到“结果一致”；用“按水位复跑核对”确认摘要可还原。
10. 09:50 viewer 登录复核，登记迟到账单与创建样本均返回 403，但可查看与导出；10:00 admin 查看审计。

```sql
SELECT customer, SUM(amount) AS total
FROM invoices
WHERE temperature <= 8
GROUP BY customer
HAVING SUM(amount) > 100
ORDER BY customer
```

## 算法、输入与口径

输入是原始/候选单条 SELECT。sqlglot 解析 AST，仅准许 invoices 表、指定业务字段和 SUM/COUNT/AVG/MIN/MAX/ROUND/COALESCE/CAST。禁止多语句、INTO、锁、CTE、跨表、schema 和任意函数。解析后重新生成 PostgreSQL SQL，执行在 READ ONLY 事务中，切换 NOLOGIN `query_reader` 角色；该角色仅有 invoices 的 SELECT 权限。每条 statement 超时 2500ms，最多返回 1000 行，AST 最多 250 节点，候选 SQL 最多 6000 字符，最多 20 个排队/运行任务。

两侧均实际执行 `EXPLAIN (ANALYZE, FORMAT JSON, BUFFERS)` 与 SELECT。结果每行按列位置规范为数据库文本值，`Counter` 统计行的重复次数；列数相同且计数集合相同即 equivalent。忽略行顺序及列别名、保留重复行，NULL 保留为空值。按有序的“规范行/计数”生成 SHA-256。数值不同文本表示（如 1 与 1.0）会判不同；这是明确的保守口径。不能据固定数据集通过证明所有可能输入上的 SQL 语义等价；一次耗时不能证明优化收益。

失败态包括语法/白名单失败（提交前 422）、缺少样本（404）、重名（409）、任务配额（429）、数据库超时/运行错误（批次 failed，保存 error）。正常差异是 different，不当成异常。账期不存在（404）、账期已关闭不可重开（409）、账单号已归属其他账期（409）、账单字段越界（422）同样返回明确错误。

## 技术栈、结构与本地开发

| 部分 | 实现 |
|---|---|
| frontend | 原生 ES modules、CSS、Nginx 1.27，olive paper 风格 |
| backend | Python 3.11、FastAPI、SQLAlchemy、sqlglot、PyJWT |
| worker | 独立 Python 进程，Redis BLPOP + DB 状态恢复队列，心跳文件 |
| db / cache | PostgreSQL 16、Redis 7 |
| auth | JWT 8 小时有效，PBKDF2 SHA-256 加盐密码 |

`backend/src/` 按 config/models/schemas/services/controllers/routes/middlewares/utils/constants 分工；`backend/scripts/seed_demo.py` 管理幂等初始化；`backend/src/config/schema.py` 负责幂等建表与补列；`backend/migrations/README.md` 说明 schema v2。`frontend/src/` 按 api/stores/types/components/common/hooks/pages/router/utils/constants 分工。实体链分别为 sample、batch、evidence，加上 settlement_period/bill_ingestion/backfill，各自有模型、schema、服务、路由、API、store 和页面。

后端本地：创建虚拟环境并安装 `backend/requirements.txt`，先用 Compose 启动 db/redis，设置本地可达的 DATABASE_URL/REDIS_URL/JWT_SECRET，进入 backend 执行 `uvicorn src.main:app --reload`；worker 需 `/artifacts` 可写目录（可用 ARTIFACTS_DIR 覆盖）并执行 `python -m src.worker`。最省事的开发方式是修改源码后 `docker compose up -d --build`。前端通过 Nginx 的 `/api/` 去前缀代理访问后端，禁止双重 `/api/api`。

集成测试需要真实 PostgreSQL 与 Redis：

```bash
cd backend
TEST_DATABASE_URL=postgresql+psycopg2://app:...@127.0.0.1:17740/query_review \
TEST_REDIS_URL=redis://127.0.0.1:6379/0 \
python -m pytest tests -q
```

覆盖迟到入账（水位/幂等/客户范围）、跨客户并发补算、补算中断恢复、乐观版本不覆盖、水位还原复跑、跨账期隔离与观察员只读。测试会在开始时清理 `2026-10` 之外的账期、批次、证据和迟到账单，请只在测试库运行。

环境变量：COMPOSE_PROJECT_NAME（容器命名）、FRONTEND_PORT/BACKEND_PORT/DB_PORT（宿主端口）、POSTGRES_PASSWORD（演示库密码）、JWT_SECRET（签名密钥）、SEED_DEMO（默认 true）。后端容器另接收 DATABASE_URL 和 REDIS_URL。5 个服务均有 healthcheck；数据库、Redis、共享证据各自使用命名卷。

共享枚举位置：角色与任务状态在 `backend/src/constants/enums.py` 和 `frontend/src/constants/enums.js`；模型/鉴权服务/worker 消费后端值，状态标签与前端权限消费前端值。源数据 invoice status 为账单业务值 settled/pending，与任务状态独立。

## 已知边界

- 小型工作流没有样本删除和版本编辑；新口径新建样本/批次，旧记录保持独立。
- 使用固定业务数据和单实例 worker；不对接生产数据库，不部署生产高可用。并发正确性靠数据库行锁与乐观版本保证，但吞吐按单 worker 设计。
- 迟到账单由人工在账期水位页登记（或调用 `POST /ingestions`）；不与真实计费系统对接，水位以登记顺序为准，不做按账单时间的乱序回填。
- 只允许受限 SQL 子集；不支持 CTE、窗口、跨表联接和用户自定义函数。
- 查询会执行两次（一次 EXPLAIN ANALYZE，一次取结果）；数据源固定只读，避免两次间口径变化。
- 后端迁移/seed 角色为本地数据库管理员；执行用户 SQL 时强制切换受限只读角色。生产应拆分独立迁移凭据。
- Web 字体来自 Google Fonts，离线会使用 serif/monospace 回退；不影响功能。

License: MIT，见 LICENSE。
