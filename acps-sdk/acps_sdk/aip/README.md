# AIP SDK — 智能体交互协议 (Agent Interaction Protocol) v2

AIP SDK 提供 AIP v2 协议的 Python 实现，覆盖任务命令/结果模型、JSON-RPC 交互、群组模式的 RabbitMQ 协作，以及流式与 mTLS 场景下的客户端/服务端辅助能力。

## 目录结构

```text
acps_sdk/aip/
├── __init__.py             # 根模块公共导出
├── aip_base_model.py       # AIP v2 基础数据模型
├── aip_rpc_model.py        # JSON-RPC 与 AIP RPC 数据模型
├── aip_rpc_client.py       # AIP RPC 客户端
├── aip_rpc_server.py       # FastAPI RPC 服务端辅助工具
├── aip_stream_model.py     # 流式传输数据模型
├── aip_group_model.py      # 群组模式数据模型
├── aip_group_leader.py     # 群组模式 Leader 端实现
├── aip_group_partner.py    # 群组模式 Partner 端实现
├── aip_group_auth.py       # 群组 ACL 服务客户端
├── aip_group_runtime.py    # 群组运行时命名、邀请和 AMQP URL 工具
└── README.md               # 本文件
```

## 功能概览

### 通信模式

| 模式 | 适用场景 | 传输方式 | 主要入口 |
| ---- | -------- | -------- | -------- |
| RPC 模式 | 1:1 Leader-Partner 请求/响应交互 | HTTP(S) + JSON-RPC | `AipRpcClient`；服务端辅助工具在 `aip_rpc_server.py` |
| 群组模式 | 1:N Leader 协调多 Partner 协作 | RabbitMQ (AMQP) + Fanout Exchange | `GroupLeader` / `GroupLeaderMqClient` / `GroupPartnerMqClient` |
| 流式模型 | SSE 或重连流式协议的数据建模 | JSON-RPC + 事件模型 | `aip_stream_model.py` |

### 根模块导出

`from acps_sdk.aip import ...` 当前导出以下常用类型：

| 类别 | 导出对象 |
| ---- | -------- |
| 基础模型 | `TaskState`, `TaskCommandType`, `DataItem`, `TextDataItem`, `FileDataItem`, `StructuredDataItem`, `Message`, `TaskCommand`, `TaskResult`, `TaskStatus`, `Product`, `GetCommandParams`, `StartCommandParams` |
| RPC 客户端 | `AipRpcClient` |
| 群组模型 | `ACSObject`, `GroupInfo`, `GroupMgmtCommandType`, `GroupMgmtCommand`, `GroupMgmtResult`, `GroupMemberStatus`, `RabbitMQRequest`, `RabbitMQResponse`, `RabbitMQRequestParams`, `RabbitMQServerConfig`, `AMQPConfig` |
| 群组客户端 | `GroupLeaderMqClient`, `GroupLeaderSession`, `GroupLeader`, `GroupPartnerMqClient`, `PartnerGroupSession`, `PartnerGroupState` |

RPC 请求/响应模型 `RpcRequest`、`RpcResponse`、服务端工具 `CommandHandlers` / `add_aip_rpc_router`、流式模型、mTLS 工具和部分群组邀请/运行时工具未从根模块导出，请从对应子模块导入。

### 任务命令与状态

Leader 通过 `TaskCommand` 发送命令，Partner 通过 `TaskResult` 回传任务状态和产出物。

| 类型 | 当前实现 |
| ---- | -------- |
| 命令 | `get`, `start`, `continue`, `cancel`, `complete`, `re-stream` |
| 状态 | `accepted`, `working`, `awaiting-input`, `awaiting-completion`, `completed`, `canceled`, `failed`, `rejected` |
| 数据项 | 文本 `TextDataItem`、文件 `FileDataItem`、结构化数据 `StructuredDataItem` |

典型状态流转是：`accepted -> working -> awaiting-input / awaiting-completion -> completed`，也可能进入 `failed`、`canceled` 或 `rejected` 等终态。`start` 是命令，不是任务状态。

