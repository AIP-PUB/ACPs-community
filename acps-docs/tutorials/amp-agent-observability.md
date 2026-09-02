[首页](../README.md)

# 在 Agent 中接入 AMP 可观测性

这篇教程写给 **Agent 开发者**：平台侧的 AMP（转发、入库、查询服务）已经由运维装好时，你要做的是两件事——

1. **头**：在 Agent 代码里用 `acps-sdk` 的 Emitter 写出日志  
2. **尾**：用 Monitor / Discovery / `acps-cli` 确认日志能被看见  

中间「谁来采集、怎么入库」当作黑盒即可。怎么搭安装包、怎么配 Forwarder，见 [Ansible 部署](./install-package-ansible-deploy.md)，本文不展开。

成品参考：`demo-partner/partners/generic_runner.py`、`demo-leader` 里的装配；端到端验收故事见文末的 `business.yml` Step D。

---

## 0. 你要达到什么效果

业务跑起来之后，对某个 Agent 的 AIC，你应该能够：

- 在 **Monitor** 里查到多类 AMP 记录（heartbeat / metrics / access / message / system / audit）  
- 在 **Discovery** 的存活视图里看到该 Agent（heartbeat 经平台同步后的结果）

SDK 侧的默认行为是：**往本地 NDJSON 文件追加一行**。采集与入库是平台的事；你只要保证进程在写、AIC 与注册身份一致。

```text
[头] Agent：Emitter → 本地 *.jsonl / *.ndjson
        ↓  （平台黑盒：Forwarder → 总线 → 入库 / alive-sync）
[尾] Monitor 查询 / Discovery 存活 / acps-cli monitor …
```

---

## 1. 准备：目录、AIC、依赖

```bash
# 开发机：安装含 amp 能力的 SDK（以你项目的依赖方式为准）
# uv add acps-sdk   或在 pyproject 中声明依赖后 uv sync
```

```python
import os
from pathlib import Path

# 安装部署通常会注入 AMP_LOG_DIR（例如 /opt/acps/app/logs）
# 本地开发未设置时，可用仓库下的 logs/
AMP_LOG_DIR = Path(os.environ.get("AMP_LOG_DIR", "./logs"))
AMP_LOG_DIR.mkdir(parents=True, exist_ok=True)

# 必须与 Registry 上该 Agent 实例的 AIC 一致
AIC = os.environ["ACPS_AIC"]  # 示例：换成你的真实 AIC
```

约定建议（与 demo 一致，便于 Forwarder 按文件收）：

| 类型 | 建议文件名示例 |
| --- | --- |
| heartbeat | `amp_heartbeat_<name>.jsonl` |
| metrics | `amp_metrics_<name>.jsonl` |
| access | `amp_access_<name>.jsonl` |
| message | `amp_message_<name>.jsonl` |
| system | `amp_system_<name>.jsonl` |
| audit | `amp_audit_<name>.jsonl` |

---

## 2. 六类 Emitter：怎么用

这一节按「什么时候用 → 怎么挂进进程 → 字段怎么填 → 示例」来讲。示例对齐 demo 的常见写法，目的是你能直接抄到自己的 Agent 里改，而不是只证明 API 能跑通。

共性习惯：

- **启动时创建一次** Emitter（绑定 `aic` 与日志文件），业务路径里反复 `emit` / `emit_sync`。  
- 异步服务里优先 `await emitter.emit(...)`（内部用线程写文件，不堵事件循环）；同步回调里用 `emit_sync`。  
- 写入失败默认只打 WARNING、不抛异常——业务主路径不要依赖「emit 成功才继续」。  
- `trace_id` / `span_id` / `correlation_id` 能带就带：后续在 Monitor 里按会话、任务、链路排查会轻松很多。

### 2.1 Heartbeat — 告诉平台「我还活着」

**用途**：Discovery / Monitor 的存活视图依赖心跳。没有持续 heartbeat，Agent 在 alive 视图里会「消失」，即使业务还在跑。

