// 补插 P0(#33-36)与 P1(#37-44):首次因锚点用连字符、标题实为 em-dash 未匹配。
// 仅做这两处插入(其余 header/table/P2/EOF 已应用)。em-dash 用 String.fromCharCode(8212) 规避输入歧义。
import { readFileSync, writeFileSync } from 'node:fs';
const P = String.raw`d:\Code\factorybot-docs\factorybot\优化与待办清单.md`;
let t = readFileSync(P, 'utf8').replace(/\r\n/g, '\n');
const EM = String.fromCharCode(8212); // U+2014 -

const p0 = [
'### [ ] 33. Observability 包装类缺 recursion_limit_hit/schema_error/cost 方法,异常路径 AttributeError 崩溃并吞原始异常(2026-07-15)',
'- **位置**:`app/infrastructure/obs/observability.py:32-84`(Observability 仅暴露 tool_*/llm_called/low_confidence/session_*);调用方 `app/application/diagnosis_service.py:93` `self._obs.recursion_limit_hit("diagnosis")`(在 `except GraphRecursionError` 内)、`app/infrastructure/ai/observable_chat_model.py:41` `self._obs.schema_error(model)`(在 LLM `except` 内)',
'- **问题**:`MetricsCollector` 有 `recursion_limit_hit`/`schema_error`/`cost` 三方法,但包装类 `Observability` 未代理。诊断递归超限时进 `except GraphRecursionError` 调 `recursion_limit_hit` -> `AttributeError` 覆盖原始 `GraphRecursionError`,本应降级为 partial report 却崩溃;LLM 异常时同理被 `AttributeError` 吞没。mock 模式不触发故测试假绿,real 模式必崩。比 #11 更严重:#11 只说 `schema_error` 命名误导,实则该方法在包装类上根本不存在。',
'- **建议**:`Observability` 补 `recursion_limit_hit`/`schema_error`/`cost` 三个代理方法(经 `_safe` 调 `_metrics`),并同步补入 `ObservabilityPort` 协议(见 #39)。',
'',
'### [ ] 34. real 模式租户 scopes/role 回退默认值,跨租户越权 + 隐式提权(2026-07-15)',
'- **位置**:`app/api/deps.py:42` `scopes=default.scopes`;`app/api/deps.py:40` `role=role or "ENGINEER"`',
'- **问题**:`resolve_tenant_context` real 分支用 `default.scopes`(`default = c.default_tenant()` = WS-A 全量读写,含 `rework:write`/`process:write`/`pass:write`),任意 real 租户(如 WS-B)请求都拿到 WS-A 的 scope 集合 -> 跨租户越权写;`X-Tenant-Role` 缺失时默认 `ENGINEER`(高权限)。#1 修了"端点缺 tenant 依赖",但这里在"已解析出的 tenant 上"仍把权限挂到 WS-A,是 #1 残留的权限侧漏洞。',
'- **建议**:real 模式 scopes 应从租户自身 IAM/权限源解析,短期无法接入 IAM 至少 `scopes=[]` 兜底拒绝写操作;role 缺失回退最低权限(`VIEWER`/`OPERATOR`)而非 `ENGINEER`。🔴 需拍板 scope 来源(IAM 接入 vs 配置映射)。',
'',
'### [ ] 35. barrier_node 跨场景复用,PROCESS_CHANGE 会话被误置 SUSPENDED + 投递无关动作卡(2026-07-15)',
'- **位置**:`app/orchestration/code_nodes/barrier.py:33-53`(硬编码读 `tooling_result`/`kitting_result`);`app/orchestration/scenarios/specs.py:179-195`(PROCESS_CHANGE 并行分支为 `draft_sop`+`qualification_check`,汇入同一 `barrier` 节点)',
'- **问题**:`barrier_node` 是换线场景专用(检查 tooling/kitting 的 PASS/FAIL 分流),但 PROCESS_CHANGE 复用之。该场景 `tooling_result`/`kitting_result` 均为 `{}`,两个 PASS 检查与 FAIL 检查全落空,跌入 kitting FAIL 分支:`dispatcher.push_exception_card(...,"物料齐套未达标,请催料")`(与工艺变更完全无关)+ `repo.update_status(session_id,"SUSPENDED",...)`。虽 spec 中 barrier->gate_sop_publish 为固定边图会继续跑,但 session 已被污染为 SUSPENDED 且动作卡语义错误。',
'- **建议**:barrier 节点场景参数化,或 PROCESS_CHANGE 用纯同步汇合节点(不做业务判断)。🔴 需拍板:barrier 抽象为通用汇合 + 场景化分流子节点,还是各场景独立 barrier 节点。',
'',
'### [ ] 36. diagnosis catch-all 把原始异常消息写入报告返回调用方,敏感信息泄漏(2026-07-15)',
'- **位置**:`app/application/diagnosis_service.py:100` `except Exception as e: report = DiagnosisReport.partial(f"异常: {e}", ...)`',
'- **问题**:兜底 `except` 把 `str(e)` 直接塞进返回给调用方的 `DiagnosisReport`。异常可能含 DB 连接串、内部路径、SQL、stack 细节,经 API 泄漏给调用方(CWE-209)。',
'- **建议**:生产环境异常消息脱敏,报告只返通用描述(如"诊断服务内部错误"),原始异常记日志(注意此 catch-all 还会吞掉 #33 的 `AttributeError`,修 #33 后此处仍应脱敏)。',
].join('\n');

