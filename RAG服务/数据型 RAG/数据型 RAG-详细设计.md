# 数据型 RAG / Text2SQL 详细设计（语义层 + 只读 SQL 编排）

> 本文是 [RAG服务引入路线.md](../RAG服务引入路线.md) §2.3 路线 C（数据型 RAG / Text2SQL）的落地展开，输出**技术栈、语义层设计、SQL 生成与校验、权限与版本一致性、包结构、关键代码骨架与约束落地**。
> **技术栈**：Python（FastAPI + SQLAlchemy + LlamaIndex + Pydantic）。RAG 服务与三大 MES 服务（Java/Spring）跨语言共存，通过**只读数据库账号 + 只读 REST** 解耦，互不侵入。
> **口径纪律**：数据型 RAG 在本项目中是**设计阶段的引入规划**，不是线上已落地能力。讲法遵循 [项目亮点与指标卡片.md](../../面试指南/项目亮点与指标卡片.md) §0 的口径纪律--说"规划方向 / 设计取舍"，不说"我们已经做了 Text2SQL"。MES 领域对错误答案零容忍（错给一份数据会误导决策），所以本文强调**只在语义层上生成 SQL + 多层校验防写 + 只读账号兜底**，而非让 LLM 直接碰原始表。

---

## 1. 设计目标与边界

### 1.1 目标

对 MES 业务库做**自然语言 -> SQL -> 图表/表格**，服务管理层/班长的结构化数据查询场景--"SMT1 号线昨天 OEE？""工单 WO-1234 现在到哪站？""上周返修 TOP3 缺陷？"。这类问题的答案来源是**MES 业务库实时数据 + 报表**（[RAG服务引入路线.md](../RAG服务引入路线.md) §0 第四类用户），不是文档（路线 B）、不是追溯链（路线 A）、不是拦截处置（路线 D），必须分线做。

核心设计：**不让 LLM 直接碰原始表，而是在"语义层"上生成 SQL**。本 MES 已有清晰的 14 个限界上下文边界（[领域总览.md](../../领域模型/领域总览.md) §2），正好按上下文把"工单进度""在制品位置""缺陷统计"这些概念固化成**语义视图**，LLM 只在视图上生成 SELECT，准确率远高于直接 Text2SQL 原始表。

### 1.2 硬边界（一开口就要讲）

| 边界 | 说明 | 落地 |
|------|------|------|
| **只在语义层生成 SQL** | LLM 不直接碰原始表，只在按上下文固化的语义视图上生成 SELECT | 语义视图作为 LLM 的"可见 schema"，原始表对 LLM 不可见（§4） |
| **只读账号兜底** | 数据库连接用只读账号，DB 级禁止任何写操作 | 只读账号仅授 SELECT 权限，DDL/DML/事务提交在 DB 级被拒（§5.2） |
| **表白名单 + AST 校验** | 应用层校验：生成的 SQL 只能查白名单视图，且必须是 SELECT 语义 | SQL 经 AST 解析，非 SELECT / 含写操作 / 访问非白名单对象直接拒绝（§5.3） |
| **版本一致性** | 查工艺相关数据带 `route_version` 过滤；查历史按当时版本回放 | 语义视图含 `route_version` 维度，查询工艺数据强制版本入参（§6.3） |
| **权限隔离** | 查询带 `tenant_scope`（车间/产线）前置过滤，不是查完再裁剪 | 语义视图带 `tenant_scope` 列，SQL 生成时注入租户过滤条件（§6.2） |
| **旁路解耦** | C 与 A/B/D 解耦，独立给管理层入口，互不影响 | C 服务独立部署，不订阅过点事件、不依赖 A/B 就绪（§1.4） |
| **可观测兜底** | 每个答案带生成 SQL + 执行计划 + 置信度；低置信度转人工 | SQL 审计落库 + 置信度阈值；与 MES 防错理念一致：宁可让人判 |
| **不进过点主事务** | C 是管理层离线查询，与过点执行完全无关 | C 查询走只读副本，不碰过点主库写路径（§5.4） |

### 1.3 与文档型、追溯型、防错即时辅助 RAG 的关系

| 路线 | 答案来源 | 形态 | 用户 | 与 C 的关系 |
|------|---------|------|------|-------------|
| **A 追溯型** | 追溯链（图） | GraphRAG + 一次综合 | 工艺/质量工程师 | 互补：C 答"数据是多少"，A 答"为什么是这个数据" |
| **B 文档型** | SOP/手册/标准 | 向量检索 + 引用 | 设备工程师/操作工 | 互补：C 答结构化数据，B 答处置知识 |
| **D 防错即时辅助** | 拦截事件 + SOP | 事件驱动 + 推送 | 操作工 | 互补：C 是管理层主动查，D 是操作工被动收 |
| **C 数据型（本文）** | MES 业务库 | Text2SQL + 图表 | 管理层/班长 | 独立旁路 |

- **C 是旁路**：[RAG服务引入路线.md](../RAG服务引入路线.md) §3 明确"C 旁路并行，和 B 解耦，互不影响"。C 不依赖 A/B/D 就绪，可独立先行或并行。
- **C 不替代 A**：管理层问"昨天 OEE 多少"走 C（Text2SQL）；工程师问"这条单件为什么不良"走 A（追溯图）。数据查询与根因诊断是两类问题，不能一个入口硬塞。

### 1.4 与 Java 技术栈的关系