**怎么挂**：进程启动后开一个后台周期任务；关闭时 cancel。demo 用环境变量 `AMP_HEARTBEAT_INTERVAL_SECONDS`（默认约 15s）。`uptimeSeconds` 由 Emitter 按创建时刻自动算，你不用填。

```python
import asyncio
import contextlib
import os

from acps_sdk.amp import HeartbeatEmitter

heartbeat = HeartbeatEmitter(AMP_LOG_DIR / f"amp_heartbeat_{agent_name}.jsonl", aic=AIC)


async def run_agent() -> None:
    interval = float(os.environ.get("AMP_HEARTBEAT_INTERVAL_SECONDS", "15"))
    hb_task = asyncio.create_task(heartbeat.run_periodic(interval), name="amp-heartbeat")
    try:
        await serve_forever()  # 你的主循环
    finally:
        hb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb_task
```

`run_periodic` 会**立刻发首条**，再按间隔续发，方便刚启动就验证链路。本地自检：文件在增长，且 Monitor `heartbeat liveness <AIC>` 能点到。

### 2.2 Metrics — 周期性指标快照

**用途**：给 Monitor 提供负载与窗口统计（并发任务、CPU/内存、成功率、时延分位等），支撑 snapshots / series 查询。

**怎么挂**：和 heartbeat 类似，启动时 `run_periodic`。差别是 body 不由 Emitter 编造——你要实现 `SampleProvider`（只要有 `sample() -> MetricsBody`）。生产环境应读真实队列长度、进程资源；联调可先用 SDK 的 `DemoMetricsSampler`（**仅 demo，勿上生产**）。

`resource` 建议带上 OpenTelemetry 风格的服务标识，便于按服务名过滤：

```python
import time

from acps_sdk.amp import LoadMetrics, MetricsBody, MetricsEmitter, WindowMetrics

_start = time.monotonic()


class AgentMetricsSampler:
    """示例：从本进程的任务表采真实负载（字段按你能拿到的填）。"""

    def __init__(self, tasks: dict) -> None:
        self._tasks = tasks

    def sample(self) -> MetricsBody:
        active = sum(1 for t in self._tasks.values() if t.state == "working")
        queued = sum(1 for t in self._tasks.values() if t.state == "submitted")
        return MetricsBody(
            uptime_seconds=round(time.monotonic() - _start, 3),
            load_metrics=LoadMetrics(
                active_tasks=active,
                queued_tasks=queued,
                max_active_tasks=10,
                max_queued_tasks=50,
                cpu_usage=None,  # 有 psutil / cgroup 再填
                memory_usage=None,
            ),
            window_metrics=[
                WindowMetrics(
                    window="PT5M",
                    success_rate=98.5,
                    request_total=120,
                    request_per_second=0.4,
                    p50_latency_ms=80.0,
                    p95_latency_ms=220.0,
                    p99_latency_ms=450.0,
                ),
            ],
        )


metrics = MetricsEmitter(
    AMP_LOG_DIR / f"amp_metrics_{agent_name}.jsonl",
    aic=AIC,
    sampler=AgentMetricsSampler(tasks),
    resource={
        "service.name": f"my-partner-{agent_name}",
        "service.namespace": "acps-demo",
        "deployment.environment.name": "dev",
    },
)

# 启动后：asyncio.create_task(metrics.run_periodic(30.0))
```

窗口字符串用 ISO 8601 Duration（如 `PT1M` / `PT5M` / `PT15M`）。公共指标名约定见 SDK `metrics_catalog`；分位务必保持 `p50 ≤ p95 ≤ p99`。

### 2.3 Access — 一次请求/响应的边

**用途**：记录「谁用什么方法调了谁、花了多久、成功还是失败」。典型挂点是 HTTP `/rpc`、出站 Partner 调用——**在 `finally` 里打**，保证异常路径也有记录。

