# 数据型 RAG / Text2SQL 实现方案（Python 技术栈：3 语义视图 MVP）

> 本文是 [RAG服务引入路线.md](../RAG服务引入路线.md) §2.3 路线 C（数据型 RAG / Text2SQL）的**实现层落地**，与 [数据型 RAG-详细设计.md](./数据型 RAG-详细设计.md) 的关系：
> - **详细设计**是全 12 语义视图的**设计层**（广）--语义层设计、SQL 生成校验、权限版本一致性的全景；
> - **本文**是 3 个语义视图（工单进度 + 在制品位置 + 缺陷统计 TOP）的**实现层**（深）--把详细设计的骨架补全到可落地的 MVP，新增**依赖清单、语义视图 DDL、只读账号 DDL、SQL 校验代码、Docker 部署、测试策略**等实现层内容，并对个别视图口径按各上下文落地细化（如缺陷统计的 `rule_version` 维度，§4.3 🔴）。
> 其余 9 个语义视图按 §11 相同范式扩展，MVP 不展开。
>
> **技术栈**：Python（FastAPI + SQLAlchemy + sqlglot + LlamaIndex + Pydantic）。RAG 服务与三大 MES 服务（Java/Spring）跨语言共存，通过**只读数据库账号 + 只读副本**解耦，互不侵入。
> **口径纪律**：数据型 RAG 在本项目中是**设计阶段的引入规划**，不是线上已落地能力。讲法遵循 [项目亮点与指标卡片.md](../../面试指南/项目亮点与指标卡片.md) §0 的口径纪律--说"规划方向 / 设计取舍"，不说"我们已经做了 Text2SQL"。MES 领域对错误答案零容忍，所以本文强调**只在语义层上生成 SQL + 多层校验防写 + 只读账号兜底**。

---

## 1. 设计目标与边界

### 1.1 目标（3 语义视图 MVP）

对 MES 业务库做**自然语言 -> SQL -> 图表/表格**，服务管理层/班长的结构化数据查询。**MVP 范围**：聚焦 3 个最高频语义视图，跑通"NL -> 意图分类 -> SQL 生成 -> 校验 -> 只读执行 -> 图表"闭环：

| 语义视图 | 来源上下文 | MVP 典型问题 | 产出 |
|---------|-----------|-------------|------|
| **`v_work_order_progress`**（工单进度） | 工单管理 + 过点执行 | "工单 WO-1234 现在到哪站""昨天 SMT1 号线完工量" | 进度表格 / 完工量柱状图 |
| **`v_wip_position`**（在制品位置） | 在制品追踪 + 过点执行 | "WO-1234 的在制品都在哪些工位""当前 SMT1 号线在制数" | 在制明细表 / 工位分布图 |
| **`v_defect_statistics`**（缺陷统计 TOP） | 质量 | "上周返修 TOP3 缺陷""WO-1234 的不良分布" | 缺陷帕累托图 / TOP 表 |

> 其余 9 个语义视图（过点历史、质量判定、批量异常、工艺版本、设备可用性、维修、齐套、首件、返修）按 §11 相同范式扩展，MVP 不展开。

典型场景："上周返修 TOP3 缺陷" -> LLM 在 `v_defect_statistics` 上生成 `SELECT defect_code, defect_name, count FROM v_defect_statistics WHERE ... ORDER BY count DESC LIMIT 3` -> 校验通过 -> 只读执行 -> 返回帕累托图配置 + 数据。

### 1.2 硬边界（一开口就要讲）

| 边界 | 说明 | 落地（MVP 具体动作） |
|------|------|----------------------|
| **只在语义层生成 SQL** | LLM 不直接碰原始表，只在 3 个语义视图上生成 SELECT | `SemanticLayer` 只暴露 3 视图 schema；原始表对 LLM 不可见（§4.3） |
| **只读账号兜底** | DB 账号级禁止写操作 | 只读账号 `data_rag_ro` 仅授 3 视图 SELECT；`assert_read_only_account` 启动断言（§5.2、§9.4） |
| **表白名单 + AST 校验** | 应用层 + 语义级校验 | `SqlValidator` 用 sqlglot 解析 AST，非 SELECT / 非白名单 / 危险操作拒绝（§5.3） |
| **版本一致性** | 查工艺数据带 `route_version` | 视图含 `route_version` 维度；工艺相关查询强制版本入参（§6.3） |
| **权限隔离** | 查询带 `tenant_scope` 前置过滤 | `SqlValidator` 强制注入 `WHERE tenant_scope IN (...)`（§6.2） |
| **旁路解耦** | C 独立于 A/B/D | C 服务独立部署，不订阅过点事件、不依赖 A/B（§1.3） |
| **不进过点主事务** | C 查询走只读副本 | 连只读副本，不碰主库写路径（§5.4） |
| **可观测兜底** | 每答案带 SQL + 审计 + 置信度 | `SqlAudit` 落库 + `/explain` 回溯；低置信度转人工（§10.3） |

### 1.3 与详细设计、A/B/D 的关系

- **与详细设计**：详细设计给全 12 视图全景与校验设计；本文把其中 3 个高频视图的 DDL、生成校验代码、只读账号补全到可落地，并新增实现层内容（依赖、Docker、测试）。
- **与 A/B/D**：C 是旁路，独立部署，不依赖 A/B/D 就绪（[RAG服务引入路线.md](../RAG服务引入路线.md) §3"C 旁路并行"）。C 不订阅过点事件、不调 A/B RAG，只连 MES 业务库只读副本。
- **与 L1 诊断型 Agent**：L1 是工程师侧多步推理（[L1诊断型Agent-实现方案.md](../../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md)），C 是管理层侧单步数据查询，分层不重复。L1 的 `query_data` 工具可封装 C 的 `/rag/data/query`。