- C 服务用 Python，**不替换** MES 三大服务的 Java/Spring 栈--只用**只读数据库账号**直连 MES 业务库只读副本，或调只读 REST。
- 跨语言物理边界 + 只读账号双保险：C 服务无法共享 Java 事务/内存，DB 账号级只读，双重强制不写 MES。
- **只读副本**：C 查询走 MES 业务库的只读副本（读多写少场景的标准做法），不增加主库读压力、不碰过点主库写路径。

---

## 2. 技术栈

### 2.1 选型总览

| 层次 | 选型 | 选型理由 |
|------|------|---------|
| 语言 | Python 3.11+ | 类型提示 + Pydantic，AI 生态最成熟，与 A/B/D 同栈 |
| Web 框架 | **FastAPI** | 异步、原生 OpenAPI，查询 HTTP 入口 + OpenAPI 文档 |
| SQL 引擎 | **SQLAlchemy 2.0 (async) + asyncmy** | 语义视图定义 + SQL 执行；只读账号连接 |
| SQL 解析校验 | **sqlglot** | AST 解析，校验 SQL 是否纯 SELECT + 只访问白名单视图（§5.3） |
| LLM 抽象 | `langchain-core` 的 `BaseChatModel` | 模型可插拔，与 A/B/D 一致 |
| 检索编排 | **LlamaIndex**（NLSQLTableQueryEngine 封装） | 自然语言 -> SQL 的上层抽象，可定制 schema 上下文 |
| 数据校验 | **Pydantic v2** | 查询请求/SQL 审计/图表 DTO 的 schema 即类型 |
| 缓存 | **redis-py (async)** | 相同查询短缓存（同问题重复查不重跑 SQL/LLM） |
| 图表 | **前端渲染**（C 只产数据 + 图表配置 JSON） | C 不渲染 UI，产数据 + 图表类型/配置，前端 ECharts/AntV 渲染 |
| 可观测 | **OpenTelemetry Python** + `prometheus-client` | trace 串联、指标告警 |
| 配置 | pydantic-settings | 环境变量统一管理 |
| 部署 | 独立微服务 `data-rag-service`（uvicorn + gunicorn worker） | 与三大服务同网格，K8s 部署 |

### 2.2 为什么是"语义层"而非"直接 Text2SQL 原始表"

- **原始表太复杂、LLM 易错**：MES 业务库有几十张表、复杂外键、版本字段（`route_version`/`bom_version`/`rule_version`）、租户字段（`tenant_scope`）。LLM 直接在原始表上生成 SQL，准确率低且容易漏掉版本/租户过滤--漏掉 `route_version` 会把已失效工艺的数据混进来，漏掉 `tenant_scope` 会跨车间越权。
- **语义层固化业务概念**：本 MES 已有 14 个限界上下文（[领域总览.md](../../领域模型/领域总览.md) §2），正好按上下文把"工单进度""在制品位置""缺陷统计"这些**业务概念**固化成视图，视图内部已经处理了版本/租户/多表关联，LLM 只需在视图上写简单 SELECT--准确率高、安全。
- **视图是 ACL**：语义视图本身就是一道防腐层--LLM 看不到原始表，只能访问视图，视图内部强制注入 `tenant_scope` 过滤与 `route_version` 维度。这比"让 LLM 生成 SQL 再事后裁剪"安全得多。

### 2.3 为什么多层校验而非单靠提示词

- **提示词不可靠**：告诉 LLM"只能 SELECT"它仍可能生成写操作或访问非白名单表。MES 领域不能赌模型听话。
- **三层防线**：① **DB 级**--只读账号，DDL/DML 在数据库层被拒；② **应用级**--表白名单，SQL 访问非白名单对象直接拒绝；③ **语义级**--AST 校验，非纯 SELECT / 含子查询写操作 / 含危险函数直接拒绝。三层任一拦截即失败，不依赖单点。
- **与 MES 防错理念一致**：宁可多层兜底让人判，不可错放一条写操作。

### 2.4 部署形态（车间网隔离）

- 车间网络常与办公网隔离（[RAG服务引入路线.md](../RAG服务引入路线.md) §4）。管理层查询通常在办公网，C 服务部署在办公网侧，经只读副本查 MES 业务库。
- **LLM 部署**：视安全策略二选一（云端 API 或本地化模型），`BaseChatModel` 抽象保证切换零代码改动。

---

## 3. 总体架构

```text
┌──────────────────────────────────────────────────────────────────┐
│ data-rag-service（独立微服务，Python + FastAPI + SQLAlchemy）      │
│                                                                    │
│  ┌──────────────┐  ┌──────────────────────────────────────────┐  │
│  │ FastAPI      │─▶│ QueryOrchestrator                          │  │
│  │ /rag/data/*  │  │  意图分类 -> 视图选择 -> SQL 生成 -> 校验 -> 执行 │  │
│  └──────────────┘  └────────────┬─────────────────────────────┘  │
│                                 │                                  │
│         ┌───────────────────────┼───────────────────────┐          │
│         ▼                       ▼                       ▼          │
│  ┌──────────────┐    ┌──────────────────┐    ┌──────────────┐    │
│  │ IntentClassifier│ │ SqlGenerator      │    │ SqlValidator │    │
│  │ NL -> 意图/视图  │ │ LLM 在视图上生 SQL │    │ AST+白名单校验 │    │
│  └──────────────┘    └────────┬─────────┘    └──────┬───────┘    │
│                               │                     │            │
│                       ┌───────▼─────────┐  ┌────────▼────────┐   │
│                       │ SemanticLayer   │  │ 只读执行 + 图表   │   │
│                       │ 14 上下文语义视图 │  │ ReadOnlyExecutor │   │
│                       └─────────────────┘  └────────┬────────┘   │
│                                                     │            │
│  ┌──────────────────┐  ┌──────────────────┐         │            │
│  │ SqlAudit (MySQL) │  │ QueryCache(Redis)│         │            │
│  └──────────────────┘  └──────────────────┘         │            │
└─────────────────────────────────────────────────────┼────────────┘
                                                      │ 只读账号 SELECT
                                          ┌───────────▼───────────┐
                                          │ MES 业务库只读副本     │
                                          │ （Java/Spring 写主库）  │
                                          └───────────────────────┘
```