关键字段：

| 字段 | 含义 |
| --- | --- |
| `request.method` / `route` / `url` | 动词与路由；`route` 用稳定模板（如 `/rpc`），便于按 endpoint 聚合 |
| `response.statusCode` | HTTP / RPC 状态 |
| `caller` / `callee` | 双方 AIC + `serviceName` |
| `durationMs` | 总耗时 |
| `error` | 失败时的 `ErrorInfo` |
| `trace_id` / `span_id` / `correlation_id` | 链路与会话（demo 里常用 `sessionId` 作 correlation） |

下面是 Partner 入站 RPC 的教学写法（结构对齐 `demo-partner/partners/main.py`）：

```python
import time

from acps_sdk.amp import (
    AccessBody,
    AccessEmitter,
    AccessParticipant,
    AccessRequest,
    AccessResponse,
    ErrorInfo,
)

access = AccessEmitter(AMP_LOG_DIR / f"amp_access_{agent_name}.jsonl", aic=AIC)


async def handle_rpc(command, request_headers: dict[str, str]) -> object:
    t0 = time.monotonic()
    request_bytes = len(command.model_dump_json().encode("utf-8"))
    resp = None
    status_code = 200
    error_info = None
    try:
        resp = await dispatch(command)
        status_code = status_from_rpc_response(resp)
        error_info = error_info_from_rpc_response(resp)
        return resp
    except Exception as exc:
        status_code = 500
        error_info = ErrorInfo(code=500, message=str(exc))
        raise
    finally:
        duration_ms = int((time.monotonic() - t0) * 1000)
        response_bytes = (
            len(resp.model_dump_json().encode("utf-8")) if resp is not None else 0
        )
        method = str(command.command)
        body = AccessBody(
            request=AccessRequest(
                method=method,
                url="/rpc",
                route="/rpc",
                headers=request_headers,
                bodySizeBytes=request_bytes,
            ),
            response=AccessResponse(statusCode=status_code, bodySizeBytes=response_bytes),
            caller=AccessParticipant(aic=command.senderId, serviceName="demo-leader"),
            callee=AccessParticipant(aic=AIC, serviceName=f"demo-partner-{agent_name}"),
            error=error_info,
            durationMs=duration_ms,
        )
        try:
            await access.emit(
                body,
                trace_id=trace_id,          # 从入站 headers / 上下文取
                span_id=span_id,
                parent_span_id=parent_span_id,
                correlation_id=command.sessionId,
            )
        except Exception:
            logger.warning("Access emit failed", exc_info=True)
```

Leader 出站调用 Partner 时同样可以打 access，只是 `caller` / `callee` 对调，`url` 写成对方地址。查询时用 `acps-cli monitor access events`，也可按 `trace_id` 串整条调用链。

### 2.4 Message — 消息生命周期的边

**用途**：群组 / RabbitMQ / Kafka 场景。同一条业务消息的 **send、receive、ack/nack… 各写一条独立日志**，靠相同的 `messageId`（以及可选的 `correlationId`）在查询层拼成生命周期。

`event_type` 取值：`send` | `receive` | `ack` | `nack` | `reject` | `timeout` | `dead_letter`。结算类事件再用 `MessageSettlement` 补耗时与原因。

若走 SDK 的群组 MQ 客户端，可注入 `MessageEmitter`，由客户端在收发路径自动打点；自己组 body 时也可直接调 Emitter。组包时可参考 SDK 里群组客户端使用的组装逻辑（`acps_sdk/amp/_message_tap.py` 中的 `build_send_body` / `build_receive_body` / `build_settlement_body`）：