### 1.4 与 Java 技术栈的关系

- C 服务用 Python，**不替换** MES 三大服务的 Java/Spring 栈--只用**只读数据库账号**直连 MES 业务库只读副本。
- 跨语言物理边界 + 只读账号双保险：C 服务无法共享 Java 事务/内存，DB 账号级只读，双重强制不写 MES。
- 复用 [实现说明](../../实现说明/) 既有的 MySQL 基础设施（[MySQL配置说明.md](../../实现说明/基础设施/MySQL配置说明.md)），C 连只读副本，不碰主库。

---

## 2. 技术栈

### 2.1 选型总览

| 层次 | 选型 | 版本 | 选型理由 |
|------|------|------|---------|
| 语言 | Python | 3.11+ | 类型提示 + Pydantic，与 A/B/D 同栈 |
| Web 框架 | **FastAPI** | 0.110+ | 异步、原生 OpenAPI，查询 HTTP 入口 |
| SQL 引擎 | **SQLAlchemy 2.0 (async) + asyncmy** | 2.0+ | 语义视图定义 + SQL 只读执行 |
| SQL 解析校验 | **sqlglot** | 23+ | AST 解析，校验纯 SELECT + 白名单 + 危险操作 |
| LLM 抽象 | `langchain-core` 的 `BaseChatModel` | 0.2+ | 模型可插拔，与 A/B/D 一致 |
| 检索编排 | **LlamaIndex**（NLSQLTableQueryEngine） | 0.10+ | NL -> SQL 上层抽象（MVP 可先用裸提示词） |
| 数据校验 | **Pydantic** | v2 | 查询请求/SQL 审计/图表 DTO schema 即类型 |
| 缓存 | **redis-py (async)** | 5.0+ | 相同查询短缓存 |
| 图表 | **前端渲染**（C 只产数据 + 图表配置 JSON） | - | 前端 ECharts/AntV 渲染，C 不渲染 UI |
| 可观测 | **OpenTelemetry Python** + `prometheus-client` | - | trace 串联、指标告警 |
| 配置 | pydantic-settings | 2.0+ | 环境变量统一管理 |
| 部署 | 独立微服务 `data-rag-service`（uvicorn + gunicorn worker） | - | K8s 部署；MVP 可 docker-compose 本地起 |

### 2.2 为什么是"语义层"而非"直接 Text2SQL 原始表"

- 原始表复杂、LLM 易错（漏 `route_version` 混入失效工艺数据、漏 `tenant_scope` 跨车间越权）。
- 语义层按 14 上下文固化业务概念，视图内部处理版本/租户/多表关联，LLM 只写简单 SELECT--准确率高、安全。
- 视图即 ACL：LLM 看不到原始表，视图内部强制注入过滤，比事后裁剪安全。

### 2.3 为什么多层校验而非单靠提示词

- 提示词不可靠，MES 不能赌模型听话。三层防线：① DB 级只读账号；② 应用级表白名单；③ 语义级 AST 校验。三层任一拦截即失败。

### 2.4 部署形态（车间网隔离）

- 管理层查询通常在办公网，C 服务部署在办公网侧，经只读副本查 MES 业务库。
- LLM 视安全策略二选一（云端 API 或本地化模型），`BaseChatModel` 抽象保证切换零代码改动。
- MVP 用 `docker-compose` 本地起 MySQL（只读副本）+ Redis + data-rag-service（§9.9）。

### 2.5 依赖清单（pyproject.toml 片段）

```toml
[project]
name = "data-rag-service"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.110",
  "uvicorn[standard]>=0.29",
  "gunicorn>=21.2",
  "pydantic>=2.6",
  "pydantic-settings>=2.2",
  "sqlalchemy[asyncio]>=2.0",
  "asyncmy>=0.2.9",
  "sqlglot>=23.0",
  "llama-index>=0.10",
  "langchain-core>=0.2",
  "redis>=5.0",
  "opentelemetry-api>=1.24",
  "opentelemetry-instrumentation-fastapi>=0.45b",
  "prometheus-client>=0.20",
]
```

---

## 3. 总体架构