### 3.1 关键设计决策

- **语义层即 ACL**：`SemanticLayer` 是 LLM 可见的全部 schema，原始表对 LLM 不可见。视图内部处理版本/租户/多表关联，LLM 只写简单 SELECT。
- **生成与校验分离**：`SqlGenerator`（LLM 生成 SQL）与 `SqlValidator`（AST + 白名单校验）解耦，校验失败可重试或转人工--单一职责（SRP）。
- **只读执行**：`ReadOnlyExecutor` 用只读账号执行校验通过的 SQL，结果转图表配置 JSON 返回前端。
- **审计可回溯**：每条查询的 NL 问题、生成 SQL、执行计划、结果摘要落 `SqlAudit`，工程师可回溯"这个 OEE 数字怎么来的"。

---

## 4. 语义层设计：对齐 14 个限界上下文

语义层是 C 的核心--把 14 个限界上下文的业务概念固化成视图，LLM 在视图上生成 SQL。视图严格对齐 [领域总览.md](../../领域模型/领域总览.md) §2 的限界上下文边界，不另造分类。

### 4.1 语义视图（按上下文固化）

每个视图带统一列：`tenant_scope`（车间/产线，权限过滤用）、`route_version`（工艺版本，版本过滤用，仅工艺相关视图）、`occurred_at`（时间维度）。

| 语义视图 | 来源上下文 | 固化的业务概念 | 关键列 | 版本维度 |
|---------|-----------|--------------|--------|---------|
| `v_work_order_progress` | 工单管理 + 过点执行 | 工单进度（完成/良品/不良/返修量） | work_order_id, status, completed_qty, good_qty, bad_qty, reworked_qty | route_version |
| `v_wip_position` | 在制品追踪 + 过点执行 | 在制品当前位置与状态 | sn, work_order_id, current_station, status, position | route_version |
| `v_checkpoint_history` | 过点执行 | 过点记录历史（放行/拦截） | sn, station_id, decision, blocking_reason, scanned_by, occurred_at | route_version |
| `v_defect_statistics` | 质量 | 缺陷统计（按缺陷码/工位/工单） | defect_code, defect_name, severity, count, work_order_id, station_id | rule_version |
| `v_quality_verdict_history` | 质量 | 质量业务判定历史 | verdict_id, sn, business_verdict, defect_records, station_id | rule_version |
| `v_batch_anomaly` | 质量 | 批量质量异常 | anomaly_id, work_order_id, defect_code, affected_count, scope | - |
| `v_route_version_active` | 工艺管理 | 当前生效工艺版本 | route_id, route_version, route_type, status, activated_at | route_version |
| `v_asset_availability` | 设备工装台账 + 点检保养 | 设备可用性与锁定原因 | asset_id, asset_kind, available, blocking_reasons, tenant_scope | - |
| `v_repair_order` | 维修 | 维修工单统计 | order_id, asset_id, severity, status, created_at | - |
| `v_kit_status` | 工单管理 | 工单齐套状态 | work_order_id, kit_ready, missing_items | - |
| `v_first_article_status` | 首件处理 | 首件放行状态 | work_order_id, status, progress | - |
| `v_rework_task` | 返修 | 返修任务统计 | task_id, sn, defect_reason, status, source_station | - |

> **视图即业务概念**：管理层问"工单 WO-1234 进度"-> LLM 在 `v_work_order_progress` 上生成 `SELECT * FROM v_work_order_progress WHERE work_order_id='WO-1234'`，视图内部已关联工单管理 + 过点执行的原始表、已处理版本/租户。LLM 不需知道底层几张表。

### 4.2 视图内部强制注入版本与租户

视图定义内部强制带 `tenant_scope` 与 `route_version` 维度，LLM 生成的 SQL 即使忘加过滤，视图也暴露这些列供校验层补全：

```sql
-- 示例：工单进度视图（内部关联工单管理 + 过点执行原始表，暴露版本/租户维度）
CREATE VIEW v_work_order_progress AS
SELECT
  wo.work_order_id, wo.status, wo.target_qty, wo.product_id,
  wo.route_id, wo.route_version,                  -- 版本维度（工单下达时锁定）
  wo.tenant_scope,                                 -- 租户维度（权限过滤）
  wop.completed_qty, wop.good_qty, wop.bad_qty, wop.reworked_qty,
  wo.updated_at
FROM work_order wo
LEFT JOIN work_order_progress wop ON wo.work_order_id = wop.work_order_id
WHERE wo.status NOT IN ('CANCELLED');
```

- **`tenant_scope` 必现**：所有视图都带租户列，校验层强制 SQL 注入 `WHERE tenant_scope IN (...)`（§6.2）。
- **`route_version` 必现（工艺相关视图）**：查工艺数据时校验层强制版本入参，不取"当前生效版"除非用户明确问当前（§6.3）。

### 4.3 视图对 LLM 可见，原始表不可见