```python
from acps_sdk.amp import MessageBody, MessageDestination, MessageEmitter, MessageRouting, MessageSettlement

message = MessageEmitter(AMP_LOG_DIR / f"amp_message_{agent_name}.jsonl", aic=AIC)

# 生产者：发布到群组 exchange
await message.emit(
    MessageBody(
        event_type="send",
        operation_name="publish",
        system="rabbitmq",
        destination=MessageDestination(
            name="group.demo.exchange",
            kind="exchange",
            virtual_host="/",
        ),
        routing=MessageRouting(key="group.demo"),
        message_id="msg-001",
        payload_size_bytes=256,
    ),
    correlation_id="group-session-1",
    trace_id=trace_id,
)

# 消费者：收到后
await message.emit(
    MessageBody(
        event_type="receive",
        operation_name="deliver",
        system="rabbitmq",
        destination=MessageDestination(name="group.demo.exchange", kind="exchange"),
        subscription_name="partner-queue-1",
        routing=MessageRouting(key="group.demo"),
        message_id="msg-001",
        payload_size_bytes=256,
        delivery_attempt=1,
    ),
    correlation_id="group-session-1",
)

# 结算：ack（失败则 nack + reason）
await message.emit(
    MessageBody(
        event_type="ack",
        operation_name="basic.ack",
        system="rabbitmq",
        destination=MessageDestination(name="group.demo.exchange", kind="exchange"),
        subscription_name="partner-queue-1",
        message_id="msg-001",
        delivery_attempt=1,
        settlement=MessageSettlement(latency_ms=35.0),
    ),
    correlation_id="group-session-1",
)
```

查询：`message events` 按时间窗；有 `messageId` 时可查 lifecycle。不要把整段 payload 塞进日志，只记大小与标识即可。

### 2.5 System — Agent 内部事件

**用途**：没有强 schema 的内部可观测性——LLM 调用、Skill 执行、进程生命周期、内部错误等。`body` 是自由 `dict`；靠 `severity_text` / `severity_number` 和约定好的字段（如 `category`、`component`）在 Monitor 里过滤。

建议约定（demo 常用）：

- `message`：人可读一句话  
- `category`：`llm` / `skill` / `lifecycle` …  
- `component` / `module`：落点  
- `elapsed_ms`、错误时的 `error_type` / `error_message`  
- `correlation_id=task_id`：把同一任务的多条 system 串起来  

严重级别示例：成功 INFO → `severity_number=9`；失败 ERROR → `17`（与 demo 一致即可，团队内统一比「绝对值」更重要）。

```python
import time

from acps_sdk.amp import SystemEmitter

system = SystemEmitter(
    AMP_LOG_DIR / f"amp_system_{agent_name}.jsonl",
    aic=AIC,
    resource={
        "service.name": f"demo-partner-{agent_name}",
        "service.namespace": "acps-demo",
        "deployment.environment.name": "dev",
    },
)


async def call_llm(stage: str, model: str, task_id: str) -> str:
    t0 = time.monotonic()
    try:
        content = await client.chat.completions.create(...)
        elapsed = int((time.monotonic() - t0) * 1000)
        system.emit_sync(
            {
                "message": f"LLM call completed: stage={stage}, model={model}, elapsed={elapsed}ms",
                "category": "llm",
                "component": "llm_client",
                "module": stage,
                "model": model,
                "tags": {"model": model, "stage": stage, "task_id": task_id},
                "elapsed_ms": elapsed,
                "task_id": task_id,
                "token_total": getattr(getattr(response, "usage", None), "total_tokens", None),
            },
            severity_number=9,
            severity_text="INFO",
            correlation_id=task_id,
        )
        return content
    except Exception as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        system.emit_sync(
            {
                "message": f"LLM call failed: stage={stage}, model={model}, error={type(e).__name__}",
                "category": "llm",
                "component": "llm_client",
                "module": stage,
                "model": model,
                "tags": {"model": model, "stage": stage, "error_type": type(e).__name__},
                "elapsed_ms": elapsed,
                "task_id": task_id,
                "error_type": type(e).__name__,
                "error_message": str(e)[:500],
            },
            severity_number=17,
            severity_text="ERROR",
            correlation_id=task_id,
        )
        raise
```