```text
┌──────────────────────────────────────────────────────────────────┐
│ data-rag-service（独立微服务，Python + FastAPI + SQLAlchemy）      │
│                                                                    │
│  ┌──────────────┐  ┌──────────────────────────────────────────┐  │
│  │ FastAPI      │─▶│ QueryOrchestrator                          │  │
│  │ /rag/data/*  │  │  意图分类 -> SQL 生成 -> 校验 -> 执行 -> 图表 │  │
│  └──────────────┘  └────────────┬─────────────────────────────┘  │
│                                 │                                  │
│         ┌───────────────────────┼───────────────────────┐          │
│         ▼                       ▼                       ▼          │
│  ┌──────────────┐    ┌──────────────────┐    ┌──────────────┐    │
│  │ IntentClassifier│ │ SqlGenerator      │    │ SqlValidator │    │
│  │ NL -> 3 视图意图 │ │ LLM 在视图上生 SQL │    │ AST+白名单+租户 │    │
│  └──────────────┘    └────────┬─────────┘    └──────┬───────┘    │
│                               │                     │            │
│                       ┌───────▼─────────┐  ┌────────▼────────┐   │
│                       │ SemanticLayer   │  │ 只读执行 + 图表   │   │
│                       │ 3 语义视图 schema │  │ ReadOnlyExecutor │   │
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

- **语义层即 ACL**：`SemanticLayer` 只暴露 3 视图 schema，原始表对 LLM 不可见。
- **生成与校验分离**：`SqlGenerator`（LLM 生成）与 `SqlValidator`（AST + 白名单校验）解耦（SRP）。
- **只读执行**：`ReadOnlyExecutor` 用只读账号执行校验通过的 SQL。
- **审计可回溯**：每条查询的 NL/SQL/执行计划/结果摘要落 `SqlAudit`。

---

## 4. 语义层设计：MVP 3 视图

### 4.1 语义视图（MVP）

| 语义视图 | 来源上下文 | 固化的业务概念 | 关键列 | 版本维度 |
|---------|-----------|--------------|--------|---------|
| `v_work_order_progress` | 工单管理 + 过点执行 | 工单进度（完成/良品/不良/返修量） | work_order_id, status, completed_qty, good_qty, bad_qty, reworked_qty, route_version, tenant_scope | route_version |
| `v_wip_position` | 在制品追踪 + 过点执行 | 在制品当前位置与状态 | sn, work_order_id, current_station, status, position, route_version, tenant_scope | route_version |
| `v_defect_statistics` | 质量 | 缺陷统计（按缺陷码/工位/工单） | defect_code, defect_name, severity, count, work_order_id, station_id, rule_version, tenant_scope | rule_version |

> 全 12 语义视图见 [详细设计](./数据型 RAG-详细设计.md) §4.1。

### 4.2 视图 DDL（MVP 3 视图）

视图内部关联各上下文原始表，强制带 `tenant_scope` 与版本维度：

```sql
-- 视图 1：工单进度（关联工单管理 work_order + 过点执行 work_order_progress）
CREATE VIEW v_work_order_progress AS
SELECT
  wo.work_order_id,
  wo.status,
  wo.target_qty,
  wo.product_id,
  wo.route_id,
  wo.route_version,                  -- 版本维度（工单下达时锁定，领域总览 §5.1）
  wo.tenant_scope,                   -- 租户维度（权限过滤）
  COALESCE(wop.completed_qty, 0)  AS completed_qty,
  COALESCE(wop.good_qty, 0)      AS good_qty,
  COALESCE(wop.bad_qty, 0)       AS bad_qty,
  COALESCE(wop.reworked_qty, 0)  AS reworked_qty,
  wo.updated_at
FROM work_order wo
LEFT JOIN work_order_progress wop ON wo.work_order_id = wop.work_order_id
WHERE wo.status NOT IN ('CANCELLED');

-- 视图 2：在制品位置（关联在制品追踪 wip_unit + 过点执行 routing_progress）
CREATE VIEW v_wip_position AS
SELECT
  wu.sn,
  wu.work_order_id,
  wu.route_version,                  -- 版本维度（单件首次过点锁定）
  wu.tenant_scope,
  rp.current_step AS current_station,
  wu.status,
  wu.position,
  wu.updated_at
FROM wip_unit wu
LEFT JOIN routing_progress rp ON wu.sn = rp.sn
WHERE wu.status NOT IN ('COMPLETED', 'SCRAPPED');

-- 视图 3：缺陷统计（关联质量 quality_verdict + defect_catalog）
-- 🔴 rule_version 维度：质量判定带 rule_version，缺陷统计按规则版本可回溯（§4.3）
CREATE VIEW v_defect_statistics AS
SELECT
  dc.defect_code,
  dc.name        AS defect_name,
  dc.severity,
  qv.work_order_id,
  qv.station_id,
  qv.rule_version,                   -- 版本维度（判定时生效的规则版本）
  qv.tenant_scope,
  COUNT(*)       AS defect_count,
  DATE(qv.occurred_at) AS defect_date
FROM quality_verdict qv
JOIN JSON_TABLE(qv.defect_records, '$[*]'
  COLUMNS (defect_code VARCHAR(32) PATH '$.defect_code')
) dr ON TRUE
JOIN defect_catalog dc ON dr.defect_code = dc.defect_code
GROUP BY dc.defect_code, dc.name, dc.severity,
         qv.work_order_id, qv.station_id, qv.rule_version,
         qv.tenant_scope, DATE(qv.occurred_at);
```

> 🔴 **契约待对齐：`defect_records` 的 JSON 结构**。MVP 假设 `quality_verdict.defect_records` 是 JSON 数组（含 `defect_code`），用 `JSON_TABLE` 展开统计。该字段结构来自质量上下文 `QualityVerdictIssued` 事件载荷（[质量上下文.md](../../领域模型/制造资源服务/事件风暴/质量上下文.md) §2.4），落库表结构待与质量上下文领域建模确认。MVP 兜底：若 JSON 结构不同，调整 `JSON_TABLE` 路径或改用关联子表。

### 4.3 视图对 LLM 可见，原始表不可见

- `SemanticLayer` 只把 3 视图的列定义与业务描述喂给 LLM，原始表（`work_order`/`wip_unit`/`quality_verdict`/...）不出现在 schema 上下文。
- DB 权限：只读账号仅授 3 视图 SELECT，原始表不授权--即使 LLM 生成访问原始表的 SQL，DB 层也拒绝（§5.2 双保险）。

> 🔴 **`rule_version` 维度的必要性**：缺陷统计按 `rule_version` 可回溯到当时生效的质量门禁规则。漏掉会把不同规则版本下的缺陷混在一起统计，误导管理层。MVP 暴露 `rule_version` 维度，查缺陷统计时可按版本过滤（§6.3）。待与质量工程团队确认是否默认按最新版本统计（MVP 建议默认全版本 + 可选版本过滤）。

---

## 5. SQL 生成与校验：多层防线

### 5.1 SQL 生成（SqlGenerator）

```text
QueryOrchestrator.generate_sql(question, intent, tenant)
   │
   ├─ 1. 意图分类（IntentClassifier）：NL -> 命中 3 视图之一 + 查询意图（聚合/明细/排名）
   ├─ 2. 构造 schema 上下文：只喂命中视图的列定义 + 业务描述
   ├─ 3. LLM 生成 SQL（with_structured_output(SqlDraft)）
   │     系统提示词约束：只在视图 SELECT、带 tenant_scope、工艺数据带 route_version、禁写
   └─ 4. 返回待校验 SQL