- **LLM 的 schema 上下文**：`SemanticLayer` 只把视图的列定义与业务描述喂给 LLM，原始表（`work_order`/`checkpoint_record`/...）不出现在 schema 上下文里。
- **DB 权限**：只读账号仅授视图 SELECT 权限，原始表不授权--即使 LLM 生成访问原始表的 SQL，DB 层也会拒绝（§5.2 双保险）。

---

## 5. SQL 生成与校验：多层防线

### 5.1 SQL 生成（SqlGenerator）

```text
QueryOrchestrator.generate_sql(question, intent, tenant)
   │
   ├─ 1. 意图分类（IntentClassifier）：NL -> 命中哪个语义视图 + 查询意图（聚合/明细/趋势）
   ├─ 2. 构造 schema 上下文：只喂命中视图的列定义 + 业务描述（不喂全量 schema）
   ├─ 3. LLM 生成 SQL（with_structured_output(SqlResult)）
   │     系统提示词约束：只在视图上 SELECT、带 tenant_scope 过滤、工艺数据带 route_version、禁写
   └─ 4. 返回待校验 SQL
```

- **意图分类优先规则/向量**：高频问题（"X 号线 OEE""工单进度"）走规则匹配命中视图，命中不了再走 LLM 意图分类。降低对模型依赖。
- **schema 上下文裁剪**：只喂命中视图的 schema，不喂全 12 个视图--减少 LLM 混淆与 token 消耗。
- **LLM 只生成 SQL，不执行**：生成的 SQL 必须经 `SqlValidator` 校验 + `ReadOnlyExecutor` 执行，LLM 无权直接碰库。

### 5.2 只读账号（DB 级防线）

```sql
-- 只读账号：仅授视图 SELECT，原始表与写操作全部拒绝
CREATE USER 'data_rag_ro'@'%' IDENTIFIED BY '***';
GRANT SELECT ON mes_readonly.v_work_order_progress TO 'data_rag_ro'@'%';
GRANT SELECT ON mes_readonly.v_wip_position TO 'data_rag_ro'@'%';
-- ... 仅授语义视图 SELECT
-- 不授原始表、不授 INSERT/UPDATE/DELETE/DDL
REVOKE ALL PRIVILEGES ON mes.* FROM 'data_rag_ro'@'%';
```

- **DB 级兜底**：即使应用层校验全失效，只读账号也无法写 MES--数据库层直接拒绝 DDL/DML。
- **只读副本**：C 连接 MES 业务库的只读副本，不碰主库写路径，不增加过点主库压力。

### 5.3 AST + 白名单校验（应用级 + 语义级防线）

`SqlValidator` 用 sqlglot 解析 SQL AST，三层校验：

```python
class SqlValidator:
    """校验生成的 SQL：纯 SELECT + 白名单视图 + 无危险操作。"""

    ALLOWED_VIEWS = {
        "v_work_order_progress", "v_wip_position", "v_checkpoint_history",
        "v_defect_statistics", "v_quality_verdict_history", "v_batch_anomaly",
        "v_route_version_active", "v_asset_availability", "v_repair_order",
        "v_kit_status", "v_first_article_status", "v_rework_task",
    }

    def validate(self, sql: str, tenant: TenantContext) -> str:
        ast = sqlglot.parse_one(sql, dialect="mysql")
        # 1. 语义级：必须是 SELECT（非 INSERT/UPDATE/DELETE/DDL）
        if not isinstance(ast, exp.Select):
            raise SqlValidationError("仅允许 SELECT")
        # 2. 应用级：访问的对象必须在白名单视图内
        for tbl in ast.find_all(exp.Table):
            if tbl.name not in self.ALLOWED_VIEWS:
                raise SqlValidationError(f"禁止访问非白名单对象: {tbl.name}")
        # 3. 语义级：禁止危险函数 / 子查询写操作 / INTO OUTFILE 等
        if ast.find(exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.IntoOutfile):
            raise SqlValidationError("禁止写操作或导出")
        # 4. 强制注入 tenant_scope 过滤（§6.2）
        return self._inject_tenant_filter(ast, tenant)
```

- **AST 解析防绕过**：不靠正则匹配关键字（易绕过），而是解析 AST 结构，识别真实语义。
- **白名单视图**：LLM 只能访问 §4.1 的 12 个语义视图，原始表与系统表全拒绝。
- **强制租户过滤**：校验通过后注入 `WHERE tenant_scope IN (...)`，权限不达标查不到数据（§6.2）。

---

## 6. 权限、版本一致性与租户隔离

### 6.1 查询请求与租户上下文

```python
class DataQuery(BaseModel):
    question: str                       # 自然语言问题
    as_of: datetime | None = None       # 时间窗（"截至昨天"）
    route_version: str | None = None    # 工艺版本（查历史时指定）
    tenant: TenantContext               # 租户上下文（从 token 解析）
```

- 租户上下文从 token 解析，注入查询链路全程。

### 6.2 租户隔离（前置过滤）

- `SqlValidator._inject_tenant_filter` 在 SQL AST 上强制注入 `WHERE tenant_scope IN ($tenant.scopes)`，权限不达标查不到数据。
- **不是查完再裁剪**：在 SQL 执行前就注入过滤，DB 只返回权限内数据--既安全又省传输。

### 6.3 版本一致性