进程启停也可各打一条 lifecycle system（见 `demo-partner` / `demo-leader` 的 main）。查询可用 `--severity-min` 只看错误，或 `--correlation-id` 跟任务。

### 2.6 Audit — 需要留痕的关键动作

**用途**：谁对什么资源做了什么、结果如何——适合任务受理/终态、权限相关操作、不可抵赖的状态变更。结构固定为 **actor / action / target / result**。

与 system 的分工：system 偏「运行时可观测」；audit 偏「事后追责与合规」。同一业务可以两者都写（例如 LLM 细节走 system，任务终态走 audit）。

**签名**：生产环境应给 `AuditEmitter` 传 `signer`（demo 优先 CA 证书，失败则回退 `audit_keys.json` / `load_signer_from_keys_json`）。未签名仍能写入，但 Monitor 可能标 `missing_public_key`。规范要求 audit 带 integrity；联调可先通链路，上线前补齐签名与信任材料。

```python
from pathlib import Path

from acps_sdk.amp import (
    AuditAction,
    AuditActor,
    AuditBody,
    AuditEmitter,
    AuditResult,
    AuditTarget,
    load_signer_from_keys_json,
)

signer = load_signer_from_keys_json(Path("config/audit_keys.json"), AIC)
# 或 CertificateAuditSigner(private_key_pem=..., cert_pem=...)

audit = AuditEmitter(
    AMP_LOG_DIR / f"amp_audit_{agent_name}.jsonl",
    aic=AIC,
    signer=signer,
)

# 收到 Start、完成 decision 之后（对齐 demo B1）
await audit.emit(
    AuditBody(
        actor=AuditActor(id=command.senderId or "unknown", type="agent"),
        action=AuditAction(name="receive_task_start", type="aip_protocol"),
        target=AuditTarget(type="task", id=task_id),
        result=AuditResult(
            status="success" if accepted else "failure",
            reason=None if accepted else reject_reason,
        ),
    ),
    trace_id=command.sessionId,
    correlation_id=task_id,
)

# 任务进入终态时（对齐 demo B2；同步状态机里用 emit_sync）
audit.emit_sync(
    AuditBody(
        actor=AuditActor(id=AIC, type="agent"),
        action=AuditAction(name="task_state_transition", type="aip_protocol"),
        target=AuditTarget(type="task", id=task_id),
        result=AuditResult(status="success", reason="completed"),
    ),
    trace_id=session_id,
    correlation_id=task_id,
)
```

`result.status` 只能是 `success` / `failure` / `unknown`。`action.name` / `action.type` 建议团队内定一套稳定词汇，便于按动作检索。

---

## 3. 中间发生了什么（黑盒）

安装部署完成后，大致是：

1. Agent 把 NDJSON 写到 `AMP_LOG_DIR`  
2. **AMP Forwarder**（Fluent Bit 等）尾追这些文件并送进平台总线  
3. **Monitor** 各 Writer 入库；heartbeat 还会参与 **alive-sync**，供 Discovery 展示存活  

你作为开发者通常**不用**改 Forwarder 配置。只要：

- 文件路径落在部署约定的目录下  
- 进程持续写出  
- `aic` 字段正确  

平台排障（Forwarder 没挂上、分区缺失等）交给运维，参见安装与 [日常运维](./install-package-day2-ops.md)。

---

## 4. 怎么观测（尾）

### 4.1 先看本地文件（最快自检）

```bash
ls -la "${AMP_LOG_DIR:-./logs}"/amp_*.jsonl
tail -n 1 "${AMP_LOG_DIR:-./logs}"/amp_heartbeat_demo.jsonl
```

文件在增长，说明「头」已经成功。

### 4.2 用 acps-cli 查 Monitor

前提：能连上已部署的 monitor-server，且 CLI 已配置好地址与鉴权（OIDC 或 local-auth，见 [CLI 参考](../references/cli-reference.md) 的 `acps-cli monitor`）。

