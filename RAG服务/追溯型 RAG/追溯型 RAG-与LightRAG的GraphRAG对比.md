# 追溯型 RAG 与 LightRAG 的 GraphRAG 对比

> 本文是一份**架构路径对比**，回答一个容易混淆的问题：FactoryRAG 路线 A（追溯型）和开源项目 LightRAG 都自称 "GraphRAG"，到底有什么异同？
> **对比依据**：FactoryRAG 侧以路线 A 的**实现代码**为准（`FactoryRAG/app/routes/traceability/`，见 [`追溯型 RAG-详细设计.md`](追溯型 RAG-详细设计.md) 的落地）；LightRAG 侧以其 **v1.5.6 源码**为准（`lightrag/`，HKUDS/LightRAG）。
> **口径纪律**：本文对比的是**两种架构路径**，不评判孰优孰劣--两者面向的场景相反，各自合理。FactoryRAG 路线 A 在本项目是**设计 + 部分实现**状态（实现缺口见文末附录），讲法遵循 [`RAG服务引入路线.md`](../RAG服务引入路线.md) §6 的口径纪律。
> **关联文档**：[`追溯型 RAG-详细设计.md`](追溯型 RAG-详细设计.md)（路线 A 落地展开）、[`rag-service-技术选型和实现方案.md`](../rag-service-技术选型和实现方案.md)（整体选型收敛）。

---

## 0. 一句话结论

**同壳异骨。** 两者共用的只是"知识图 + LLM + 向量"这个外壳；**图怎么来、怎么进、怎么保证对**三件事完全相反：

- **LightRAG** = 让 LLM 从文本造图、用向量相似度找路 -- 适合**没有 schema、只有文档**的场景。
- **FactoryRAG A** = 领域专家定图、领域事件填图、用精确键找路 -- 适合**有 schema、有结构化运营数据、且正确性性命攸关**的场景。

把它俩都叫 "GraphRAG" 是名字造成的最大误导。

---

## 1. 对比对象的精确界定

| 维度 | FactoryRAG 路线 A（追溯型） | LightRAG |
|------|----------------------------|----------|
| 定位 | 车间 MES 单件/批次全链路追溯 + LLM 根因综合 | 通用文档问答的 GraphRAG 框架 |
| 图来源 | 领域专家手工建模，Kafka 域事件投影确定性写入 | LLM 读文本 chunk 抽取实体/关系，非确定 |
| 代码位置 | `FactoryRAG/app/routes/traceability/` | `lightrag/`（v1.5.6） |
| 主存储 | Neo4j（属性图）+ MySQL（审计） | NetworkX（默认）/ Neo4j / PG·AGE / Mongo / Memgraph / OpenSearch |
| 数据源 | MES 结构化域事件（Kafka 流） | 非结构化文档（文件） |

---

## 2. 同：GraphRAG 的最低公约数

下面五点是两者真共享的，也是 "GraphRAG" 这个名字能同时套在两者头上的原因：

1. **图 + LLM + Embedding 三件套**：图存关系证据，LLM 做最终综合，embedding 做语义入口。
2. **都是只读索引，不回写源系统**：FactoryRAG 是对 MES 的只读旁路（5 个 `ReadOnly*Gate` 启动红线，见 [`FactoryRAG/README.md`](../../FactoryRAG/README.md) §1 核心安全契约）；LightRAG 是文档索引，根本不回写源文档。
3. **都支持增量摄入**：FactoryRAG 走 Kafka 域事件 + projection handler（`MERGE` 幂等）；LightRAG 走文档管线 + content-hash 去重 + `_merge_nodes_then_upsert`（`lightrag/operate.py:2260`）。
4. **都能用 Neo4j 当图库**：FactoryRAG 把 Neo4j 当主存；LightRAG 默认 NetworkX，Neo4j 是可选 backend 之一（`lightrag/kg/__init__.py:13`）。但用法天差地别（见 §3）。
5. **都给某个产物建了向量索引当入口**：FactoryRAG 给 `DefectCatalog.name_embedding` 建 1024 维 cosine 索引（`schema.py:31`）；LightRAG 给 entity / relation / chunk 三类都建。

> 共享到此为止。下面每一维都开始分叉。

---

## 3. 异：逐维对照

