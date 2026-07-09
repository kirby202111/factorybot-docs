# KitStatus 迁移：在制品追踪 → 工单管理（独立聚合根）+ 在制品追踪重构

## 决策摘要（已定）

1. **KitStatus 整体迁入工单管理上下文，作为独立聚合根**（不折叠进 `WorkOrder` 聚合）。带走的资产：`KitStatus` 聚合根、`KitItem` 值对象、`KitStatusService` 领域服务、`KitStatusChanged` 事件、INV-08（kit_ready⇔missing 空，双向可逆）、BIZ-02（重算幂等）。
2. **KitStatus 初始化时机**：`WorkOrderReleased`（审核下达、BOM 版本已锁定）时，工单管理上下文内部创建 `KitStatus` 聚合并开始消费 `material.*` 重算。理由：下达时 BOM 已知，齐套判定有了依据；且 `wo.order.released` 不再需要通知在制品追踪"初始化齐套投影"（该消费引用删除）。
3. **主题改名**：`wip.kit.status` → `wo.kit.status`（沿用 `wo.<aggregate>.<event>` 前缀，保留 `status` 词面最小化改名）。
4. **编号策略**：
   - 工单管理上下文新增：`INV-08`（kit_ready⇔missing 空，可逆）、`BIZ-08`（重算幂等 (work_order_id, source_event_id) 无变化不发布）、`INV-CX-08`（KitStatus 最终一致于物料上下文事件，可幂等重放重建）。三者编号在 WO §6 均为空位/续号，不冲突。
   - 在制品追踪上下文：**删除** INV-08、BIZ-02 两行，**保留空号不重排**（避免牵动全文 INV/BIZ 内联引用与 §10 映射表的级联改动），在 §变更说明追加一条记录解释空号由来。其余 INV/BIZ/INV-CX 编号不动。
5. **改动范围 = 全量一致性更新**（不止 WO+WIP）：消费方/发布方文档若仍指向 `wip.kit.status` 或"在制品追踪重算 KitStatus"会造成自相矛盾，必须一并改。

## 受影响文件清单（11 个）

| # | 文件 | 改动性质 |
|---|------|---------|
| 1 | `生产执行服务/领域建模/工单管理上下文.md` | **新增** KitStatus 聚合/服务/事件/不变式/主题 + 改既有齐套引用 |
| 2 | `生产执行服务/事件风暴/工单管理上下文.md` | **新增** 齐套判定主链 + 改既有引用 |
| 3 | `生产执行服务/领域建模/在制品追踪上下文.md` | **瘦身** 删 KitStatus 全部内容 + 重写 §0 职责 |
| 4 | `生产执行服务/事件风暴/在制品追踪上下文.md` | **瘦身** 删 KitStatus 主链/约束/契约 + 重写 §0 |
| 5 | `生产执行服务/领域建模/过点执行上下文.md` | 改消费方：`wip.kit.status`→`wo.kit.status`、降级查询目标→工单管理 |
| 6 | `生产执行服务/事件风暴/过点执行上下文.md` | 改引用：`wip.kit.status`→`wo.kit.status`、来源→工单管理 |
| 7 | `生产执行服务/领域建模/排产上下文.md` | 改消费方：`wip.kit.status`→`wo.kit.status`、来源→工单管理 |
| 8 | `生产执行服务/事件风暴/排产上下文.md` | 改引用：`wip.kit.status`→`wo.kit.status`、来源→工单管理 |
| 9 | `制造资源服务/领域建模/物料上下文.md` | 改 ~15 处"在制品追踪重算 KitStatus"→"工单管理重算 KitStatus" + §7 契约表 |
| 10 | `制造资源服务/事件风暴/物料上下文.md` | 改引用（若存在） |
| 11 | `领域总览.md` | §2.3 职责表 + §3 mermaid 图 |

> `工艺管理上下文.md.bak` 忽略。`质量上下文`/`工艺管理上下文` 的命中为 incidental（"齐套"作防错/门禁语境），逐处核对后仅在确指 KitStatus 时改。

---

## A. 工单管理上下文（领域建模）— 新增内容

### A1. §0 建模总览
- 职责段补一句："**工单齐套状态判定**——消费物料上下文事件重算 `KitStatus`（`kit_ready` + `missing_items[]`），双向可逆，发布 `wo.kit.status` 供过点执行/排产消费"。
- "不负责"段把"在制品身份、位置投影、齐套状态计算 -> 在制品追踪"拆为：位置投影仍归在制品追踪；**齐套状态判定改归本上下文**。
- 聚合划分图新增 `KitStatus (聚合根·判定型)` 方框；共享值对象补 `KitItem`；领域服务补 `KitStatusService`。
- §0 聚合边界说明补：`KitStatus` 是独立聚合根（工单级齐套判定，双向可逆 INV-08），与 `WorkOrder` 生命周期聚合分离，避免物料高频重算拖重 WorkOrder 聚合。