时间窗用**北京时间（东八区）**即可，写成带 `+08:00` 的 ISO 8601；CLI 会原样交给 Monitor，服务端按带时区的时间解析。把区间换成你实际发日志前后的时段（access / message / system / audit 的 `events` / `records` 未提供完整 request 时，一般需要 `--start` / `--end`）：

```bash
# 例：2026-07-25 当天（北京时间）
START=2026-07-25T00:00:00+08:00
END=2026-07-25T23:59:59+08:00

# 健康
acps-cli monitor status

# 心跳（点查单个 AIC）
acps-cli monitor heartbeat liveness "$AIC"

# 指标快照
acps-cli monitor metrics snapshots --aic "$AIC"

# 访问 / 消息 / 系统 / 审计（按 AIC + 时间窗）
acps-cli monitor access events --aic "$AIC" --start "$START" --end "$END"
acps-cli monitor message events --start "$START" --end "$END"
acps-cli monitor system events --aic "$AIC" --start "$START" --end "$END"
acps-cli monitor audit records --aic "$AIC" --start "$START" --end "$END"
```

刚写入时可能有短暂延迟，稍等再查。更多子命令（trace、series、verify 等）见 CLI 参考。

### 4.3 Discovery 存活视图

heartbeat 被平台处理后，Discovery 的 alive 视图应能覆盖该 AIC（具体 API 见 Discovery / ADP 文档）。业务验收 Step D 会检查「Leader + Partner 的存活覆盖」。

---

## 5. 端到端故事：`business.yml` 的 Step D

这不是教你写 Ansible，而是说明：**demo 代码 + 平台 + 查询** 如何证明 AMP 可用。

1. **运维**用安装包跑通 `site.yml`，打开 demo-leader / demo-partner（见 [部署教程](./install-package-ansible-deploy.md)）。  
2. **头**：demo 进程启动时已创建六类 Emitter（Partner 见 `generic_runner.py`）。跑业务验收 A/B/C 时：  
   - RPC 路径写 **access**  
   - 群组路径写 **message**  
   - LLM / 生命周期写 **system**  
   - 周期任务写 **heartbeat** / **metrics**  
   - 任务终态等写 **audit**  
3. **中间**：Forwarder 与 Monitor 按安装约定工作（开发者不管）。  
4. **尾**：`business.yml` 的 **Step D** 轮询 Monitor：demo 相关 AIC 是否出现 metrics / system / access / message / audit 等记录，并检查 Discovery 存活。  

因此：你自己写 Agent 时，对齐的是 **同类 Emitter + 正确 AIC**，而不是去改 `business.yml`。Step D 只是平台侧的「验收剧本」。

---

## 6. 开发者自检清单

- [ ] `AMP_LOG_DIR`（或本地 `logs/`）下对应 `amp_*.jsonl` 在增长  
- [ ] 每条记录的 `aic` 与注册身份一致  
- [ ] 六类里你启用的类型，都能在 Monitor 按 AIC 查到（允许短暂延迟）  
- [ ] Heartbeat 持续一段时间后，Discovery 存活能看到自己  
- [ ] Audit 若要求验签，已配置 `signer` 且 Monitor 侧信任材料齐全  

---

## 7. 接下来做什么

- AIP 业务怎么写：[智能体快速开发指南](./agent-development.md)  
- SDK / 协议细节：`acps-sdk` 的 `acps_sdk/amp/`，以及 `acps-specs` 中 AMP 相关规范  
- CLI 查询参数：[CLI 参考 · monitor](../references/cli-reference.md)  
- 平台怎么装、怎么运维：[Ansible 部署](./install-package-ansible-deploy.md)、[日常运维](./install-package-day2-ops.md)  
- 成品装配：`demo-partner/partners/generic_runner.py`、`demo-leader` 中的 AMP 初始化与调用点  