| 维度 | FactoryRAG 路线 A | LightRAG |
|------|-------------------|----------|
| **图的来源** | 领域专家手工建模，事件投影**确定性**写入 | LLM 读 chunk 抽取，**非确定**（`lightrag/prompt.py:54` entity_extraction prompt） |
| **Schema** | 强类型固定本体：`WipUnit`/`CheckpointRecord`/`RouteVersion`/`Bom`/`InventoryBatch`… + `UNIQUE` 约束（`schema.py:14`） | 无本体，LLM 自由发明类型，12 个默认只是"建议"，带 `Other` 逃生口 |
| **边的语义** | 类型化**有向**业务关系：`FOR_UNIT` / `SNAPSHOT_OF_ROUTE` / `CONSUMED_BATCH` / `USED_EQUIPMENT`… | 无类型、**无向**、关键词标签（"works_at"/"develops"），`lightrag/prompt.py:97` |
| **检索入口** | 正则精确抽 SN → `MATCH (w:WipUnit {sn:$sn})` 等值命中唯一约束（`retriever.py:26`） | **只能** embed 关键词 → 向量 top-k 找入口，无任何精确键查找路径（`lightrag/operate.py:5574` 恒走 `entities_vdb.query()`） |
| **确定性** | 确定性：同一 SN 永远命同一节点，同一事件流投影出同一图 | 非确定：不同 LLM run 抽出不同图，召回"相关"而非"精确" |
| **版本/时序** | 物理版本锁定边 `SNAPSHOT_OF_ROUTE{version}` + `as_of` 历史回溯，历史快照永不删 | 只有 `created_at`，无版本锚点、无 as-of 遍历、无版本锁定 |
| **向量扮演的角色** | **边缘兜底入口**（仅缺陷描述→缺陷码，且当前还返回空子图） | **核心入口**--没向量相似度根本进不了图，local/global/mix 全靠它 |
| **数据源** | MES 结构化域事件（Kafka 流），运营数据 | 非结构化文档（文件），文本语料 |
| **查询模式** | 单一目的化模式：seed → 5M1E 展开 → LLM 综合 | 多模式探索：naive / local / global / hybrid / mix / bypass（`lightrag/base.py:90` QueryParam） |
| **写回** | 只读旁路，从不回写 MES | 只读索引，从不回写源文档 |
| **典型场景** | 运营级追溯、防错、correctness-critical | 文档探索式问答、recall-oriented |

---

## 4. 三个关键分歧点

### 4.1 图的来源：被设计出来的 vs 被 LLM 猜出来的

FactoryRAG 的图**就是 MES 领域模型本身**（5M1E：Man/Machine/Material/Method/Measurement）。节点、边、约束都由领域专家在 `schema.py` 显式定义，projection handler 只是把 Kafka 事件流确定性地"落"进这个模子（`checkpoint.py`、`process_route.py`、`material.py`、`quality.py` 各管一个上下文）。这是 **schema-on-write**。

LightRAG 的图是 LLM 读文本后"幻觉/识别"出来的实体关系。类型名和边名都由 LLM 即兴生成（`entity_extraction_system_prompt` 只给 12 个**建议**类型，不强制），两次跑可能抽出两张不同的图。这是 **schema-on-read**。节点属性只是字符串字典（`entity_type`/`description`/`source_id`…），没有形式化本体、没有类型系统。

### 4.2 进图的钥匙：精确等值 vs 相似近邻

这是两者最本质的分歧，也直接回答"为什么 A 能按流水号精确追溯、而典型 RAG 做不到"。

FactoryRAG 用 `WipUnit.sn` 的 `UNIQUE` 约束做**确定性实体解析**（`schema.py:16`）。对一个已知流水号，`MATCH (w:WipUnit {sn:$sn})` 必须、也只能命一个节点，随后沿类型化边做多跳 `OPTIONAL MATCH` 展开 5M1E（`retriever.py:25-66`）。这是防错红线要求的--追溯场景里"近似匹配返回了另一个单件的历史"是不能容忍的故障。

LightRAG 没有这条路。它的查询恒从 `entities_vdb.query()` / `relationships_vdb.query()` 起步（`lightrag/operate.py:5574`、`:5849`），即向量相似度 top-k。`get_node` / `search_labels` 在 LightRAG 里**存在但不在检索路径上**（仅合并/管理用，`lightrag/base.py:826`）。若把 SN 当低层关键词喂进去，它被 embed 后跟一堆 entity 向量比 cosine--对标识符这是错的：`SN-001` 与 `SN-002` 的向量几乎贴一起，但它们是两个不同单件的历史，语义相似度对高基数任意串返回的是"语义相近但实体无关"的结果，对追溯**主动有害**。

> 一句话：标识符没有有意义的邻域，相似度检索对 ID 在原理上就是错的。FactoryRAG 把"找对实体"这个需要确定性的子问题交给带唯一约束的属性图，把"描述模糊→实体"才交给向量（且仅缺陷码一处）。