### A2. §1 聚合根 — 新增 §1.3 KitStatus（工单齐套状态）
照搬在制品追踪原 §1.2 内容（一致性边界/职责/yaml 结构/行为 `recompute(items[], source_event_id)`），编号引用本地化：INV-08、BIZ-08。补"初始化"行为：`initOnRelease(work_order_id, bom_binding, source_event_id)` 由 `WorkOrderReleased` 触发建档（首次 `kit_ready` 按当前库存判定或置 NOT_READY 待首条物料事件）。

### A3. §2 值对象 — 新增 KitItem / KitReadyStatus
照搬在制品追踪原 §2.4（KitItem）、§2.6（KitReadyStatus）。

### A4. §3 领域服务 — 新增 §3.6 KitStatusService
照搬原 `KitStatusService.onMaterialEvent`（消费 `material.*` 重算 + BIZ-08 幂等 + INV-08 可逆 + 无变化不发布）。补"为什么不放聚合根内"（跨实例去重 + 跨上下文消费物料事件）。🔴 物料上下文事件契约热点——物料上下文已落地，把"🔴 待对齐"降级为已对齐，列出实际订阅主题：`material.bom.lifecycle`(BomActivated/Deprecated)、`material.inventory.changed`、`material.substitute.lifecycle`、`material.kit.reserved`、`material.kit.released`。

### A5. §4.2 WorkOrderEventConsumeAppService — 新增 material.* 订阅块
新增"物料上下文（主题前缀 material.*）"订阅段，列出 5 个主题 → `KitStatusService.onMaterialEvent`，幂等 `(work_order_id, source_event_id)`（BIZ-08）。同时在内部策略段补：`WorkOrderReleased` → `KitStatusService.initOnRelease`（建档齐套聚合）。
> 工单管理上下文此前对物料上下文仅 REST 查询（绑 BOM）；本次**新增事件订阅**关系——这是工单物料故事（BOM 绑定 + 实时齐套）的自然延伸，经 ACL + `consumed_event` 去重。

### A6. §5 领域事件 — 新增 §5.8 齐套状态事件
`KitStatusChanged`（work_order_id, kit_ready, missing_items[], changed_at, source_event_id），发布到 `wo.kit.status`，有变化时发布（BIZ-08）。

### A7. §6 不变规则
- §6.1 新增子节 §6.1.3 KitStatus：`INV-08`。
- §6.2 新增 `BIZ-08`。
- §6.3 新增 `INV-CX-08`（KitStatus 最终一致于物料上下文事件，幂等重放可重建）。

### A8. §7 对外契约 — 新增主题行
`wo.kit.status` | KitStatusChanged | 主消费方：**过点执行上下文**（首次过点齐套防错）、**排产上下文**（投放/重排决策）。
**改既有行**：`wo.order.released` 的消费方去掉"在制品追踪上下文（初始化齐套状态投影）"（齐套建档改本上下文内部）；`wo.order.created` 同理去掉"齐套初始化预热"。

### A9. §8 聚合间关系图 / §10 映射表
补 KitStatus 分支（物料事件 → KitStatusService → KitStatus → wo.kit.status → 过点执行/排产）；§10 补 KitStatus 相关贴纸映射。

### A10. §变更说明
追加 2026-07-09 条：`KitStatus` 聚合根（含 KitItem/KitStatusService/KitStatusChanged/INV-08/BIZ-02→BIZ-08/INV-CX-08）由在制品追踪上下文迁入，作为本上下文独立聚合根；主题 `wip.kit.status`→`wo.kit.status`；`wo.order.released` 触发本上下文内部建档 KitStatus。

## B. 工单管理上下文（事件风暴）— 新增内容
- §0 核心关注 / 上下文内职责：补"工单齐套状态判定"；上下文外职责把"齐套状态 -> 在制品追踪"改为归本上下文。
- §1.2 聚合根主语清单：补 `KitStatus` 行。
- §2 新增主链段"§2.x 齐套状态判定与发布"（照搬在制品追踪事件风暴原 §2.4 的风暴图 + 关键事件表 + 热点）。
- §2.5 上下文外事件消费表：补 5 个 `material.*` 主题行。
- 已识别约束表：补 KitStatus 两条约束（kit_ready⇔missing 空/可逆、重算幂等无变化不发布）。
- 关键策略表：补"物料事件触发齐套重算"、"WorkOrderReleased 触发齐套建档"。
- 对外契约表：补 `wo.kit.status` 行；`wo.order.released` 消费方去掉在制品追踪齐套初始化。
- 事件命名速查表：补 KitStatusChanged；外部消费事件补 material.* 系列。