## 使用示例

### RPC 客户端

```python
from acps_sdk.aip import AipRpcClient

client = AipRpcClient(
    partner_url="https://partner.example.com/aip/rpc",
    leader_id="1.2.156.3088.1.1.LDR001.ONT001.000001.0000",
)

result = await client.start_task(
    session_id="session-001",
    user_input="请总结这份材料",
)

await client.close()
```

### RPC 服务端

```python
from fastapi import FastAPI

from acps_sdk.aip.aip_rpc_server import CommandHandlers, add_aip_rpc_router

app = FastAPI()
handlers = CommandHandlers()

add_aip_rpc_router(app, endpoint="/aip/rpc", agent_handlers=handlers)
```

`aip_rpc_server.py` 还提供 `TaskManager` 和 `DefaultHandlers`，用于内存任务状态管理和默认命令语义；它们没有从 `acps_sdk.aip` 根模块导出。

### 群组模式

高层 Leader 使用 `GroupLeader` 管理共享 RabbitMQ 连接、群组会话、邀请和任务控制：

```python
from acps_sdk.aip import ACSObject, GroupLeader

leader = GroupLeader(
    leader_aic="1.2.156.3088.1.1.LDR001.ONT001.000001.0000",
    rabbitmq_config={
        "host": "mq.example.com",
        "port": 5672,
        "vhost": "/",
        "user": "guest",
        "password": "guest",
    },
)

session = await leader.create_group_session(
    session_id="session-001",
    initial_partners=[
        ACSObject(aic="1.2.156.3088.1.1.PTR001.ONT001.000001.0000"),
    ],
)

task_id = await leader.start_task(
    session_id=session.session_id,
    task_content="请协作完成一次调研",
)

runtime = leader.get_group_runtime(session.session_id)
await leader.dissolve_group_session(session.session_id)
await leader.close()
```

低层 `GroupLeaderMqClient` 可直接创建 RabbitMQ fanout exchange、发布任务命令、发送管理命令和解散群组；对应的 Partner 端入口是 `GroupPartnerMqClient`。Partner 可通过 `join_group()` 处理直接 RPC 邀请，也可通过 inbox 队列接收 `InboxGroupInvitation` 后调用 `join_group_from_invitation()`。

Partner 侧提供状态回传辅助方法：

```python
await partner.accept_task(task_id, session_id)
await partner.start_working(task_id, session_id)
await partner.request_input(task_id, session_id, "请补充输入")
await partner.submit_for_completion(task_id, session_id, products)
await partner.complete_task(task_id, session_id)
await partner.fail_task(task_id, session_id, "处理失败原因")
```

### mTLS 配置

```python
import ssl

client_ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
client_ssl_context.load_cert_chain(
    certfile="./certs/client.pem",
    keyfile="./certs/client.key",
)
client_ssl_context.load_verify_locations(cafile="./certs/trust-bundle.pem")
client_ssl_context.check_hostname = False  # 本地联调无 SAN/域名时可关闭
client_ssl_context.verify_mode = ssl.CERT_REQUIRED
client_ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2

server_ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
server_ssl_context.load_cert_chain(
    certfile="./certs/server.pem",
    keyfile="./certs/server.key",
)
server_ssl_context.load_verify_locations(cafile="./certs/trust-bundle.pem")
server_ssl_context.verify_mode = ssl.CERT_REQUIRED
server_ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
```

`AipRpcClient`、`AipNotificationClient`、`AipStreamClient` 以及群组模式相关客户端都可接收 `ssl_context`，用于 HTTPS + mTLS 或 AMQPS EXTERNAL 认证场景。若你要暴露自己的 HTTPS / mTLS 回调或 RPC 入口，`server_ssl_context` 需要交给实际承载 TLS 的 HTTP/ASGI 服务器或反向代理，而不是传给 SDK 的路由辅助函数本身。

## 参考

- [ACPs-spec-AIP-v02.02](../../../acps-specs/07-ACPs-spec-AIP/ACPs-spec-AIP.md) - 智能体交互协议