```

- **意图分类优先规则匹配**：MVP 3 视图的关键词规则覆盖高频问题，命中不了再走 LLM。
- **schema 上下文裁剪**：只喂命中视图 schema，不喂全 3 视图（减少混淆）。
- **LLM 只生成不执行**：SQL 须经 `SqlValidator` + `ReadOnlyExecutor`。

### 5.2 只读账号（DB 级防线）

```sql
-- 只读账号：仅授 3 语义视图 SELECT，原始表与写操作全部拒绝
CREATE USER 'data_rag_ro'@'%' IDENTIFIED BY '***';
GRANT SELECT ON mes_readonly.v_work_order_progress TO 'data_rag_ro'@'%';
GRANT SELECT ON mes_readonly.v_wip_position TO 'data_rag_ro'@'%';
GRANT SELECT ON mes_readonly.v_defect_statistics TO 'data_rag_ro'@'%';
-- 不授原始表（work_order / wip_unit / quality_verdict / defect_catalog ...）
-- 不授 INSERT/UPDATE/DELETE/DDL
FLUSH PRIVILEGES;
```

- **DB 级兜底**：应用层校验全失效时，只读账号也无法写 MES。
- **只读副本**：C 连只读副本，不碰主库写路径。

### 5.3 AST + 白名单校验（应用级 + 语义级防线）

`SqlValidator` 用 sqlglot 解析 AST，三层校验 + 强制租户注入（详见 §9.3）：

1. **语义级**：必须是 `exp.Select`（非 INSERT/UPDATE/DELETE/DDL）。
2. **应用级**：访问的 `exp.Table` 必须在 3 视图白名单内。
3. **语义级**：禁止 `exp.Insert`/`exp.Update`/`exp.Delete`/`exp.Create`/`exp.Drop`/`exp.IntoOutfile`。
4. **强制租户注入**：校验通过后在 AST 注入 `WHERE tenant_scope IN ($tenant.scopes)`（§6.2）。

---

## 6. 权限、版本一致性与租户隔离

### 6.1 查询请求

```python
class DataQuery(BaseModel):
    question: str
    as_of: datetime | None = None       # 时间窗
    route_version: str | None = None    # 工艺版本（查历史时指定）
    rule_version: str | None = None     # 质量规则版本（缺陷统计时指定）
    tenant: TenantContext
```

### 6.2 租户隔离（前置过滤）

`SqlValidator._inject_tenant` 在 SQL AST 上强制注入 `WHERE tenant_scope IN ($tenant.scopes)`，权限不达标查不到数据。不是查完再裁剪。

### 6.3 版本一致性

- **查历史工艺数据**：用户指定 `route_version`，视图按版本过滤，不取"当前生效版"。
- **查缺陷统计**：可指定 `rule_version` 按规则版本过滤（🔴 §4.3），默认全版本 + 可选过滤。
- **查当前**：走 `v_route_version_active`（`status=ACTIVATED`），MVP 未含此视图，§11 扩展。
- 版本一致性从领域模型兜上来（过点记录绑 `routeVersion`，[领域总览.md](../../领域模型/领域总览.md) §5.1）。

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
        # 1. 缓存
        cached = await self._cache.get(request)
        if cached:
            self._metrics.cache_hit.inc()
            return cached
        # 2. 意图分类
        intent = await self._intent_classifier.classify(request.question)
        # 3. 生成 SQL
        sql = await self._generator.generate(request.question, intent, request.tenant)
        # 4. 校验（AST + 白名单 + 租户注入）
        try:
            validated_sql = self._validator.validate(sql, request.tenant)
        except SqlValidationError as e:
            self._metrics.validation_rejected.inc(str(e))
            return self._fallback(request, sql, str(e))  # 转人工
        # 5. 只读执行
        try:
            rows, latency_ms = await self._executor.execute(validated_sql)
        except ExecTimeout:
            return self._fallback(request, validated_sql, "执行超时")
        # 6. 转图表 + 答案
        answer = self._build_answer(request, intent, validated_sql, rows, latency_ms)
        # 7. 审计 + 缓存
        await self._audit_repo.record(request, validated_sql, answer)
        await self._cache.set(request, answer)
        return answer
```

- 编排与各步骤分离（SRP）；校验失败/超时转人工兜底。

### 7.2 只读执行器（ReadOnlyExecutor）

```python
class ReadOnlyExecutor:
    """用只读账号执行校验通过的 SQL，带超时与行数限制。"""

    def __init__(self, engine: AsyncEngine, settings: Settings) -> None:
        self._engine = engine; self._settings = settings

    async def execute(self, sql: str) -> tuple[list[dict], int]:
        async with self._engine.connect() as conn:
            await conn.execute(text("SET SESSION MAX_EXECUTION_TIME = :ms"),
                               {"ms": self._settings.sql_timeout_ms})
            result = await conn.execute(text(sql))
            rows = result.fetchmany(self._settings.max_rows)
            return [dict(r._mapping) for r in rows], len(rows)
```

- 只读连接 + 行数限制 + 超时（`MAX_EXECUTION_TIME`）。