## C. 在制品追踪上下文（领域建模）— 瘦身
- §0 建模总览：职责改为纯"在制品身份建档、位置投影、流转历史累积、位置快照发布、批次视图"——**删除"齐套状态判定与发布"**；"不负责"段补"工单齐套状态判定（归工单管理上下文）"。聚合划分图删 `KitStatus` 方框、共享值对象删 `KitItem`、领域服务删 `KitStatusService`。删"判定型聚合"表述，本上下文只剩投影型聚合 `WipUnit`。
- §1：删 §1.2 KitStatus 整节。
- §2：删 §2.4 KitItem、§2.6 KitReadyStatus。
- §3：删 §3.2 KitStatusService 整节。
- §4.1 WipEventConsumeAppService：删 `material.*` 订阅行；事务边界表删"消费物料齐套事件"行。§4.2 WipQueryAppService：删 `getKitStatus` / `listKitStatuses` 两方法。
- §5：删 §5.2 齐套状态事件。
- §6：删 §6.1.2 KitStatus 子节（INV-08）、§6.2 BIZ-02 行；保留空号不重排，§变更说明追加说明。
- §7：删 `wip.kit.status` 主题行。
- §8/§10：删 KitStatus 分支与映射行。
- §0 与过点执行协作边界表：删"齐套判定归本上下文"表述、删 `wip.kit.status` 事件流向、删"与物料上下文协作边界"整节（齐套不再归本上下文，本上下文与物料上下文无直接协作）。

## D. 在制品追踪上下文（事件风暴）— 瘦身
- §0 核心关注：删"工单齐套状态"、删"为什么不把齐套拆成独立上下文"整段（已 N/A，齐套整体迁出）。
- §1.1 聚合根表：删 KitStatus 行。§1.2 读模型表不变。
- §2.2.2 KitStatus 状态机：删整节。
- §2.4 齐套状态判定与发布：删整节。
- §2.5 消费表：删"物料齐套相关事件"行。
- 已识别约束表：删 KitStatus 两条。
- 关键策略表：删 RecomputeKitStatus / KitStatusChanged 行。
- 对外契约表：删 `wip.kit.status` 行。
- "与物料上下文、工艺管理上下文协作边界"节：删 KitStatus 相关行/流向（工艺管理 `ProcessRouteActivated` 刷新 route_version 缓存仍保留，归 WipUnit）。
- "与首件/返修/返工协作边界"节：删 `wip.kit.status` 流向。
- 事件命名速查表：删 KitStatusChanged、删 material.* 消费行。

## E. 过点执行上下文（领域建模 + 事件风暴）
- 领域建模：`KitStatusCache` 刷新来源 `wip.kit.status`→`wo.kit.status`；降级查询目标"在制品追踪上下文"→"工单管理上下文"；§2.5 消费表、§4.2 订阅、§10 映射、外部系统表、协作边界表、速查表同步改来源为工单管理上下文 + `wo.kit.status`。
- 事件风暴：消费表/协作边界/速查表 `wip.kit.status`→`wo.kit.status`，来源→工单管理。

## F. 排产上下文（领域建模 + 事件风暴）
- 领域建模：`onKitStatusChanged` 来源 `wip.kit.status`→`wo.kit.status`、来源上下文→工单管理；§4.2 订阅段、§7 契约/外部系统表、§8 关系图、INV-CX-03、§10 映射同步改。
- 事件风暴：§0"不负责齐套状态判定（归在制品追踪）"→归工单管理；消费表/策略表/契约表/关系图 `wip.kit.status`→`wo.kit.status`、来源→工单管理。

## G. 物料上下文（领域建模 + 事件风暴）
- 领域建模：~15 处"在制品追踪上下文重算 KitStatus"→"工单管理上下文重算 KitStatus"；§7 对外契约表 5 行（bom.lifecycle / substitute.lifecycle / inventory.changed / kit.reserved / kit.released）消费方在制品追踪→工单管理；§8 关系图、§10 映射、各 Service 注释同步改。BOM 生效/替代料/库存/预占/释放的事件消费方标注全部改向工单管理。
- 事件风暴：核对并同步引用。

## H. 领域总览
- §2.3 职责表：在制品追踪上下文职责去掉"齐套状态"；工单管理上下文职责补"工单齐套状态判定（消费物料事件重算，双向可逆）"。
- §3 mermaid：`MAT -->|BOM版本(REST)| WO` 边补标注"齐套事件(material.*)→WO 重算 KitStatus"；`EXEC -->|过点位置变更| WIP` 保留；新增 `WO -->|wo.kit.status| EXEC`（首次过点防错）与 `WO -->|wo.kit.status| 排产`（投放决策）边；移除原 `WIP` 与 kit 相关隐含边。注：排产上下文未在 §2.3 表中列出（领域总览遗漏），本次顺手在 §2.3 补一行排产上下文。

---

## 执行顺序
1. **A**（工单管理·领域建模新增）→ 2. **B**（工单管理·事件风暴新增）——先建新家。
3. **C**（在制品追踪·领域建模瘦身）→ 4. **D**（在制品追踪·事件风暴瘦身）——再清旧家。
5. **E/F**（过点执行、排产消费方）→ 6. **G**（物料发布方）→ 7. **H**（领域总览）——最后对齐引用与全局图。

每步完成后逐文件核对：无残留 `wip.kit.status`、无残留"在制品追踪…齐套/KitStatus"误指、编号与 §10 映射自洽。

## 不在本次范围
- 字段级 Schema 精度、终端 UI、批次视图完整查询模型——维持暂缓。
- 排产上下文的缺料预计齐套时间 `expected_ready_at` 🔴 热点——维持开放，不本次解决。
- 物料上下文 BIZ-03 跨引用歧义（注释中"BIZ-03"指代）——实现时核对修正，不影响本次结构。
