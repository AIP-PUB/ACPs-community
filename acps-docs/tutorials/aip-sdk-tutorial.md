# 基于 AIP 协议 SDK 的开发教程

本教程介绍如何使用 `acps_sdk.aip` 开发 AIP（Agent Interaction Protocol，智能体交互协议）代码，重点覆盖当前 SDK 已经落地的两类能力：

- 直连 RPC 模式
- 群组 RabbitMQ 模式


> 参考规范：[ACPs-spec-AIP.md](../../acps-specs/07-ACPs-spec-AIP/ACPs-spec-AIP.md)

---

## 目录

1. [AIP 协议基本概念](#1-aip-协议基本概念)
   - [1.1 角色定义](#11-角色定义)
   - [1.2 交互模式](#12-交互模式)
   - [1.3 核心数据对象](#13-核心数据对象)
   - [1.4 最小状态机](#14-最小状态机)
2. [SDK 模块概览](#2-sdk-模块概览)
3. [直连 RPC 模式开发](#3-直连-rpc-模式开发)
   - [3.1 Partner 端](#31-partner-端)
   - [3.2 Leader 端](#32-leader-端)
   - [3.3 实践要点](#33-实践要点)
4. [群组模式开发](#4-群组模式开发)
   - [4.1 Partner 端](#41-partner-端)
   - [4.2 Leader 端](#42-leader-端)
   - [4.3 高层封装 GroupLeader](#43-高层封装-groupleader)
5. [附录](#5-附录)

---

## 1. AIP 协议基本概念

AIP 是 ACPs 协议体系中负责“任务委托、状态回传、产出物交换”的协议层。SDK 里的 AIP 代码只负责协议对象和通信框架，不负责你的业务逻辑，例如：

- 意图识别
- 工具调用
- 任务持久化
- 权限判断
- LLM 推理
- Web API / UI

这些都应由你的 Leader 或 Partner 自己实现。

### 1.1 角色定义

| 角色 | 说明 |
| --- | --- |
| `Leader` | 创建任务、下发命令、轮询状态、继续任务、确认完成或取消任务 |
| `Partner` | 接收 Leader 的任务命令，执行自己的能力，返回状态和产出物 |

角色是协议角色，不是固定框架。一个最简单的 Leader 可以只是一个 Python 脚本；一个最简单的 Partner 可以只是一个 FastAPI 服务。

### 1.2 交互模式

#### (1) 直连 RPC 模式

Leader 通过 HTTP(S) + JSON-RPC 与单个 Partner 交互。

```text
Leader -> TaskCommand(start/get/continue/complete/cancel) -> Partner /rpc
Leader <- TaskResult
```

这是最容易调试和落地的模式，也是当前 SDK 最完整的即插即用能力。

#### (2) 群组 RabbitMQ 模式

Leader 创建群组 Exchange，Partner 加入群组队列后，通过 RabbitMQ 广播 `TaskCommand` / `TaskResult` / `GroupMgmtCommand` / `GroupMgmtResult`。

```text
Leader <-> RabbitMQ Exchange <-> Partner A
                              <-> Partner B
                              <-> Partner C
```

群组模式适合多个 Partner 在同一会话里共享上下文和状态。

### 1.3 核心数据对象

#### Message

所有消息类的基类，定义了通用字段：

```python
from acps_sdk.aip import Message

class Message(BaseModel):
    type: str = "message"
    id: str
    sentAt: str
    senderRole: Literal["leader", "partner"]
    senderId: str
    mentions: Optional[Union[Literal["all"], List[str]]] = None
    dataItems: Optional[List[DataItem]] = None
    groupId: Optional[str] = None
    sessionId: Optional[str] = None
```

#### TaskCommand

Leader 发给 Partner 的任务命令：

```python
from acps_sdk.aip import TaskCommand, TaskCommandType, TextDataItem

command = TaskCommand(
    id="cmd-001",
    sentAt="2025-01-27T10:00:00+08:00",
    senderRole="leader",
    senderId="leader-aic-001",
    command=TaskCommandType.Start,
    taskId="task-001",
    sessionId="session-001",
    dataItems=[TextDataItem(text="请推荐北京的景点")],
)
```

#### TaskResult

Partner 回给 Leader 的任务状态和结果：

```python
from acps_sdk.aip import Product, TaskResult, TaskState, TaskStatus, TextDataItem

result = TaskResult(
    id="result-001",
    sentAt="2025-01-27T10:00:01+08:00",
    senderRole="partner",
    senderId="partner-aic-001",
    taskId="task-001",
    sessionId="session-001",
    status=TaskStatus(
        state=TaskState.AwaitingCompletion,
        stateChangedAt="2025-01-27T10:00:01+08:00",
    ),
    products=[
        Product(
            id="product-001",
            name="景点推荐",
            dataItems=[TextDataItem(text="推荐您参观故宫")],
        )
    ],
)
```

#### DataItem

SDK 当前支持三类数据项：

```python
from acps_sdk.aip import FileDataItem, StructuredDataItem, TextDataItem

text_item = TextDataItem(text="一段文本")

file_item = FileDataItem(
    name="image.jpg",
    mimeType="image/jpeg",
    bytes="base64-encoded-content",
)

structured_item = StructuredDataItem(
    data={"city": "Beijing", "score": 95}
)
```

#### TaskCommandType

| 枚举 | 线上的值 | 说明 |
| --- | --- | --- |
| `TaskCommandType.Start` | `"start"` | 创建并启动任务 |
| `TaskCommandType.Get` | `"get"` | 获取任务当前状态 |
| `TaskCommandType.Continue` | `"continue"` | 给等待中的任务补充输入 |
| `TaskCommandType.Complete` | `"complete"` | 确认产出物并结束任务 |
| `TaskCommandType.Cancel` | `"cancel"` | 取消任务 |
| `TaskCommandType.ReStream` | `"re-stream"` | 流式重连命令 |

#### TaskState

| 枚举 | 线上的值 | 说明 |
| --- | --- | --- |
| `TaskState.Accepted` | `"accepted"` | Partner 已接受任务 |
| `TaskState.Working` | `"working"` | 正在处理 |
| `TaskState.AwaitingInput` | `"awaiting-input"` | 需要 Leader / 用户继续补充信息 |
| `TaskState.AwaitingCompletion` | `"awaiting-completion"` | 已生成产出物，等待确认 |
| `TaskState.Completed` | `"completed"` | 完成 |
| `TaskState.Failed` | `"failed"` | 失败 |
| `TaskState.Canceled` | `"canceled"` | 取消 |
| `TaskState.Rejected` | `"rejected"` | 拒绝 |

### 1.4 最小状态机

最小可运行的 Partner 可以只实现这样一条路径：

```text
start -> awaiting-completion -> complete -> completed
```

更完整一点的 Partner 往往会经历：

```text
start -> accepted -> working -> awaiting-input -> continue -> working
      -> awaiting-completion -> complete -> completed
```

需要特别记住三件事：

- `accepted` / `working` 是过程状态，Leader 通常继续轮询或等待消息。
- `awaiting-input` / `awaiting-completion` 是稳定等待状态，需要 Leader 下一步动作。
- `completed` / `failed` / `canceled` / `rejected` 是终态。

---

## 2. SDK 模块概览

`acps_sdk.aip` 目录下与教程直接相关的文件如下：

| 文件 | 说明 |
| --- | --- |
| [acps_sdk/aip/__init__.py](../../acps-sdk/acps_sdk/aip/__init__.py) | 公共导出 |
| [acps_sdk/aip/aip_base_model.py](../../acps-sdk/acps_sdk/aip/aip_base_model.py) | `Message`、`TaskCommand`、`TaskResult`、`TaskState` 等基础模型 |
| [acps_sdk/aip/aip_rpc_model.py](../../acps-sdk/acps_sdk/aip/aip_rpc_model.py) | JSON-RPC 包装模型 |
| [acps_sdk/aip/aip_rpc_client.py](../../acps-sdk/acps_sdk/aip/aip_rpc_client.py) | Leader 侧 RPC 客户端 |
| [acps_sdk/aip/aip_rpc_server.py](../../acps-sdk/acps_sdk/aip/aip_rpc_server.py) | Partner 侧 RPC 处理框架 |
| [acps_sdk/aip/aip_group_model.py](../../acps-sdk/acps_sdk/aip/aip_group_model.py) | 群组模式数据模型 |
| [acps_sdk/aip/aip_group_leader.py](../../acps-sdk/acps_sdk/aip/aip_group_leader.py) | 群组模式 Leader 客户端与高层会话封装 |
| [acps_sdk/aip/aip_group_partner.py](../../acps-sdk/acps_sdk/aip/aip_group_partner.py) | 群组模式 Partner 客户端 |
| [acps_sdk/aip/aip_stream_model.py](../../acps-sdk/acps_sdk/aip/aip_stream_model.py) | 流式传输模型定义 |

---

## 3. 直连 RPC 模式开发

### 3.1 Partner 端

当前 SDK 提供了两层 Partner 端支持：

- `CommandHandlers`: 把不同命令映射到不同处理器
- `add_aip_rpc_router`: 直接把处理器挂到 FastAPI 路由

下面是一个最小可运行的 Partner。它的行为很简单：

- 收到 `start` 后，如果文本为空，进入 `awaiting-input`
- 如果文本里含有“天气”，直接 `rejected`
- 否则生成一个文本产出物，进入 `awaiting-completion`
- 在等待输入或等待确认状态收到 `continue` 后，更新产出物并进入 `awaiting-completion`
- 收到 `complete` 后使用 SDK 默认逻辑转成 `completed`

```python
from fastapi import FastAPI

from acps_sdk.aip import (
    Product,
    TaskCommand,
    TaskResult,
    TaskState,
    TextDataItem,
)
from acps_sdk.aip.aip_rpc_server import (
    CommandHandlers,
    DefaultHandlers,
    TaskManager,
    add_aip_rpc_router,
)


PARTNER_AIC = "example-partner-aic"

app = FastAPI(title="Minimal AIP Partner")


def _text_from(command: TaskCommand) -> str:
    for item in command.dataItems or []:
        if isinstance(item, TextDataItem):
            return item.text
    return ""


def _with_sender(task: TaskResult) -> TaskResult:
    task.senderId = PARTNER_AIC
    return task


async def on_start(command: TaskCommand, task: TaskResult | None) -> TaskResult:
    if task:
        return _with_sender(task)

    user_input = _text_from(command)

    if not user_input.strip():
        task = TaskManager.create_task(
            command,
            initial_state=TaskState.AwaitingInput,
            data_items=[TextDataItem(text="请提供要处理的文本。")],
        )
        return _with_sender(task)

    if "天气" in user_input:
        task = TaskManager.create_task(
            command,
            initial_state=TaskState.Rejected,
            data_items=[TextDataItem(text="这个示例 Partner 不提供天气查询能力。")],
        )
        return _with_sender(task)

    task = TaskManager.create_task(
        command,
        initial_state=TaskState.AwaitingCompletion,
    )
    TaskManager.set_products(
        task.taskId,
        [
            Product(
                id=f"product-{task.taskId}",
                name="echo",
                dataItems=[TextDataItem(text=f"Partner 已处理：{user_input}")],
            )
        ],
    )
    return _with_sender(TaskManager.get_task(task.taskId) or task)


async def on_continue(command: TaskCommand, task: TaskResult) -> TaskResult:
    user_input = _text_from(command)
    TaskManager.add_command_to_history(task.taskId, command)

    if task.status.state not in (
        TaskState.AwaitingInput,
        TaskState.AwaitingCompletion,
    ):
        return _with_sender(task)

    if not user_input.strip():
        return _with_sender(task)

    TaskManager.set_products(
        task.taskId,
        [
            Product(
                id=f"product-{task.taskId}",
                name="echo",
                dataItems=[TextDataItem(text=f"Partner 收到补充信息：{user_input}")],
            )
        ],
    )
    updated = TaskManager.update_task_status(task.taskId, TaskState.AwaitingCompletion)
    return _with_sender(updated)


handlers = CommandHandlers(
    on_start=on_start,
    on_get=DefaultHandlers.get,
    on_cancel=DefaultHandlers.cancel,
    on_complete=DefaultHandlers.complete,
    on_continue=on_continue,
)

add_aip_rpc_router(app, "/rpc", handlers)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "aic": PARTNER_AIC}
```

启动：

```bash
uv run --with uvicorn uvicorn partner:app --host 0.0.0.0 --port 8011
```

上面这个示例完全基于当前 SDK 的实际接口。需要注意的是：

- `TaskManager` 只是内存存储示例，不适合生产环境。
- `DefaultHandlers.complete` 只会在当前状态为 `AwaitingCompletion` 时真正把任务推进到 `Completed`。
- `DefaultHandlers.continue_` 默认只做合法性检查，不会替你写业务逻辑。

### 3.2 Leader 端

Leader 端最常用的是 `AipRpcClient`。它负责拼装 `TaskCommand`、发送 JSON-RPC 请求、校验响应，并把结果反序列化成 `TaskResult`。

```python
import asyncio
import uuid

from acps_sdk.aip import AipRpcClient, TaskState, TextDataItem


LEADER_AIC = "example-leader-aic"
PARTNER_RPC_URL = "http://localhost:8011/rpc"


def _print_data_items(items) -> None:
    for item in items or []:
        if isinstance(item, TextDataItem):
            print(item.text)


def _print_products(task) -> None:
    for product in task.products or []:
        print(f"[product] {product.name or product.id}")
        _print_data_items(product.dataItems)


async def main() -> None:
    session_id = f"session-{uuid.uuid4()}"
    task_id = f"task-{uuid.uuid4()}"

    client = AipRpcClient(
        partner_url=PARTNER_RPC_URL,
        leader_id=LEADER_AIC,
    )

    try:
        task = await client.start_task(
            session_id=session_id,
            task_id=task_id,
            user_input="请处理这段文本",
        )
        print(f"start -> {task.status.state}")

        while task.status.state in (TaskState.Accepted, TaskState.Working):
            await asyncio.sleep(1)
            task = await client.get_task(task_id=task_id, session_id=session_id)
            print(f"get -> {task.status.state}")

        if task.status.state == TaskState.AwaitingInput:
            print("Partner 需要补充信息：")
            _print_data_items(task.status.dataItems)
            task = await client.continue_task(
                task_id=task_id,
                session_id=session_id,
                user_input="这是 Leader 补充的信息",
            )
            print(f"continue -> {task.status.state}")

        if task.status.state == TaskState.AwaitingCompletion:
            print("Partner 产出物：")
            _print_products(task)
            task = await client.complete_task(
                task_id=task_id,
                session_id=session_id,
            )
            print(f"complete -> {task.status.state}")

        if task.status.state in (
            TaskState.Failed,
            TaskState.Rejected,
            TaskState.Canceled,
        ):
            print("任务未完成：")
            _print_data_items(task.status.dataItems)

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
```

运行：

```bash
uv run python leader.py
```

### 3.3 实践要点

- `AipRpcClient.start_task()` 的 `task_id` 是可选的；如果不传，客户端会自动生成。
- `get_task()`、`continue_task()`、`complete_task()`、`cancel_task()` 都要求显式传入 `task_id` 与 `session_id`。
- `AipRpcClient` 可选接收 `ssl_context`，用于 HTTPS / mTLS 场景。
- SDK 的 RPC 服务端只做协议层解析和状态机辅助；你自己的业务状态表、数据库和异步任务框架仍需自行接入。

---

## 4. 群组模式开发

群组模式有两层 API：

- 低层：`GroupLeaderMqClient` / `GroupPartnerMqClient`
- 高层：`GroupLeader`

如果你只想快速跑通一个群组交互，直接使用低层类最直观；如果你要长期维护多个 session 和群组，则更推荐 `GroupLeader`。

### 4.1 Partner 端

Partner 端的典型流程是：

1. 提供一个 HTTP 端点接收 Leader 的 `RabbitMQRequest`
2. 创建 `GroupPartnerMqClient`
3. 在 `join_group()` 之前注册命令处理器
4. `join_group()` 成功后把客户端按 `groupId` 缓存起来
5. 在 `set_command_handler()` 回调里处理群组内发来的 `TaskCommand`

```python
import uuid
from typing import Dict

from fastapi import FastAPI

from acps_sdk.aip import (
    GroupPartnerMqClient,
    Product,
    TaskCommand,
    TaskCommandType,
    TaskResult,
    TextDataItem,
)
from acps_sdk.aip.aip_group_model import (
    RabbitMQRequest,
    RabbitMQResponse,
)


app = FastAPI()
PARTNER_AIC = "partner-urban-aic"
group_clients: Dict[str, GroupPartnerMqClient] = {}


async def on_task_command(command: TaskCommand, is_mentioned: bool) -> None:
    if not is_mentioned:
        return

    if not command.groupId or not command.taskId or not command.sessionId:
        return

    client = group_clients.get(command.groupId)
    if not client:
        return

    if command.command == TaskCommandType.Start:
        await client.accept_task(command.taskId, command.sessionId)

        product = Product(
            id=f"product-{uuid.uuid4()}",
            name="reply",
            dataItems=[TextDataItem(text="已收到群组任务，并生成了示例结果。")],
        )
        await client.submit_for_completion(
            command.taskId,
            command.sessionId,
            [product],
        )

    elif command.command == TaskCommandType.Complete:
        await client.complete_task(command.taskId, command.sessionId)

    elif command.command == TaskCommandType.Cancel:
        await client.cancel_task(command.taskId, command.sessionId)


async def on_task_result(result: TaskResult) -> None:
    if result.senderId != PARTNER_AIC:
        print(f"收到其他 Partner 状态: {result.senderId} -> {result.status.state}")


@app.post("/group/rpc", response_model=RabbitMQResponse)
async def handle_group_invite(request: RabbitMQRequest) -> RabbitMQResponse:
    group_id = request.params.group.groupId
    client = GroupPartnerMqClient(partner_aic=PARTNER_AIC)
    client.set_command_handler(on_task_command)
    client.set_task_result_handler(on_task_result)

    response = await client.join_group(request)
    if response.result:
        group_clients[group_id] = client

    return response
```

Partner 端还有一组非常实用的快捷方法：

- `accept_task()`
- `start_working()`
- `request_input()`
- `submit_for_completion()`
- `complete_task()`
- `reject_task()`
- `fail_task()`
- `cancel_task()`

它们本质上都是对 `send_task_result()` 的语义化封装。

### 4.2 Leader 端

如果你想手工控制 RabbitMQ 群组生命周期，可以直接使用 `GroupLeaderMqClient`：

```python
import asyncio

from acps_sdk.aip import ACSObject, GroupLeaderMqClient, TaskResult, TaskState


async def main() -> None:
    leader = GroupLeaderMqClient(
        leader_aic="leader-example-aic",
        rabbitmq_host="localhost",
        rabbitmq_port=5672,
        rabbitmq_user="guest",
        rabbitmq_password="guest",
    )

    await leader.connect()
    group_id = await leader.create_group()
    print(f"group created: {group_id}")

    try:
        async def handle_message(message) -> None:
            if isinstance(message, TaskResult):
                print(f"task update: {message.senderId} -> {message.status.state}")
                if message.status.state == TaskState.AwaitingCompletion:
                    await leader.complete_task(
                        task_id=message.taskId,
                        session_id=message.sessionId or "session-001",
                        mentions=[message.senderId],
                    )

        leader.set_message_handler(handle_message)
        await leader.start_consuming()

        await leader.invite_partner(
            partner_acs=ACSObject(aic="partner-urban-aic"),
            partner_rpc_url="http://localhost:8011/group/rpc",
        )

        task_id = await leader.start_task(
            session_id="session-001",
            text_content="请推荐故宫周边的景点",
            mentions=["partner-urban-aic"],
        )
        print(f"task started: {task_id}")

        await asyncio.sleep(5)
    finally:
        await leader.close()


if __name__ == "__main__":
    asyncio.run(main())
```

这里要注意两点：

- `GroupLeaderMqClient.invite_partner()` 返回的是 `PartnerConnectionInfo`
- `GroupLeaderMqClient.start_task()` 返回的是 `task_id`

### 4.3 高层封装 GroupLeader

如果你不想自己维护 “session -> group -> MQ client” 的映射，更适合使用 `GroupLeader`。它在当前 SDK 中提供的关键方法是：

- `create_group_session(session_id, initial_partners)`
- `invite_partner(session_id, partner_acs, partner_rpc_url=None, partner_acs_data=None)`
- `start_task(session_id, *, task_content, task_id=None, target_partners=None)`
- `continue_task(session_id, task_id, content, target_partner=None)`
- `complete_task(session_id, task_id, target_partner=None)`
- `cancel_task(session_id, task_id, reason=None, target_partner=None)`

一个最小示例如下：

```python
import asyncio

from acps_sdk.aip import ACSObject, GroupLeader


async def main() -> None:
    group_leader = GroupLeader(
        leader_aic="leader-example-aic",
        rabbitmq_config={
            "host": "localhost",
            "port": 5672,
            "vhost": "/",
            "user": "guest",
            "password": "guest",
        },
    )

    try:
        session = await group_leader.create_group_session(
            session_id="session-001",
            initial_partners=[],
        )

        joined = await group_leader.invite_partner(
            session_id="session-001",
            partner_acs=ACSObject(aic="partner-urban-aic"),
            partner_rpc_url="http://localhost:8011/group/rpc",
        )
        print(f"partner joined: {joined}")

        task_id = await group_leader.start_task(
            session_id="session-001",
            task_content="请推荐故宫周边的景点",
            target_partners=["partner-urban-aic"],
        )

        await asyncio.wait_for(session.state_update_event.wait(), timeout=10)
        session.state_update_event.clear()

        summary = session.get_task_summary(task_id)
        print(summary)
    finally:
        await group_leader.close()


if __name__ == "__main__":
    asyncio.run(main())
```

与低层类相比，高层 `GroupLeader` 有几个当前实现层面的特点：

- `invite_partner()` 返回 `bool`，而不是 `PartnerConnectionInfo`
- 会自动把收到的 `TaskResult` 记录到 `GroupLeaderSession.task_states` / `task_products` / `task_prompts`
- `GroupLeaderSession.state_update_event` 可用于等待任务状态变化
- 若 `partner_acs_data` 中带有可用的 AMQP inbox endpoint，且 `auth_service_url` 已配置，高层封装会优先走 inbox 邀请；否则回退到 RPC 邀请

---

## 5. 附录

### 5.1 当前常见错误码

这些错误码是当前 SDK 代码里明确使用到的：

| 错误码 | 位置 | 说明 |
| --- | --- | --- |
| `-32700` | RPC 服务端 | JSON 解析失败 |
| `-32602` | RPC 服务端 | 参数无效，例如非 `start` 命令缺少 `taskId` |
| `-32001` | RPC 服务端 | 任务不存在 |
| `-32020` | 群组邀请 | 邀请被 Partner 拒绝 |
| `-32021` | 群组邀请 | Partner 连接 RabbitMQ 失败 |

### 5.2 常看的源文件

| 文件 | 用途 |
| --- | --- |
| [acps_sdk/aip/aip_base_model.py](../../acps-sdk/acps_sdk/aip/aip_base_model.py) | 查看命令、状态、数据项和消息模型 |
| [acps_sdk/aip/aip_rpc_client.py](../../acps-sdk/acps_sdk/aip/aip_rpc_client.py) | 查看直连模式客户端签名 |
| [acps_sdk/aip/aip_rpc_server.py](../../acps-sdk/acps_sdk/aip/aip_rpc_server.py) | 查看 `CommandHandlers`、`DefaultHandlers`、`TaskManager` |
| [acps_sdk/aip/aip_group_partner.py](../../acps-sdk/acps_sdk/aip/aip_group_partner.py) | 查看群组模式 Partner 的 join / publish / helper API |
| [acps_sdk/aip/aip_group_leader.py](../../acps-sdk/acps_sdk/aip/aip_group_leader.py) | 查看群组模式 Leader 的低层与高层 API |
| [acps_sdk/aip/aip_group_model.py](../../acps-sdk/acps_sdk/aip/aip_group_model.py) | 查看群组模式请求 / 响应 / 管理消息模型 |
| [acps_sdk/aip/aip_stream_model.py](../../acps-sdk/acps_sdk/aip/aip_stream_model.py) | 查看流式模型定义 |

### 5.3 相关规范

- [ACPs-spec-AIP.md](../../acps-specs/07-ACPs-spec-AIP/ACPs-spec-AIP.md)