- **查历史工艺数据**：用户指定 `route_version`（或从 `CheckpointRecord.route_version` 取当时版本），视图按版本过滤，不取"当前生效版"。
- **查当前工艺**：用户明确问"当前"时，走 `v_route_version_active`（`status=ACTIVATED`）过滤。
- **版本一致性从领域模型兜上来**：过点记录绑 `routeVersion`（[领域总览.md](../../领域模型/领域总览.md) §5.1），工艺/BOM/规则有版本生命周期。C 只是严格遵循这套契约，查工艺数据强制版本维度。

> 这条要讲清楚：**Text2SQL 也要版本一致性**。漏掉 `route_version` 会把已失效工艺的数据混进报表，误导管理层决策。语义视图暴露 `route_version` 维度，校验层强制工艺相关查询带版本过滤--和 A/B/D 共享同一套版本契约。

---

## 7. 实现方案

### 7.1 查询编排（QueryOrchestrator）

```python
class QueryOrchestrator:
    def __init__(
        self,
        intent_classifier: IntentClassifier,
        generator: SqlGenerator,
        validator: SqlValidator,
        executor: ReadOnlyExecutor,
        cache: QueryCache,
        audit_repo: SqlAuditRepo,
    ) -> None: ...

    async def query(self, request: DataQuery) -> DataAnswer:
        # 1. 查询缓存（同问题 + as_of + 租户命中即用）
        cached = await self._cache.get(request)
        if cached:
            return cached
        # 2. 意图分类：NL -> 命中视图 + 查询意图
        intent = await self._intent_classifier.classify(request.question)
        # 3. LLM 生成 SQL（在视图上）
        sql = await self._generator.generate(request.question, intent, request.tenant)
        # 4. 校验（AST + 白名单 + 注入租户过滤）
        validated_sql = self._validator.validate(sql, request.tenant)
        # 5. 只读执行
        rows, latency_ms = await self._executor.execute(validated_sql)
        # 6. 转图表配置 + 答案
        answer = self._build_answer(request, intent, validated_sql, rows, latency_ms)
        # 7. 审计落库 + 缓存
        await self._audit_repo.record(request, validated_sql, answer)
        await self._cache.set(request, answer)
        return answer
```

- 编排与各步骤分离：分类/生成/校验/执行各司其职（SRP）。
- 缓存按"问题 + as_of + 租户"键，同问题重复查不重跑 SQL/LLM。

### 7.2 只读执行器（ReadOnlyExecutor）

```python
class ReadOnlyExecutor:
    """用只读账号执行校验通过的 SQL，带超时与行数限制。"""

    def __init__(self, engine: AsyncEngine, settings: Settings) -> None:
        self._engine = engine; self._settings = settings

    async def execute(self, sql: str) -> tuple[list[dict], int]:
        async with self._engine.connect() as conn:
            result = await conn.execute(text(sql))
            rows = result.fetchmany(self._settings.max_rows)  # 行数限制
            return [dict(r._mapping) for r in rows], len(rows)
```

- **只读连接**：`engine` 用只读账号，连接只读副本。
- **行数限制**：`max_rows` 防止全表扫描拖垮副本。
- **超时**：SQL 执行超时直接失败，不长时间占用连接。

### 7.3 ACL 防腐层（语义视图定义）

```python
class SemanticLayer:
    """语义视图注册表：视图列定义 + 业务描述，喂给 LLM 的 schema 上下文。"""

    VIEWS: dict[str, ViewSchema] = {
        "v_work_order_progress": ViewSchema(
            name="v_work_order_progress",
            description="工单执行进度：完成量/良品/不良/返修量，按工单维度",
            columns=[
                Column("work_order_id", "工单号"), Column("status", "工单状态"),
                Column("completed_qty", "完成量"), Column("good_qty", "良品量"),
                Column("bad_qty", "不良量"), Column("route_version", "工艺版本"),
                Column("tenant_scope", "租户(车间/产线)"),
            ],
            examples=["WO-1234 现在到哪站", "昨天 SMT1 号线完工量"],
        ),
        # ... 其余 11 个视图
    }

    def schema_for(self, view_names: list[str]) -> str:
        """构造 LLM schema 上下文（只喂命中视图）。"""
        ...
```

- 视图定义是 C 的"领域模型"，对齐 14 上下文（ISP），每个视图独立描述。
- LLM 只看到命中视图的 schema，不是全量--减少混淆。

---

## 8. 推荐包结构（Python src layout）

```text
data_rag_service/
  app/
    api/
      query_router.py          # /rag/data/query, /rag/data/explain
      schemas.py
    application/
      query_orchestrator.py    # QueryOrchestrator
      intent_classifier.py     # NL -> 意图/视图（规则优先 + LLM 兜底）
    domain/
      semantic_layer.py        # SemanticLayer / ViewSchema / Column
      sql_result.py            # DataAnswer / SqlIntent / ChartConfig
      tenant.py                # TenantContext
      audit.py                 # SqlAudit
    infrastructure/
      sql/
        generator.py           # SqlGenerator（LLM 生成 SQL）
        validator.py           # SqlValidator（AST + 白名单 + 租户注入）
        executor.py            # ReadOnlyExecutor（只读账号执行）
        engine.py              # AsyncEngine（只读副本连接）
      ai/
        llm_factory.py
      acl/                     # 各上下文只读 REST（降级查询，可选）
        work_order.py
        quality.py
      persistence/
        models.py              # sql_audit
        audit_repo.py
      redis_/
        query_cache.py
      obs/
        tracing.py
        metrics.py
    config.py
    main.py                    # FastAPI app + lifespan 启动断言
  tests/
  pyproject.toml
```

