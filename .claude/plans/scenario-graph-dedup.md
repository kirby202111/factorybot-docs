# scenario graph 去重

## 目标
4 个 `build_*_graph`（~80% 雷同，合计 ~140 行）抽 `ScenarioGraphBuilder` + 声明式 spec，消除重复并降低新增场景成本。**纯装配去重，不改运行时行为。**

## 现状
4 个 `build_*_graph(sup, checkpointer)` 各自 `StateGraph(OrchestrationState)` -> `add_node` -> `add_edge`/`add_conditional_edges` -> `compile`。差异仅在节点列表与边拓扑。三种条件边：
- 并行派发：`add_conditional_edges(from, lambda s: ["a","b"])`（fault/complaint/process 的 plan/traceability，changeover 的 gate_process_switch）
- barrier 分流：`add_conditional_edges("barrier", barrier_route, {"draft_release":..,"root_cause":..,"suspend":END})`（仅 changeover；process 的 barrier 是普通汇聚边）
- gate retry：`add_conditional_edges("gate_disposition", lambda s: "tooling_check" if retry else "done", {...})`（仅 changeover）

`sup` 方法：`sup.plan`/`sup.done`（直接 async fn）、`sup.gate(step, capability=None)`/`sup.run_agent(capability)`（工厂）、`sup.barrier`、`sup.qc.*`。

## 方案：dataclass spec + ScenarioGraphBuilder

### 1. 新建 `app/orchestration/scenarios/specs.py`
```python
@dataclass
class NodeSpec:
    name: str
    factory: Callable[[SupervisorGraph], Callable]   # sup -> 节点函数

@dataclass
class EdgeSpec:
    from_node: str            # "START" 或节点名
    to: str | list[str] | None = None   # str=普通边, list=并行派发, None=分流(cond)
    cond: Callable | None = None        # 分流条件函数
    path_map: dict | None = None        # 分流路径映射（值可为 "END"）

@dataclass
class ScenarioSpec:
    name: str
    nodes: list[NodeSpec]
    edges: list[EdgeSpec]

class ScenarioGraphBuilder:
    def __init__(self, sup, checkpointer): ...
    def build(self, spec: ScenarioSpec):
        g = StateGraph(OrchestrationState)
        for n in spec.nodes: g.add_node(n.name, n.factory(self._sup))
        for e in spec.edges:
            if e.cond is not None:                      # 分流
                pm = {k: _resolve(v) for k,v in (e.path_map or {}).items()}
                g.add_conditional_edges(_resolve(e.from_node), e.cond, pm)
            elif isinstance(e.to, list):                # 并行派发
                g.add_conditional_edges(_resolve(e.from_node), lambda s, t=e.to: t)
            else:                                        # 普通边
                g.add_edge(_resolve(e.from_node), _resolve(e.to))
        return g.compile(checkpointer=self._checkpointer)
# _resolve("START")->START, _resolve("END")->END, 其余原样

SCENARIO_SPECS = {"CHANGEOVER": ..., "FAULT_RESPONSE": ..., "COMPLAINT_8D": ..., "PROCESS_CHANGE": ...}
```
4 个 spec 忠实迁移原图节点/边（一一对应，含 changeover 的 barrier 分流与 gate_disposition retry）。

### 2. 删除 4 个 `build_*_graph.py`
顺带去掉 `fault_response_graph._fault_barrier` 死代码（定义未用）。

### 3. 改 `scenarios/__init__.py`
导出 `SCENARIO_SPECS` + `ScenarioGraphBuilder`，去掉 `build_*_graph` 导出。

### 4. 改 `container.py`
```python
from app.orchestration.scenarios import SCENARIO_SPECS, ScenarioGraphBuilder
...
b = ScenarioGraphBuilder(self.supervisor, self.checkpointer)
self.graphs = {name: b.build(spec) for name, spec in SCENARIO_SPECS.items()}
```

### 5. 补测试 `tests/test_scenario_specs.py`
- `test_all_scenario_specs_build_without_error`：4 spec 各 `build()` 不抛（compile 成功）
- `test_changeover_spec_node_count`：changeover spec 节点数==13、边数==14（防迁移漏节点/边）

### 6. 验证
跑全量测试：`test_orchestration_changeover`（端到端，验证 changeover 运行时行为不退化）+ 新 spec 测试 + 既有 52 测试全过。

## 风险与缓解
- **spec 声明错误**：build+compile 测试抓 compile 期错误；changeover 端到端抓运行时。
- **3 个 scenario 无端到端**：spec 忠实一一迁移原图（节点/边完全对应），行为应一致；build+compile 测试保证装配正确。不补端到端（用户第二优先级未选 scenario 测试）。
- **并行派发 lambda 闭包**：默认参数捕获 `lambda s, t=e.to: t`，避免闭包陷阱。
- **path_map 含 END**：`_resolve` 统一转换 "END" -> langgraph END。

## 不在范围
- 不改 graph 运行时行为（纯装配去重）
- 不补 3 个 scenario 端到端测试
- ACL 去重（第三优先级最后一项，另行处理）