### 4.3 时序与版本：可回溯 vs 只有当下

FactoryRAG 的 `SNAPSHOT_OF_ROUTE{route_version}` 物理锁生产时版本（`checkpoint.py:39`），配合 `as_of` 时间窗能回答"这台单件当时是按哪版工艺做的"这种历史回溯问题，且历史快照边永不删（`ReadOnlyProjectionGate` 禁 `DELETE`/`REMOVE`）。as-of 追溯是追溯型 MES 的命门。

LightRAG 的边没有版本语义，只有 `created_at`。它能回答"现在图里有什么关系"，做不了 as-of 回溯--工艺变更后老关系会被 merge/upsert 覆盖或并存，没有"生产时锁定版本"的概念。

---

## 5. 为什么不能互换用

**LightRAG 的打法放到 FactoryRAG 会出事：**

- 非确定图无法满足审计/追溯的**可重复性**（同一提问两次结果不同）；
- 无精确 SN 查找违反**防错**红线（错给一条已失效工艺会直接导致批量不良）；
- 无版本锁定做不了**历史回溯**；
- 自由边丢失 5M1E **类型语义**，根因分析失去结构。

**FactoryRAG 的打法放到 LightRAG 也不行：**

- 文档语料没有现成本体可手工建模；
- 精确键查找对"这批文档都讲了啥"的**探索式提问**毫无意义；
- 单一查询模式撑不起**召回导向**的问答（LightRAG 的 local/global/hybrid/mix 多模式正是为此而生）。

结论：两者面向的场景在"是否有 schema / 是否要精确 / 是否容忍非确定"三条轴上取值相反，互换都会砸招牌。

---

## 6. 归纳与命名澄清

两者其实是图光谱的两端：

| | FactoryRAG 路线 A | LightRAG |
|---|---|---|
| 光谱位置 | schema-on-write、确定性、精确检索图 | schema-on-read、概率性、相似检索图 |
| 图的本质 | 图**就是**数据模型，LLM 只解释 | 图是文本的**派生索引**，LLM 既建图又解释 |
| 适合 | 有 schema + 结构化运营数据 + 正确性性命攸关 | 无 schema + 文档语料 + 探索式问答 |

**命名澄清**：LightRAG 里的 "RAG" 是字面意义上的"检索增强生成"（检索文本 chunk）；FactoryRAG 路线 A 叫 "RAG" 其实是个**误称**--它检索的是图关系而非文本片段，严格说该叫 *Graph-augmented generation*。[`FactoryRAG/README.md`](../../FactoryRAG/README.md) 把 A/B/E 三路线统称 "RAG" 是图省事，容易让人误以为 A 也该是向量语义那套，本文正是为澄清这一点而写。

---

## 附录：路线 A 实现缺口（与本文对比的关系）

本文对比的是**架构路径**，按设计意图成立。但路线 A 当前实现代码存在"读侧 Cypher 设计完整、写侧投影只覆盖 2/5 个 M"的不对称，5M1E 中 Measurement(质量) / Machine(设备) / Material(物料) 三个维度对 SN 查询当前基本为空：

- **Measurement**：`retriever.py:31` 读 `PRODUCED_TESTRESULT->TestResult`，但无投影创建 `TestResult` 节点或该边（`checkpoint.py` 的 `handle()` 不建），`quality.py:19` `_LINK_VERDICT` 还 `MATCH (t:TestResult)` 假定其存在 → 永远孤悬。
- **Machine**：`retriever.py:43` 读 `USED_EQUIPMENT->Asset`，但 `equipment_id` 只作 `CheckpointRecord` 扁平属性 SET，无 Asset 节点/边，无 Asset 投影 handler。
- **Material**：`retriever.py:56` 读 `CONSUMED_BATCH->InventoryBatch`，但 `material.py` 明确"不创建 CONSUMED_BATCH"；ACL 的 `MaterialAclClient.fetch_consumption` 已定义却全代码库无调用点，降级补齐未接线。

这不影响本文的架构对比结论（精确键入口、版本锁定、强类型本体均成立且已实现），但意味着"按流水号追溯"当前实际只能拿到 Man(过点) + Method(工艺) 两个维度。上述缺口是交叉比对读侧 Cypher 与写侧投影后发现的不对称，尚未单列进 [`FactoryRAG/优化与待办清单.md`](../../FactoryRAG/优化与待办清单.md)（该清单 P1 块目前只覆盖子图写库频率/LLM schema/置信度/materialize 等质量项）；补齐 TestResult 投影、设备投影接线、Material ACL 降级接线是让追溯"名副其实"的下一步。