- `domain/semantic_layer.SemanticLayer` 是视图注册表，对齐 14 上下文（ISP）。
- `infrastructure/sql/` 三件套分离：生成/校验/执行各一个类（SRP）。
- `infrastructure/acl/` 是可选降级查询（视图未覆盖时调只读 REST 补齐）。

---

## 9. 关键代码骨架

### 9.1 意图分类器（NL -> 视图）

```python
class IntentClassifier:
    """NL -> 命中语义视图 + 查询意图。规则优先，LLM 兜底。"""

    def __init__(self, llm: BaseChatModel, semantic: SemanticLayer) -> None:
        self._llm = llm; self._semantic = semantic

    async def classify(self, question: str) -> SqlIntent:
        # 1. 规则优先：关键词命中视图
        if any(k in question for k in ["OEE", "完工", "进度", "工单"]):
            return SqlIntent(view="v_work_order_progress", agg="summary")
        if any(k in question for k in ["缺陷", "TOP", "不良"]):
            return SqlIntent(view="v_defect_statistics", agg="rank")
        if any(k in question for k in ["在制品", "位置", "到哪站"]):
            return SqlIntent(view="v_wip_position", agg="detail")
        # 2. LLM 兜底分类
        return await self._llm.with_structured_output(SqlIntent).ainvoke(
            f"从以下问题判断命中哪个语义视图与查询意图：\n{question}\n可选视图：{self._semantic.view_list()}"
        )
```

- 规则优先降低对模型依赖，高频问题不走 LLM。

### 9.2 SQL 生成器

```python
class SqlGenerator:
    """LLM 在语义视图上生成 SELECT SQL。"""

    def __init__(self, llm: BaseChatModel, semantic: SemanticLayer) -> None:
        self._llm = llm; self._semantic = semantic

    async def generate(self, question: str, intent: SqlIntent, tenant: TenantContext) -> str:
        schema_ctx = self._semantic.schema_for([intent.view])
        prompt = f"""
你是 MES 数据查询助手。只能在语义视图上生成 SELECT 语句。
规则：
1. 只能 SELECT，禁止 INSERT/UPDATE/DELETE/DDL
2. 只能访问给定的语义视图，禁止访问原始表
3. 必须带 tenant_scope 过滤（当前租户：{tenant.scopes}）
4. 工艺相关查询必须带 route_version 维度
5. 用参数化值，不要拼接用户输入

{schema_ctx}

问题：{question}
生成 SQL：
"""
        result = await self._llm.with_structured_output(SqlDraft).ainvoke(prompt)
        return result.sql
```

- 系统提示词强约束：只 SELECT、只视图、带租户/版本过滤。
- LLM 只生成 SQL，不执行。

### 9.3 SQL 校验器（AST + 白名单 + 租户注入）

```python
import sqlglot
from sqlglot import exp

class SqlValidator:
    ALLOWED_VIEWS = {  # 12 个语义视图白名单
        "v_work_order_progress", "v_wip_position", "v_checkpoint_history",
        "v_defect_statistics", "v_quality_verdict_history", "v_batch_anomaly",
        "v_route_version_active", "v_asset_availability", "v_repair_order",
        "v_kit_status", "v_first_article_status", "v_rework_task",
    }

    def validate(self, sql: str, tenant: TenantContext) -> str:
        ast = sqlglot.parse_one(sql, dialect="mysql")
        # 1. 必须 SELECT
        if not isinstance(ast, exp.Select):
            raise SqlValidationError("仅允许 SELECT")
        # 2. 白名单视图
        for tbl in ast.find_all(exp.Table):
            if tbl.name not in self.ALLOWED_VIEWS:
                raise SqlValidationError(f"禁止访问非白名单对象: {tbl.name}")
        # 3. 禁止危险操作
        if ast.find(exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.IntoOutfile):
            raise SqlValidationError("禁止写操作或导出")
        # 4. 强制注入 tenant_scope 过滤
        return self._inject_tenant(ast, tenant).sql(dialect="mysql")

    def _inject_tenant(self, ast: exp.Select, tenant: TenantContext) -> exp.Select:
        # 在 WHERE 注入 tenant_scope IN (...)
        ...
```

### 9.4 启动断言（只读校验）

```python
class ReadOnlyAccountGate(Exception):
    """启动时发现数据库账号非只读，拒绝启动。"""

@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = app.state.engine
    # 启动断言：校验 DB 账号只有视图 SELECT 权限，无写权限
    await assert_read_only_account(engine)   # 查权限表，确认无 INSERT/UPDATE/DELETE/DDL
    # 启动断言：语义视图全部存在且可查
    await assert_views_exist(engine, SemanticLayer.VIEWS.keys())
    yield
```

- `assert_read_only_account` 启动时校验 DB 账号权限，非只读直接拒绝启动--红线靠启动断言兜底。

### 9.5 FastAPI 入口

```python
router = APIRouter(prefix="/rag/data", tags=["data-rag"])

@router.post("/query", response_model=DataAnswer)
async def query(
    req: DataQuery,
    tenant: TenantContext = Depends(tenant_from_token),
    svc: QueryOrchestrator = Depends(get_orchestrator),
) -> DataAnswer:
    return await svc.query(req)

@router.get("/explain/{audit_id}")
async def explain(audit_id: str, tenant: TenantContext = Depends(tenant_from_token)) -> SqlAudit:
    """回溯某次查询的生成 SQL 与执行计划（可观测）。"""
    ...
```

- `/query` 给管理层问答；`/explain` 回溯查询 SQL（审计可回溯）。

---