### 7.3 语义层（SemanticLayer）

```python
class SemanticLayer:
    """3 语义视图注册表：列定义 + 业务描述，喂给 LLM 的 schema 上下文。"""

    VIEWS: dict[str, ViewSchema] = {
        "v_work_order_progress": ViewSchema(
            name="v_work_order_progress",
            description="工单执行进度：完成量/良品/不良/返修量，按工单维度",
            columns=[
                Column("work_order_id", "工单号"), Column("status", "工单状态"),
                Column("target_qty", "目标量"), Column("completed_qty", "完成量"),
                Column("good_qty", "良品量"), Column("bad_qty", "不良量"),
                Column("reworked_qty", "返修量"), Column("route_version", "工艺版本"),
                Column("tenant_scope", "租户(车间/产线)"), Column("updated_at", "更新时间"),
            ],
            examples=["WO-1234 现在到哪站", "昨天 SMT1 号线完工量"],
        ),
        "v_wip_position": ViewSchema(
            name="v_wip_position",
            description="在制品当前位置与状态",
            columns=[
                Column("sn", "序列号"), Column("work_order_id", "工单号"),
                Column("current_station", "当前工位"), Column("status", "状态"),
                Column("position", "位置"), Column("route_version", "工艺版本"),
                Column("tenant_scope", "租户"),
            ],
            examples=["WO-1234 的在制品都在哪些工位", "SMT1 号线当前在制数"],
        ),
        "v_defect_statistics": ViewSchema(
            name="v_defect_statistics",
            description="缺陷统计：按缺陷码/工位/工单，可排名 TOP",
            columns=[
                Column("defect_code", "缺陷码"), Column("defect_name", "缺陷名"),
                Column("severity", "严重度"), Column("defect_count", "缺陷次数"),
                Column("work_order_id", "工单号"), Column("station_id", "工位"),
                Column("rule_version", "规则版本"), Column("tenant_scope", "租户"),
                Column("defect_date", "缺陷日期"),
            ],
            examples=["上周返修 TOP3 缺陷", "WO-1234 的不良分布"],
        ),
    }

    def schema_for(self, view_names: list[str]) -> str:
        """构造 LLM schema 上下文（只喂命中视图）。"""
        ...
```

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
      validation.py            # SqlValidationError / ReadOnlyAccountGate
    infrastructure/
      sql/
        generator.py           # SqlGenerator（LLM 生成 SQL）
        validator.py           # SqlValidator（AST + 白名单 + 租户注入）
        executor.py            # ReadOnlyExecutor（只读账号执行）
        engine.py              # AsyncEngine（只读副本连接）
      ai/
        llm_factory.py
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
  Dockerfile
  docker-compose.yml
  sql/
    views.sql                  # 3 语义视图 DDL
    readonly_user.sql          # 只读账号 DDL
```

- `domain/semantic_layer.SemanticLayer` 是视图注册表（ISP）。
- `infrastructure/sql/` 三件套分离：生成/校验/执行各一个类（SRP）。

---

## 9. 关键代码骨架

### 9.1 意图分类器（NL -> 视图）

```python
class IntentKind(str, Enum):
    SUMMARY = "summary"    # 汇总
    DETAIL = "detail"      # 明细
    RANK = "rank"          # 排名 TOP

class SqlIntent(BaseModel):
    view: str
    agg: IntentKind

class IntentClassifier:
    """NL -> 命中 3 视图 + 查询意图。规则优先，LLM 兜底。"""

    RULES = [
        (["OEE", "完工", "进度", "工单", "良品", "不良量"],
         SqlIntent(view="v_work_order_progress", agg=IntentKind.SUMMARY)),
        (["在制品", "位置", "到哪站", "在制数", "工位"],
         SqlIntent(view="v_wip_position", agg=IntentKind.DETAIL)),
        (["缺陷", "TOP", "不良分布", "帕累托", "返修"],
         SqlIntent(view="v_defect_statistics", agg=IntentKind.RANK)),
    ]

    def __init__(self, llm: BaseChatModel, semantic: SemanticLayer) -> None:
        self._llm = llm; self._semantic = semantic

    async def classify(self, question: str) -> SqlIntent:
        # 1. 规则优先
        for keywords, intent in self.RULES:
            if any(k in question for k in keywords):
                return intent
        # 2. LLM 兜底
        return await self._llm.with_structured_output(SqlIntent).ainvoke(
            f"从以下问题判断命中哪个语义视图与查询意图：\n{question}\n可选视图：{self._semantic.view_list()}"
        )
```

### 9.2 SQL 生成器

```python
class SqlDraft(BaseModel):
    sql: str
    confidence: float

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
4. 工艺相关查询带 route_version 维度；缺陷统计可带 rule_version
5. 用参数化值，不要拼接用户输入

{schema_ctx}

