[Home](../README.md)

**[English](aip-sdk-tutorial_en.md) | [中文](aip-sdk-tutorial.md)**

# Development Tutorial for the ACPs Protocol SDK

This tutorial explains how to use `acps_sdk.aip` to develop AIP (Agent Interaction Protocol) code, focusing on the two capabilities the current SDK already delivers:

- Direct RPC mode
- Grouping RabbitMQ mode


> Reference specification: [ACPs-spec-AIP.md](../../acps-specs/07-ACPs-spec-AIP/ACPs-spec-AIP_en.md)

---

## Table of Contents

1. [Basic Concepts of the AIP Protocol](#1-basic-concepts-of-the-aip-protocol)
   - [1.1 Role Definitions](#11-role-definitions)
   - [1.2 Interaction Modes](#12-interaction-modes)
   - [1.3 Core Data Objects](#13-core-data-objects)
   - [1.4 Minimal State Machine](#14-minimal-state-machine)
2. [SDK Module Overview](#2-sdk-module-overview)
3. [Direct RPC Mode Development](#3-direct-rpc-mode-development)
   - [3.1 Partner Side](#31-partner-side)
   - [3.2 Leader Side](#32-leader-side)
   - [3.3 Key Practices](#33-key-practices)
4. [Grouping Interaction Mode Development](#4-grouping-interaction-mode-development)
   - [4.1 Partner Side](#41-partner-side)
   - [4.2 Leader Side](#42-leader-side)
   - [4.3 High-Level Wrapper GroupLeader](#43-high-level-wrapper-groupleader)
5. [Appendix](#5-appendix)

---

## 1. Basic Concepts of the AIP Protocol

AIP is the protocol layer in the ACPs protocol suite responsible for "task delegation, status reporting, and Product exchange". The AIP code in the SDK only handles protocol objects and the communication framework; it does not handle your business logic, for example:

- Intent recognition
- Tool invocation
- Task persistence
- Permission checks
- LLM inference
- Web API / UI

All of these must be implemented by your own Leader or Partner.

### 1.1 Role Definitions

| Role | Description |
| --- | --- |
| `Leader` | Creates tasks, issues commands, polls status, continues tasks, and confirms completion or cancels tasks |
| `Partner` | Receives task commands from the Leader, executes its own capabilities, and returns status and Products |

Roles are protocol roles, not a fixed framework. The simplest possible Leader can be just a Python script; the simplest possible Partner can be just a FastAPI service.

### 1.2 Interaction Modes

#### (1) Direct RPC Mode

The Leader interacts with a single Partner over HTTP(S) + JSON-RPC.

```text
Leader -> TaskCommand(start/get/continue/complete/cancel) -> Partner /rpc
Leader <- TaskResult
```

This is the easiest mode to debug and put into production, and it is the most complete plug-and-play capability in the current SDK.

#### (2) Grouping RabbitMQ Mode

The Leader creates a group Exchange; after Partners join the group queue, they broadcast `TaskCommand` / `TaskResult` / `GroupMgmtCommand` / `GroupMgmtResult` through RabbitMQ.

```text
Leader <-> RabbitMQ Exchange <-> Partner A
                              <-> Partner B
                              <-> Partner C
```

The grouping mode is a good fit for multiple Partners sharing context and state within the same session.

### 1.3 Core Data Objects

#### Message

The base class for all message types, defining the common fields:

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

The task command the Leader sends to the Partner:

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
    dataItems=[TextDataItem(text="Please recommend attractions in Beijing")],
)
```

#### TaskResult

The task status and results the Partner returns to the Leader:

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
            name="Attraction recommendations",
            dataItems=[TextDataItem(text="We recommend visiting the Forbidden City")],
        )
    ],
)
```

#### DataItem

The SDK currently supports three types of DataItem:

```python
from acps_sdk.aip import FileDataItem, StructuredDataItem, TextDataItem

text_item = TextDataItem(text="A piece of text")

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

| Enum | Wire value | Description |
| --- | --- | --- |
| `TaskCommandType.Start` | `"start"` | Create and start a task |
| `TaskCommandType.Get` | `"get"` | Get the current status of a task |
| `TaskCommandType.Continue` | `"continue"` | Supply additional input to a task that is waiting |
| `TaskCommandType.Complete` | `"complete"` | Confirm the Products and finish the task |
| `TaskCommandType.Cancel` | `"cancel"` | Cancel the task |
| `TaskCommandType.ReStream` | `"re-stream"` | Streaming reconnect command |

#### TaskState

| Enum | Wire value | Description |
| --- | --- | --- |
| `TaskState.Accepted` | `"accepted"` | The Partner has accepted the task |
| `TaskState.Working` | `"working"` | Processing |
| `TaskState.AwaitingInput` | `"awaiting-input"` | The Leader / user needs to supply more information |
| `TaskState.AwaitingCompletion` | `"awaiting-completion"` | Products have been generated and are awaiting confirmation |
| `TaskState.Completed` | `"completed"` | Completed |
| `TaskState.Failed` | `"failed"` | Failed |
| `TaskState.Canceled` | `"canceled"` | Canceled |
| `TaskState.Rejected` | `"rejected"` | Rejected |

### 1.4 Minimal State Machine

A minimal working Partner can implement just this one path:

```text
start -> awaiting-completion -> complete -> completed
```

A slightly more complete Partner often goes through:

```text
start -> accepted -> working -> awaiting-input -> continue -> working
      -> awaiting-completion -> complete -> completed
```

Three things are especially worth remembering:

- `accepted` / `working` are in-progress states; the Leader typically keeps polling or waiting for messages.
- `awaiting-input` / `awaiting-completion` are stable waiting states that require the Leader's next action.
- `completed` / `failed` / `canceled` / `rejected` are terminal states.

---

## 2. SDK Module Overview

The files under the `acps_sdk.aip` directory that are directly relevant to this tutorial are listed below:

| File | Description |
| --- | --- |
| [acps_sdk/aip/__init__.py](../../acps-sdk/acps_sdk/aip/__init__.py) | Public exports |
| [acps_sdk/aip/aip_base_model.py](../../acps-sdk/acps_sdk/aip/aip_base_model.py) | Base models such as `Message`, `TaskCommand`, `TaskResult`, and `TaskState` |
| [acps_sdk/aip/aip_rpc_model.py](../../acps-sdk/acps_sdk/aip/aip_rpc_model.py) | JSON-RPC wrapper models |
| [acps_sdk/aip/aip_rpc_client.py](../../acps-sdk/acps_sdk/aip/aip_rpc_client.py) | Leader-side RPC client |
| [acps_sdk/aip/aip_rpc_server.py](../../acps-sdk/acps_sdk/aip/aip_rpc_server.py) | Partner-side RPC handling framework |
| [acps_sdk/aip/aip_group_model.py](../../acps-sdk/acps_sdk/aip/aip_group_model.py) | Data models for the grouping mode |
| [acps_sdk/aip/aip_group_leader.py](../../acps-sdk/acps_sdk/aip/aip_group_leader.py) | Grouping-mode Leader client and high-level session wrapper |
| [acps_sdk/aip/aip_group_partner.py](../../acps-sdk/acps_sdk/aip/aip_group_partner.py) | Grouping-mode Partner client |
| [acps_sdk/aip/aip_stream_model.py](../../acps-sdk/acps_sdk/aip/aip_stream_model.py) | Streaming transmission model definitions |

---

## 3. Direct RPC Mode Development

### 3.1 Partner Side

The current SDK provides two layers of Partner-side support:

- `CommandHandlers`: maps different commands to different handlers
- `add_aip_rpc_router`: attaches handlers directly to FastAPI routes

Below is a minimal working Partner. Its behavior is simple:

- On receiving `start`, if the text is empty, it enters `awaiting-input`
- If the text contains "weather", it goes straight to `rejected`
- Otherwise it generates a text Product and enters `awaiting-completion`
- When it receives `continue` in the awaiting-input or awaiting-completion state, it updates the Product and enters `awaiting-completion`
- On receiving `complete`, it uses the SDK default logic to transition to `completed`

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
            data_items=[TextDataItem(text="Please provide the text to be processed.")],
        )
        return _with_sender(task)

    if "weather" in user_input:
        task = TaskManager.create_task(
            command,
            initial_state=TaskState.Rejected,
            data_items=[TextDataItem(text="This example Partner does not offer weather lookup.")],
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
                dataItems=[TextDataItem(text=f"Partner processed: {user_input}")],
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
                dataItems=[TextDataItem(text=f"Partner received additional input: {user_input}")],
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

Start it with:

```bash
uv run --with uvicorn uvicorn partner:app --host 0.0.0.0 --port 8011
```

The example above is based entirely on the SDK's actual interfaces. Note the following:

- `TaskManager` is only an in-memory storage example and is not suitable for production.
- `DefaultHandlers.complete` only actually advances a task to `Completed` when the current state is `AwaitingCompletion`.
- `DefaultHandlers.continue_` only performs validity checks by default; it will not write business logic for you.

### 3.2 Leader Side

The most commonly used class on the Leader side is `AipRpcClient`. It is responsible for assembling the `TaskCommand`, sending the JSON-RPC request, validating the response, and deserializing the result into a `TaskResult`.

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
            user_input="Please process this text",
        )
        print(f"start -> {task.status.state}")

        while task.status.state in (TaskState.Accepted, TaskState.Working):
            await asyncio.sleep(1)
            task = await client.get_task(task_id=task_id, session_id=session_id)
            print(f"get -> {task.status.state}")

        if task.status.state == TaskState.AwaitingInput:
            print("The Partner needs more information:")
            _print_data_items(task.status.dataItems)
            task = await client.continue_task(
                task_id=task_id,
                session_id=session_id,
                user_input="This is the information supplied by the Leader",
            )
            print(f"continue -> {task.status.state}")

        if task.status.state == TaskState.AwaitingCompletion:
            print("Partner products:")
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
            print("The task was not completed:")
            _print_data_items(task.status.dataItems)

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
```

Run:

```bash
uv run python leader.py
```

### 3.3 Key Practices

- The `task_id` argument of `AipRpcClient.start_task()` is optional; if you do not pass it, the client generates one automatically.
- `get_task()`, `continue_task()`, `complete_task()`, and `cancel_task()` all require you to pass `task_id` and `session_id` explicitly.
- `AipRpcClient` optionally accepts an `ssl_context`, for HTTPS / mTLS scenarios.
- The SDK's RPC server only handles protocol-layer parsing and state-machine assistance; you still need to wire in your own business state table, database, and asynchronous task framework.

---

## 4. Grouping Interaction Mode Development

Grouping interaction mode has two API layers:

- Low level: `GroupLeaderMqClient` / `GroupPartnerMqClient`
- High level: `GroupLeader`

If you only want to get a grouping interaction working quickly, using the low-level classes directly is the most straightforward approach; if you need to maintain multiple sessions and groups over the long term, `GroupLeader` is the better choice.

### 4.1 Partner Side

The typical flow on the Partner side is:

1. Expose an HTTP endpoint that receives the Leader's `RabbitMQRequest`
2. Create a `GroupPartnerMqClient`
3. Register the command handler before calling `join_group()`
4. After `join_group()` succeeds, cache the client keyed by `groupId`
5. Handle the `TaskCommand` messages sent within the group in the `set_command_handler()` callback

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
            dataItems=[TextDataItem(text="The group task was received and a sample result was generated.")],
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
        print(f"Received status from another Partner: {result.senderId} -> {result.status.state}")


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

The Partner side also provides a set of very handy shortcut methods:

- `accept_task()`
- `start_working()`
- `request_input()`
- `submit_for_completion()`
- `complete_task()`
- `reject_task()`
- `fail_task()`
- `cancel_task()`

They are all essentially semantic wrappers around `send_task_result()`.

### 4.2 Leader Side

If you want to control the RabbitMQ group lifecycle manually, you can use `GroupLeaderMqClient` directly:

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
            text_content="Please recommend attractions around the Forbidden City",
            mentions=["partner-urban-aic"],
        )
        print(f"task started: {task_id}")

        await asyncio.sleep(5)
    finally:
        await leader.close()


if __name__ == "__main__":
    asyncio.run(main())
```

There are two things to note here:

- `GroupLeaderMqClient.invite_partner()` returns a `PartnerConnectionInfo`
- `GroupLeaderMqClient.start_task()` returns a `task_id`

### 4.3 High-Level Wrapper GroupLeader

If you would rather not maintain the "session -> group -> MQ client" mapping yourself, `GroupLeader` is a better fit. The key methods it provides in the current SDK are:

- `create_group_session(session_id, initial_partners)`
- `invite_partner(session_id, partner_acs, partner_rpc_url=None, partner_acs_data=None)`
- `start_task(session_id, *, task_content, task_id=None, target_partners=None)`
- `continue_task(session_id, task_id, content, target_partner=None)`
- `complete_task(session_id, task_id, target_partner=None)`
- `cancel_task(session_id, task_id, reason=None, target_partner=None)`

A minimal example is shown below:

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
            task_content="Please recommend attractions around the Forbidden City",
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

Compared with the low-level classes, the high-level `GroupLeader` has several characteristics at the current implementation level:

- `invite_partner()` returns a `bool` rather than a `PartnerConnectionInfo`
- It automatically records the `TaskResult` messages it receives in `GroupLeaderSession.task_states` / `task_products` / `task_prompts`
- `GroupLeaderSession.state_update_event` can be used to wait for task state changes
- If `partner_acs_data` contains a usable AMQP inbox endpoint and `auth_service_url` is configured, the high-level wrapper prefers inbox invitations; otherwise it falls back to RPC invitations

---

## 5. Appendix

### 5.1 Current Common Error Codes

These error codes are explicitly used in the current SDK code:

| Error Code | Location | Description |
| --- | --- | --- |
| `-32700` | RPC server | JSON parsing failed |
| `-32602` | RPC server | Invalid parameters, for example a non-`start` command missing `taskId` |
| `-32001` | RPC server | Task does not exist |
| `-32020` | Group invitation | The invitation was rejected by the Partner |
| `-32021` | Group invitation | The Partner failed to connect to RabbitMQ |

### 5.2 Frequently Consulted Source Files

| File | Purpose |
| --- | --- |
| [acps_sdk/aip/aip_base_model.py](../../acps-sdk/acps_sdk/aip/aip_base_model.py) | View the command, state, DataItem, and message models |
| [acps_sdk/aip/aip_rpc_client.py](../../acps-sdk/acps_sdk/aip/aip_rpc_client.py) | View the direct interaction mode client signatures |
| [acps_sdk/aip/aip_rpc_server.py](../../acps-sdk/acps_sdk/aip/aip_rpc_server.py) | View `CommandHandlers`, `DefaultHandlers`, and `TaskManager` |
| [acps_sdk/aip/aip_group_partner.py](../../acps-sdk/acps_sdk/aip/aip_group_partner.py) | View the grouping interaction mode Partner join / publish / helper API |
| [acps_sdk/aip/aip_group_leader.py](../../acps-sdk/acps_sdk/aip/aip_group_leader.py) | View the grouping interaction mode Leader low-level and high-level APIs |
| [acps_sdk/aip/aip_group_model.py](../../acps-sdk/acps_sdk/aip/aip_group_model.py) | View the grouping interaction mode request / response / management message models |
| [acps_sdk/aip/aip_stream_model.py](../../acps-sdk/acps_sdk/aip/aip_stream_model.py) | View the streaming model definitions |

### 5.3 Related Specifications

- [ACPs-spec-AIP_en.md](../../acps-specs/07-ACPs-spec-AIP/ACPs-spec-AIP_en.md)