## 10. 可观测性与兜底

### 10.1 指标（prometheus-client）

| 指标 | 含义 |
|------|------|
| `data_rag_query_total` | 查询次数（按 view label） |
| `data_rag_sql_gen_latency_seconds` | SQL 生成延迟（LLM，Histogram） |
| `data_rag_sql_exec_latency_seconds` | SQL 执行延迟（DB，Histogram） |
| `data_rag_validation_rejected_total` | 校验拒绝次数（按原因 label：非SELECT/非白名单/危险操作） |
| `data_rag_cache_hit_total` | 查询缓存命中 |
| `data_rag_low_confidence_total` | 低置信度转人工次数 |
| `data_rag_exec_timeout_total` | SQL 执行超时次数 |
| `data_rag_row_limit_hit_total` | 结果达行数上限次数 |

### 10.2 trace 串联

- 每次查询一个 `trace_id`，OpenTelemetry 在 `IntentClassifier`、`SqlGenerator`、`SqlValidator`、`ReadOnlyExecutor` 都注入 span。
- `SqlAudit` 记录 NL 问题、生成 SQL、执行计划、结果摘要、`trace_id`--工程师可从答案回溯"这个数字怎么来的"。

### 10.3 兜底

- **校验失败兜底**：SQL 校验拒绝 -> 重试 1 次（带拒绝原因反馈 LLM）；仍失败转人工，返回"无法生成安全查询，请联系数据工程师"。
- **执行超时/行数上限兜底**：SQL 超时或达行数上限 -> 返回部分结果 + 提示"结果较大，请缩小查询范围"。
- **低置信度兜底**：LLM 生成 SQL 置信度低 -> `needs_human_review=True`，标注"建议人工核对"。
- **视图未覆盖兜底**：意图分类命中不了任何视图 -> 转人工，返回"该问题暂不支持，请联系数据工程师扩展语义层"。
- **只读账号兜底**：即使应用层校验全失效，DB 级只读账号也拒绝任何写操作。

---

## 11. 实现步骤

### 阶段一：骨架与语义层（2 周）

1. 搭 `data_rag_service` 骨架（FastAPI + uvicorn），对齐 §8 包结构。
2. 定义 12 个语义视图 DDL，在只读副本上创建（§4.1）。
3. 实现只读账号 + 启动断言 `assert_read_only_account`（§9.4）。
4. 实现 `SemanticLayer` 视图注册表（§7.3）。

### 阶段二：SQL 生成与校验（2 周）

5. 实现 `IntentClassifier`（规则优先 + LLM 兜底）（§9.1）。
6. 实现 `SqlGenerator`（LLM 在视图上生成 SQL）（§9.2）。
7. 实现 `SqlValidator`（AST + 白名单 + 租户注入）（§9.3）。
8. 实现 `ReadOnlyExecutor`（只读账号执行 + 行数/超时限制）（§7.2）。

### 阶段三：编排与可观测（1-2 周）

9. 实现 `QueryOrchestrator` 编排链路（§7.1）。
10. 实现 `SqlAudit` 审计落库 + `/explain` 回溯端点（§9.5）。
11. 实现 `QueryCache`（同问题缓存）。
12. 接 OpenTelemetry + prometheus 指标（§10.1）。

### 阶段四：加固、评测与试点（1 周）

13. 沉淀评测集（典型管理层问题 + 预期 SQL/结果），回归模型/提示词变更。
14. 校验链路全测（非 SELECT/非白名单/危险操作/超时/行数上限/低置信度）。
15. 灰度一个管理层场景（如工单进度查询）试点，收集反馈。
16. 确认 🔴 决策点（语义视图覆盖范围、图表类型、只读副本部署）。

---

## 12. 约束落地检查清单

- [ ] LLM 只在语义视图上生成 SQL，原始表对 LLM 不可见（§4.3）。
- [ ] 只读账号仅授视图 SELECT，DDL/DML 在 DB 级被拒；`assert_read_only_account` 启动断言生效（§5.2、§9.4）。
- [ ] `SqlValidator` AST 校验：纯 SELECT + 白名单视图 + 无危险操作；非 SELECT/非白名单/写操作直接拒绝（§5.3）。
- [ ] 校验通过后强制注入 `tenant_scope` 过滤，权限不达标查不到数据（§6.2）。
- [ ] 工艺相关查询带 `route_version` 维度，查历史按当时版本，不取"当前生效版"除非明确问当前（§6.3）。
- [ ] C 服务不进过点主事务，查询走只读副本，不碰主库写路径（§5.4）。
- [ ] SQL 执行带超时与行数限制，防止全表扫描拖垮副本（§7.2）。
- [ ] 每条查询审计落库（NL/SQL/执行计划/结果摘要/trace_id），`/explain` 可回溯（§9.5）。
- [ ] 校验失败/低置信度/超时/视图未覆盖 -> 转人工兜底，不硬答（§10.3）。
- [ ] C 服务与 A/B/D 解耦，独立部署，不依赖 A/B 就绪（§1.3）。

---

## 13. 面试防守 Q&A