问题：{question}
生成 SQL：
"""
        draft = await self._llm.with_structured_output(SqlDraft).ainvoke(prompt)
        return draft.sql
```

### 9.3 SQL 校验器（AST + 白名单 + 租户注入）

```python
import sqlglot
from sqlglot import exp

class SqlValidationError(Exception): ...

class SqlValidator:
    """校验生成的 SQL：纯 SELECT + 白名单视图 + 无危险操作 + 强制租户过滤。"""

    ALLOWED_VIEWS = {"v_work_order_progress", "v_wip_position", "v_defect_statistics"}

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
        ast = self._inject_tenant(ast, tenant)
        return ast.sql(dialect="mysql")

    def _inject_tenant(self, ast: exp.Select, tenant: TenantContext) -> exp.Select:
        where = ast.args.get("where")
        tenant_cond = exp.column("tenant_scope").isin(values=tenant.scopes)
        if where:
            ast.set("where", exp.and_(where.this, tenant_cond))
        else:
            ast.set("where", exp.Where(this=tenant_cond))
        return ast
```

- AST 解析防绕过（不靠正则）；白名单只 3 视图；强制注入租户过滤。

### 9.4 启动断言（只读校验 + 视图存在性）

```python
class ReadOnlyAccountGate(Exception):
    """启动时发现数据库账号非只读，拒绝启动。"""

async def assert_read_only_account(engine: AsyncEngine) -> None:
    """校验 DB 账号只有视图 SELECT 权限，无写权限。"""
    async with engine.connect() as conn:
        # 查 SHOW GRANTS，确认无 INSERT/UPDATE/DELETE/DDL
        result = await conn.execute(text("SHOW GRANTS FOR CURRENT_USER()"))
        grants = [row[0] for row in result]
        for g in grants:
            if any(k in g.upper() for k in ["INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"]):
                raise ReadOnlyAccountGate(f"只读账号含写权限: {g}")

async def assert_views_exist(engine: AsyncEngine, views: list[str]) -> None:
    """校验语义视图全部存在且可查。"""
    async with engine.connect() as conn:
        for v in views:
            try:
                await conn.execute(text(f"SELECT 1 FROM {v} LIMIT 1"))
            except Exception as e:
                raise ReadOnlyAccountGate(f"语义视图不可查: {v}: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = app.state.engine
    await assert_read_only_account(engine)
    await assert_views_exist(engine, list(SemanticLayer.VIEWS.keys()))
    yield
```

- `assert_read_only_account` 查 `SHOW GRANTS`，含写权限直接拒绝启动。
- `assert_views_exist` 校验 3 视图存在且只读账号可查。
- 红线靠启动断言兜底（与追溯型 `ReadOnlyProjectionGate`、D `ReadOnlyIngestionGate` 同思路）。

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

@router.get("/explain/{audit_id}", response_model=SqlAudit)
async def explain(
    audit_id: str, tenant: TenantContext = Depends(tenant_from_token)
) -> SqlAudit:
    """回溯某次查询的生成 SQL 与执行计划。"""
    ...
```

- `/query` 给管理层问答；`/explain` 回溯查询 SQL（审计可回溯）。

### 9.6 图表配置产出

```python
class ChartType(str, Enum):
    TABLE = "table"
    BAR = "bar"
    PARETO = "pareto"
    PIE = "pie"

class ChartConfig(BaseModel):
    chart_type: ChartType
    title: str
    x_field: str | None = None
    y_field: str | None = None
    series: list[str] = []

class DataAnswer(BaseModel):
    question: str
    sql: str                       # 生成并校验后的 SQL（可回溯）
    rows: list[dict]
    chart: ChartConfig
    confidence: float
    audit_id: str
    needs_human_review: bool = False
    disclaimer: str = "数据来自 MES 只读副本，最终决策需人工核对"
```

- C 产数据 + 图表配置 JSON，前端渲染（C 不渲染 UI）。
- `sql` 字段让管理层/工程师看到"这个数字怎么来的"。

### 9.7 配置与部署

```python
# app/config.py
class Settings(BaseSettings):
    # 只读副本（MES 业务库只读副本）
    readonly_dsn: str = "mysql+asyncmy://data_rag_ro:***@mes-readonly:3306/mes_readonly?charset=utf8mb4"
    # 审计库（C 自有）
    audit_dsn: str = "mysql+asyncmy://root:root@mysql:3306/data_rag?charset=utf8mb4"
    # Redis
    redis_url: str = "redis://redis:6379/0"
    # LLM
    llm_provider: str = "deepseek"
    llm_api_key: str = ""
    # 执行限制
    sql_timeout_ms: int = 5000
    max_rows: int = 1000
    confidence_threshold: float = 0.6
    class Config:
        env_prefix = "DATA_RAG_"
```

```yaml
# docker-compose.yml（MVP 本地起）
version: "3.9"
services:
  mysql-readonly:
    image: mysql:8.0
    environment:
      MYSQL_ROOT_PASSWORD: root
      MYSQL_DATABASE: mes_readonly
    ports: ["3307:3306"]
    volumes:
      - ./sql/views.sql:/docker-entrypoint-initdb.d/01-views.sql
      - ./sql/readonly_user.sql:/docker-entrypoint-initdb.d/02-user.sql
  mysql-audit:
    image: mysql:8.0
    environment:
      MYSQL_ROOT_PASSWORD: root
      MYSQL_DATABASE: data_rag
    ports: ["3308:3306"]
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
  data-rag-service:
    build: .
    depends_on: [mysql-readonly, mysql-audit, redis]
    environment:
      DATA_RAG_READONLY_DSN: mysql+asyncmy://data_rag_ro:***@mysql-readonly:3306/mes_readonly?charset=utf8mb4
      DATA_RAG_AUDIT_DSN: mysql+asyncmy://root:root@mysql-audit:3306/data_rag?charset=utf8mb4
      DATA_RAG_REDIS_URL: redis://redis:6379/0
    ports: ["8003:8000"]
```

- MVP 用 `docker-compose` 本地起只读副本 + 审计库 + Redis + data-rag-service，验证"NL -> SQL -> 校验 -> 执行 -> 图表"闭环。

---

## 10. 可观测性与兜底

### 10.1 指标（prometheus-client）

| 指标 | 含义 |
|------|------|
| `data_rag_query_total` | 查询次数（按 view label） |
| `data_rag_sql_gen_latency_seconds` | SQL 生成延迟（LLM，Histogram） |
| `data_rag_sql_exec_latency_seconds` | SQL 执行延迟（DB，Histogram） |
| `data_rag_validation_rejected_total` | 校验拒绝次数（按原因 label） |
| `data_rag_cache_hit_total` | 查询缓存命中 |
| `data_rag_low_confidence_total` | 低置信度转人工次数 |
| `data_rag_exec_timeout_total` | SQL 执行超时次数 |
| `data_rag_row_limit_hit_total` | 结果达行数上限次数 |

### 10.2 trace 串联

- 每次查询一个 `trace_id`，OpenTelemetry 在 `IntentClassifier`/`SqlGenerator`/`SqlValidator`/`ReadOnlyExecutor` 都注入 span。
- `SqlAudit` 记录 NL/SQL/执行计划/结果摘要/`trace_id`，`/explain` 可回溯。

### 10.3 兜底

- **校验失败**：SQL 校验拒绝 -> 重试 1 次（带拒绝原因反馈 LLM）；仍失败转人工。
- **执行超时/行数上限**：返回部分结果 + "结果较大，请缩小查询范围"。
- **低置信度**：`confidence < 0.6` -> `needs_human_review=True`，标注"建议人工核对"。
- **视图未覆盖**：意图分类命中不了 3 视图 -> 转人工，"该问题暂不支持"。
- **只读账号兜底**：应用层校验全失效时，DB 级只读账号拒绝任何写操作。

---

## 11. 实现步骤

### 阶段一：骨架与语义层（2 周）

1. 搭 `data_rag_service` 骨架（FastAPI + uvicorn），对齐 §8 包结构。
2. 在只读副本上创建 3 语义视图 DDL（§4.2）。
3. 创建只读账号 `data_rag_ro` + 启动断言 `assert_read_only_account`（§5.2、§9.4）。
4. 实现 `SemanticLayer` 3 视图注册表（§7.3）。

### 阶段二：SQL 生成与校验（2 周）

5. 实现 `IntentClassifier`（规则优先 + LLM 兜底）（§9.1）。
6. 实现 `SqlGenerator`（LLM 在视图上生成 SQL）（§9.2）。
7. 实现 `SqlValidator`（AST + 白名单 + 租户注入）（§9.3）。
8. 实现 `ReadOnlyExecutor`（只读账号 + 行数/超时限制）（§7.2）。

### 阶段三：编排与可观测（1-2 周）

9. 实现 `QueryOrchestrator` 编排链路 + 图表配置产出（§7.1、§9.6）。
10. 实现 `SqlAudit` 审计落库 + `/explain` 回溯端点（§9.5）。
11. 实现 `QueryCache`（同问题缓存）。
12. 接 OpenTelemetry + prometheus 指标（§10.1）。

### 阶段四：加固、评测与试点（1 周）

13. 沉淀评测集（典型管理层问题 + 预期 SQL/结果），回归模型/提示词变更。
14. 校验链路全测（非 SELECT/非白名单/危险操作/超时/行数上限/低置信度）。
15. 灰度一个管理层场景（如工单进度查询）试点，收集反馈。
16. 确认 🔴 决策点（§4.2 `defect_records` JSON 结构、§4.3 `rule_version` 默认过滤、§11 视图扩展）。

---

## 12. 约束落地检查清单

- [ ] LLM 只在 3 语义视图上生成 SQL，原始表对 LLM 不可见（§4.3）。
- [ ] 只读账号 `data_rag_ro` 仅授 3 视图 SELECT，DDL/DML 在 DB 级被拒；`assert_read_only_account` 启动断言生效（§5.2、§9.4）。
- [ ] `SqlValidator` AST 校验：纯 SELECT + 白名单视图 + 无危险操作；非 SELECT/非白名单/写操作直接拒绝（§5.3、§9.3）。
- [ ] 校验通过后强制注入 `tenant_scope` 过滤，权限不达标查不到数据（§6.2）。
- [ ] 工艺相关查询带 `route_version` 维度；缺陷统计可带 `rule_version` 过滤（§6.3）。
- [ ] C 服务不进过点主事务，查询走只读副本，不碰主库写路径（§5.4）。
- [ ] SQL 执行带超时（`MAX_EXECUTION_TIME`）与行数限制，防止全表扫描拖垮副本（§7.2）。
- [ ] 每条查询审计落库（NL/SQL/执行计划/结果摘要/trace_id），`/explain` 可回溯（§9.5）。
- [ ] 校验失败/低置信度/超时/视图未覆盖 -> 转人工兜底，不硬答（§10.3）。
- [ ] C 服务与 A/B/D 解耦，独立部署，不依赖 A/B 就绪（§1.3）。

---

## 13. 面试防守 Q&A

**Q：MVP 选了哪三个语义视图？为什么？**
A：选了工单进度（`v_work_order_progress`）、在制品位置（`v_wip_position`）、缺陷统计 TOP（`v_defect_statistics`）三个。原因：① 这三个是管理层/班长最高频的查询场景（工单到哪了、在制多少、不良分布）；② 覆盖了三种查询模式--汇总（进度）、明细（在制位置）、排名（缺陷 TOP）；③ 验证了"NL -> 意图分类 -> SQL 生成 -> 校验 -> 只读执行 -> 图表"完整闭环。其余 9 个视图（过点历史、质量判定、批量异常等）按相同范式扩展。

**Q：怎么保证 LLM 不会生成写操作或访问敏感表？**
A：三层防线，不靠单点。① DB 级--只读账号 `data_rag_ro` 仅授 3 视图 SELECT，原始表不授权，DDL/DML 在数据库层直接被拒；② 应用级--`SqlValidator` 白名单只 3 视图，访问非白名单对象直接拒绝；③ 语义级--用 sqlglot 解析 AST，非纯 SELECT / 含 `INSERT`/`UPDATE`/`DELETE`/`Create`/`Drop`/`IntoOutfile` 直接拒绝。三层任一拦截即失败。启动时 `assert_read_only_account` 查 `SHOW GRANTS` 校验账号权限，非只读直接拒绝启动。提示词告诉 LLM"只能 SELECT"不可靠，MES 不能赌模型听话。

**Q：缺陷统计视图为什么要带 `rule_version`？**
A：质量判定带 `rule_version`（判定时生效的质量门禁规则版本），缺陷统计按规则版本可回溯。漏掉会把不同规则版本下的缺陷混在一起统计，误导管理层。比如某缺陷在规则 v1 判 NG、规则 v2 判 PASS，混在一起统计缺陷次数就失真。MVP 暴露 `rule_version` 维度，查缺陷统计时可按版本过滤（🔴 默认全版本 + 可选过滤，待与质量工程团队确认）。这与工艺数据的 `route_version` 一脉相承--版本一致性从领域模型兜上来，A/B/C/D 共享同一套契约。

**Q：和管理层说"上周返修 TOP3 缺陷是桥接/冷焊/立碑"，这个怎么来的？可信吗？**
A：可回溯。`SqlAudit` 记录 NL 问题、生成 SQL、执行计划、结果摘要、`trace_id`，`/explain/{audit_id}` 让工程师回溯"这个 TOP3 是哪条 SQL 算出来的、查了 `v_defect_statistics` 视图、什么时间窗、是否带 `rule_version` 过滤"。答案的 `sql` 字段直接展示给管理层/工程师看。低置信度查询标注"建议人工核对"。校验失败/超时都转人工，不硬答。MES 领域给管理层错数据比不答更糟。

**Q：LLM 生成的 SQL 准确率不够怎么办？**
A：三招。一是语义层固化业务概念--LLM 在视图上写简单 SELECT 比在原始表上写复杂关联准确率高得多（视图内部已处理多表关联/版本/租户）。二是意图分类优先规则匹配--MVP 3 视图的关键词规则覆盖高频问题，不走 LLM。三是 schema 上下文裁剪--只喂命中视图 schema，不喂全 3 视图，减少混淆。再叠加校验失败重试（带拒绝原因反馈 LLM），稳态准确率可控。仍不准的低置信度查询转人工。

**Q：C 和 A/B/D 是什么关系？为什么旁路？**
A：C 是旁路，独立部署，不依赖 A/B/D 就绪（[RAG服务引入路线.md](../RAG服务引入路线.md) §3"C 旁路并行"）。四条路线答案来源不同：C 答"数据是多少"（业务库 Text2SQL），A 答"为什么"（追溯图），B 答"怎么处置"（文档），D 答"现场拦了怎么办"（推送）。C 不订阅过点事件、不调 A/B RAG，只连 MES 只读副本。管理层问 OEE 走 C，工程师问根因走 A，不能一个入口硬塞。

**Q：上线了吗？**
A：这是设计阶段规划，不是已落地。重点是三条架构判断：① 不让 LLM 直接碰原始表，在按 14 上下文固化的语义视图上生成 SQL（MVP 先 3 个高频视图）；② 三层校验防写（只读账号 DB 级 + 表白名单应用级 + AST 校验语义级），不靠提示词；③ 版本一致性与租户隔离从领域模型兜上来--查工艺数据带 `route_version`、查缺陷带 `rule_version`、查数据带 `tenant_scope` 前置过滤。C 是旁路，可与 A/B/D 并行，先做管理层高频查询验证可用性。诚实 + 体现架构判断力，比硬吹"已上线 Text2SQL"得分高。

---

## 14. 一句话定位

"数据型 RAG 把 MES 业务库做成自然语言查询--MVP 覆盖工单进度、在制品位置、缺陷统计 TOP 三个高频语义视图：不让 LLM 直接碰原始表，在按上下文固化的视图上生成 SELECT。安全靠三层防线--只读账号 DB 级拒写（`data_rag_ro` 仅授视图 SELECT）、表白名单应用级拦截、sqlglot AST 校验语义级防绕过，启动断言 `assert_read_only_account` 兜底。版本一致性与租户隔离从领域模型兜上来--查工艺数据带 `route_version`、查缺陷带 `rule_version`、查数据带 `tenant_scope` 前置过滤。每个答案带生成 SQL 可回溯，低置信度转人工。C 是旁路，与 A/B/D 解耦独立并行，服务管理层结构化数据查询场景。"

---

## 15. 与各上下文的契约对齐与待办

| 契约 | 状态 | 待办 |
|------|------|------|
| `quality_verdict.defect_records` JSON 结构 | 🔴 待对齐 | 与质量上下文领域建模确认 `defect_records` 落库结构（§4.2） |
| 缺陷统计 `rule_version` 默认过滤策略 | 🔴 待确认 | 默认全版本 + 可选过滤，待质量工程团队确认（§4.3） |
| MES 业务库只读副本部署 | 🔴 待运维确认 | 只读副本配置、同步延迟（[MySQL配置说明.md](../../实现说明/基础设施/MySQL配置说明.md)） |
| 其余 9 语义视图扩展 | ⏳ §11 | 过点历史/质量判定/批量异常/工艺版本/设备可用性/维修/齐套/首件/返修 |
| 视图与原始表字段对齐 | 🔴 待对齐 | 各上下文领域建模的落库表结构（work_order/wip_unit/quality_verdict/defect_catalog）与视图 DDL 对齐 |