const p1b = [
'### [ ] 37. retry_tooling 字段永不写入,gate_disposition retry 回路不可达(死代码)(2026-07-15)',
'- **位置**:`app/domain/orchestration_state.py:166` `retry_tooling: bool`(仅定义);`app/orchestration/scenarios/specs.py:120` `cond=lambda s: "tooling_check" if s.get("retry_tooling") else "done"`',
'- **问题**:全代码库无任何节点写 `retry_tooling`,`s.get("retry_tooling")` 恒为 `None`(falsy),条件边恒走 `"done"`,gate_disposition->tooling_check 重试回路永不触发。要么 retry 功能未实现,要么遗漏了写该字段的节点(REJECT 时应置 True)。',
'- **建议**:实现 retry(gate REJECT 时写 `retry_tooling=True` 并回退 tooling_check)或删除该字段与死分支。',
'',
'### [ ] 38. GateManager.await_confirmation 静默吞 token 反序列化错误,致无日志 REJECT(2026-07-15)',
'- **位置**:`app/orchestration/code_nodes/gate.py:32-39` `except (KeyError, TypeError): confirmation = None`',
'- **问题**:checkpointer 序列化 resume value 为 dict 时用 keyword args 重建 `ConfirmationToken`,字段缺失/类型错被 catch 后 `confirmation=None` -> `decision="REJECT"`,但**零日志**。线上 checkpointer 序列化格式一变,所有 gate 静默 REJECT,排查极困难。',
'- **建议**:`except` 分支加 `_log.warning("gate.confirmation_token_reconstruct_failed", ...)` 记原始 dict 与异常类型。',
'',
'### [ ] 39. ObservabilityPort 协议与 Observability/MetricsCollector 实现严重不匹配,DIP 断裂(2026-07-15)',
'- **位置**:`app/infrastructure/obs/port.py:14-37`(协议仅 6 方法);`app/infrastructure/obs/observability.py:17-128`(实现 9 方法);`app/infrastructure/obs/metrics.py:48-90`(MetricsCollector 另有 3 方法未暴露)',
'- **问题**:协议 `ObservabilityPort` 只定义 `tool_ok/denied/error`/`llm_called`/`low_confidence`/`session_finished`,实现另有 `session_started`/`session_ended`(被 diagnosis_service 调用)及 #33 的三方法。调用方被迫依赖具体类 `Observability` 而非协议,依赖倒置断裂;也是 #33 方法缺失能逃过类型检查的根因。',
'- **建议**:把 `session_started`/`session_ended`/`recursion_limit_hit`/`schema_error`/`cost` 补入 `ObservabilityPort`,与 #33 一并修。',
'',
'### [ ] 40. COST_USD_TOTAL 指标零调用方,LLM 成本永远为 0(2026-07-15)',
'- **位置**:`app/infrastructure/obs/metrics.py:31,88-90`(定义 + `cost()` 方法);`app/infrastructure/obs/observability.py:52-73`(`llm_called` 不调 `cost()`)',
'- **问题**:`COST_USD_TOTAL` counter 注册了但 `llm_called()` 不据 token 数+模型定价调 `cost()`,且 `cost()` 未暴露到 `Observability`(见 #33/#39)。Prometheus 中 LLM 费用永远为 0,运维无法感知成本--与 #25 cost 子系统"设计+零上线"同源。',
'- **建议**:`llm_called` 内按 token 数与模型定价计算并调 `cost()`;`cost()` 暴露到 `Observability`+协议。',
'',
'### [ ] 41. httpx.AsyncClient real 模式无 close/aclose 生命周期管理(2026-07-15)',
'- **位置**:`app/infrastructure/acl/wiring.py:40` `httpx.AsyncClient(timeout=3.0)` 注入 14 个 ACL client;`app/infrastructure/acl/base.py`(无 `__del__`/`aclose`/context manager);`build_acl_clients` 返回的 `SimpleNamespace` 无 `close()`',
'- **问题**:real 模式创建的 AsyncClient 连接池进程生命周期内无关闭路径,app shutdown 时不 graceful close(残留 TCP 连接)。运行期池复用是预期的,但缺 shutdown hook 导致优雅停机时连接未释放。',
'- **建议**:`build_acl_clients` 返回对象提供 `async def close()` 遍历 client 调 `self._http.aclose()`,接到 `main.py` lifespan 的 shutdown 阶段。',
'',
'### [ ] 42. draft/orchestration 未知 kind/scenario 抛 ValueError 致 HTTP 500 而非 400(2026-07-15)',
'- **位置**:`app/application/draft_service.py:33` `raise ValueError(f"无对应草稿生成器: {draft_kind}")`;`app/application/orchestration_service.py:109` `raise ValueError(f"未知场景: {scenario}")`',
'- **问题**:客户端传入不存在的 draft_kind/scenario,抛裸 `ValueError` 未被异常处理器捕获,返回 500(内部错误)而非 400(参数错误),调用方无法判断是自身参数问题。',
'- **建议**:改抛 `DomainError`/`ValueError` 子类并在 `main.py` 注册异常处理器映射为 400,或直接 `raise HTTPException(400, ...)`。',
'',
'### [ ] 43. resume 单/多 interrupt 的 resume_value 序列化不一致,阻碍 #26 SqlSaver 接入(2026-07-15)',
'- **位置**:`app/application/orchestration_service.py:214-225`',
'- **问题**:单 interrupt 时 `resume_value = token`(`ConfirmationToken` dataclass 实例),多 interrupt 时 `resume_value[iid] = asdict(tok)`(dict)。checkpointer 切到需 JSON 序列化的后端(如 #26 的 AsyncSqlSaver)时,dataclass 实例可能无法序列化,resume 恢复失败。',
'- **建议**:统一 `resume_value = asdict(token)`(单/多均 dict)。',
'',
'### [ ] 44. BaseDraftBuilder.build LLM 调用无异常处理,失败直冲 HTTP 500(2026-07-15)',
'- **位置**:`app/application/builders/base.py:66` `await self._llm.ainvoke_structured(...)`;`app/application/draft_service.py` `draft()` 亦未捕获',
'- **问题**:LLM 调用超时/限流/格式错误时异常透传到路由层返回 500,无领域语义。',
'- **建议**:`build()` 或 `draft()` 加 try/except 转 `DomainError`/降级 partial,与 #42 异常处理器协同。',
].join('\n');

const A1 = '---\n\n## 🟡 P1 ' + EM + ' 可观测性落地';
const A2 = '---\n\n## 🟢 P2 ' + EM + ' 工程化门禁';

if (!t.includes(A1)) { console.error('ANCHOR P1 NOT FOUND'); process.exit(1); }
if (!t.includes(A2)) { console.error('ANCHOR P2 NOT FOUND'); process.exit(1); }
if (t.includes('### [ ] 33.')) { console.log('P0 已存在,跳过'); }
else { t = t.replace(A1, p0 + '\n\n' + A1); console.log('P0 已插入'); }
if (t.includes('### [ ] 37.')) { console.log('P1 已存在,跳过'); }
else { t = t.replace(A2, p1b + '\n\n' + A2); console.log('P1 已插入'); }

writeFileSync(P, t.replace(/\n/g, '\r\n'), 'utf8');
console.log('OK len=' + t.length);