**Q：为什么不让 LLM 直接查 MES 数据库，要费劲建语义层？**
A：两个原因。一是原始表太复杂、LLM 易错--MES 有几十张表、复杂外键、版本字段（`route_version`/`bom_version`/`rule_version`）、租户字段（`tenant_scope`）。LLM 直接在原始表上生成 SQL，漏掉 `route_version` 会把已失效工艺的数据混进报表，漏掉 `tenant_scope` 会跨车间越权。二是语义层把业务概念固化成视图--我已有 14 个限界上下文（[领域总览.md](../../领域模型/领域总览.md) §2），正好按上下文把"工单进度""在制品位置""缺陷统计"固化成视图，视图内部已处理版本/租户/多表关联，LLM 只需在视图上写简单 SELECT。语义层本身就是 ACL--LLM 看不到原始表，视图内部强制注入过滤，比事后裁剪安全得多（[RAG服务引入路线.md](../RAG服务引入路线.md) §5 Q&A）。

**Q：怎么保证 LLM 不会生成写操作或访问敏感表？**
A：三层防线，不靠单点。① DB 级--只读账号仅授视图 SELECT，原始表不授权，DDL/DML 在数据库层直接被拒；② 应用级--表白名单，SQL 访问非白名单视图直接拒绝；③ 语义级--用 sqlglot 解析 AST，非纯 SELECT / 含子查询写操作 / 含危险函数（`INTO OUTFILE` 等）直接拒绝。三层任一拦截即失败。启动时还有 `assert_read_only_account` 断言校验 DB 账号权限，非只读直接拒绝启动。提示词告诉 LLM"只能 SELECT"不可靠，MES 领域不能赌模型听话--与防错理念一致，多层兜底。

**Q：Text2SQL 也要版本一致性？这不是文档/追溯 RAG 才管的吗？**
A：要。漏掉 `route_version` 会把已失效工艺的数据混进报表，误导管理层决策。比如问"上周 SMT1 号线 OEE"，如果不带版本过滤，可能把工艺升版前后的数据混在一起，OEE 失真。语义视图暴露 `route_version` 维度，校验层强制工艺相关查询带版本过滤--查历史按当时版本（过点记录绑 `routeVersion`，§5.1），查当前走 `status=ACTIVATED`。版本一致性不是哪条 RAG 路线自己保证的，是从领域模型兜上来的，A/B/C/D 共享同一套契约。

**Q：不同车间能看的数据不一样怎么管？**
A：查询带 `tenant_scope` 前置过滤，不是查完再裁剪。`SqlValidator` 校验通过后在 SQL AST 上强制注入 `WHERE tenant_scope IN ($tenant.scopes)`，DB 只返回权限内数据。租户上下文从 token 解析，注入查询链路全程。本 MES 的 14 个限界上下文边界本身就是天然的权限切分面，语义视图按上下文分区，权限跟着上下文走。

**Q：和管理层说"昨天 OEE 是 85%"，这个数字怎么来的？可信吗？**
A：每个答案都可回溯。`SqlAudit` 记录 NL 问题、生成 SQL、执行计划、结果摘要、`trace_id`，`/explain/{audit_id}` 端点让工程师回溯"这个 85% 是哪条 SQL 算出来的、查了哪个视图、什么时间窗"。低置信度查询标注"建议人工核对"。C 不硬答不可信的数字--校验失败/低置信度/超时都转人工。MES 领域对错误答案零容忍，给管理层错数据比不答更糟。

**Q：C 和 A/B/D 是什么关系？为什么旁路？**
A：C 是旁路，独立给管理层入口，和 B 解耦互不影响（[RAG服务引入路线.md](../RAG服务引入路线.md) §3）。四条路线答案来源完全不同：C 答"数据是多少"（业务库 Text2SQL），A 答"为什么是这个数据"（追溯图），B 答"怎么处置"（文档），D 答"现场拦了怎么办"（推送）。管理层问 OEE 走 C，工程师问根因走 A，不能一个入口硬塞。C 不依赖 A/B/D 就绪，可独立先行或并行。

**Q：LLM 生成的 SQL 准确率不够怎么办？**
A：三招提升准确率。一是语义层固化业务概念--LLM 在视图上写简单 SELECT 比在原始表上写复杂关联准确率高得多。二是意图分类优先规则匹配--高频问题（"X 号线 OEE""工单进度"）走规则命中视图，不走 LLM。三是 schema 上下文裁剪--只喂命中视图的 schema，不喂全量，减少 LLM 混淆。再叠加校验失败重试（带拒绝原因反馈 LLM），稳态准确率可控。仍不准的低置信度查询转人工，不硬答。

**Q：上线了吗？**
A：这是设计阶段规划，不是已落地。重点是三条架构判断：① 不让 LLM 直接碰原始表，在按 14 上下文固化的语义层上生成 SQL；② 三层校验防写（只读账号 DB 级 + 表白名单应用级 + AST 校验语义级），不靠提示词；③ 版本一致性与租户隔离从领域模型兜上来，查工艺数据带 `route_version`、查数据带 `tenant_scope` 前置过滤。C 是旁路，可与 A/B/D 并行，先做管理层高频查询场景验证可用性。诚实 + 体现架构判断力，比硬吹"已上线 Text2SQL"得分高。

---

## 14. 一句话定位

"数据型 RAG 把 MES 业务库做成自然语言查询--不让 LLM 直接碰原始表，而是按 14 个限界上下文把'工单进度''在制品位置''缺陷统计'固化成语义视图，LLM 只在视图上生成 SELECT。安全靠三层防线：只读账号 DB 级拒写、表白名单应用级拦截、AST 校验语义级防绕过，不靠提示词赌模型听话。版本一致性与租户隔离从领域模型兜上来--查工艺数据带 `route_version`、查数据带 `tenant_scope` 前置过滤。每个答案带生成 SQL 可回溯，低置信度转人工。C 是旁路，与 A/B/D 解耦独立并行，服务管理层结构化数据查询场景。"
