[Home](../README.md)

# ACPs Agent Quick Development Guide

This document is intended for developers who want to develop Leader / Partner agents based on ACPs. It only covers the AIP interaction model and code structure, and does not repeat the content on setting up the development environment, packaging, or deployment.

For environment preparation, please see [Quick Start](../getting-started/README.md) and [Development and Testing Overview](../development/development-testing-overview.md).

## Table of Contents

1. [Quickly Develop an Agent Supporting AIP Interaction](#1-quickly-develop-an-agent-supporting-aip-interaction)
2. [Further: Start Interconnection](#2-further-start-interconnection)   
    2.1 [What Is Trusted Agent Registration](#21-what-is-trusted-agent-registration)  
    2.2 [How to Complete Trusted Agent Registration](#22-how-to-complete-trusted-agent-registration)  
3. [Go Further: Discover Agents](#3-go-further-discover-agents)

# 1. Quickly Develop an Agent Supporting AIP Interaction

AIP is Agent Interaction Protocol, used to describe how agents dispatch tasks, return status, exchange content, and complete collaboration.  

> For the complete definition of the AIP protocol, please see [ACPs AIP Protocol Specification](../../acps-specs/07-ACPs-spec-AIP/ACPs-spec-AIP.md)。

The focus of this chapter is independent development: first use `acps-sdk` to write a minimal Partner and a minimal Leader, and understand the AIP task state machine;  
then use `demo-partner` / `demo-leader` as references for more complex examples.  
Both demos are large-model-driven travel/task orchestration examples. They only represent one type of agent and should not be understood as a general framework that all ACPs agents must inherit from.  

## 1.1. The Most Important Things

### 1.1.1. Two Roles

| Role | Responsibility |
| --- | --- |
| Leader | Receive user input, select a Partner, create tasks, poll status, and decide whether to continue, complete, or cancel |
| Partner | Receive the Leader's task commands, execute its own capabilities, and return task status, issues, or deliverables |

Leader and Partner are protocol roles, not fixed code frameworks. A simplest Leader can be just a Python script; a simplest Partner can be just a FastAPI service. Only complex systems need planners, discovery services, group management, persistence, Web UI, or large language models.

### 1.1.2. Two Main Interaction Modes

Direct RPC mode is the easiest mode to understand:

```text
Leader -> TaskCommand(start) -> Partner /rpc
Leader <- TaskResult(accepted / working / awaiting-input / awaiting-completion / completed ...)
Leader -> TaskCommand(get / continue / complete / cancel) -> Partner /rpc
```

Group mode is used for multi-party group collaboration:

```text
Leader creates group -> Partner joins group -> Each member publishes and receives TaskCommand / TaskResult through RabbitMQ
```

The two modes reuse the same set of core AIP data objects. The main difference is the transport path: Direct RPC uses the Partner's HTTP RPC endpoint, while Group mode uses the MQ group session. For independent development, it is recommended to get Direct RPC working first, and then integrate Group mode.

### 1.1.3. Core Data Objects

The basic models of the AIP SDK are in `acps_sdk.aip.aip_base_model`. Commonly used objects are as follows:

| Object | Description |
| --- | --- |
| `Message` | Base class for all AIP messages, containing `id`, `sentAt`, `senderRole`, `senderId`, `sessionId`, `groupId`, `dataItems` |
| `TaskCommand` | Task command sent from Leader to Partner, containing `command` and `taskId` |
| `TaskResult` | Task status and deliverables returned by Partner to Leader |
| `TaskStatus` | Current task status and the `dataItems` attached to the status |
| `Product` | Deliverable produced by Partner |
| `TextDataItem` / `FileDataItem` / `StructuredDataItem` | Text, file, and structured data |

Common commands:

| Command | Meaning |
| --- | --- |
| `start` | Create and start a task |
| `get` | Get the current status of a task |
| `continue` | Provide additional information to a task waiting for input or confirmation |
| `complete` | Confirm the deliverables from Partner and end the task |
| `cancel` | Cancel the task |

Common statuses:

| Status | Meaning |
| --- | --- |
| `accepted` | Partner has accepted the task |
| `working` | Partner is processing the task |
| `awaiting-input` | Partner needs additional information from Leader or the user |
| `awaiting-completion` | Partner has generated deliverables and is waiting for confirmation from Leader |
| `completed` | Task completed |
| `failed` / `rejected` / `canceled` | Failed, rejected, or canceled |

### 1.1.4. Minimal State Machine

A minimal Partner does not need asynchronous background tasks. After receiving `start`, it can directly produce a result and enter `awaiting-completion`:

```text
start -> awaiting-completion -> complete -> completed
```

A slightly more complex Partner will go through:

```text
start -> accepted -> working -> awaiting-input -> continue -> working -> awaiting-completion -> complete -> completed
```

When writing code, remember: accepted and working are intermediate states, and Leader usually continues polling; awaiting-input and awaiting-completion are stable waiting states that require the next action from Leader or the user; completed, failed, rejected, and canceled are terminal states.

## 1.2. Which Parts of the SDK to Read First

When developing AIP code, it is recommended to read these files first:

| File | Focus |
| --- | --- |
| [acps-sdk/acps_sdk/aip/aip_base_model.py](../../acps-sdk/acps_sdk/aip/aip_base_model.py) | AIP base objects, commands, statuses, and data items |
| [acps-sdk/acps_sdk/aip/aip_rpc_model.py](../../acps-sdk/acps_sdk/aip/aip_rpc_model.py) | JSON-RPC request and response wrapper structures |
| [acps-sdk/acps_sdk/aip/aip_rpc_client.py](../../acps-sdk/acps_sdk/aip/aip_rpc_client.py) | Leader-side Direct RPC client |
| [acps-sdk/acps_sdk/aip/aip_rpc_server.py](../../acps-sdk/acps_sdk/aip/aip_rpc_server.py) | Partner-side command handling framework, `CommandHandlers`, `TaskManager`, and `add_aip_rpc_router` |
| [acps-sdk/acps_sdk/aip/aip_group_leader.py](../../acps-sdk/acps_sdk/aip/aip_group_leader.py) | Leader-side Group mode client |
| [acps-sdk/acps_sdk/aip/aip_group_partner.py](../../acps-sdk/acps_sdk/aip/aip_group_partner.py) | Partner-side Group mode client |

The SDK's boundaries are clear: it provides protocol objects, transport clients, and basic processing frameworks; business-level intent recognition, tool invocation, rule evaluation, result aggregation, and state persistence are implemented by your own Leader / Partner.

## 1.3. Independently Develop a Minimal Partner

The Partner below does not depend on `demo-partner` or a large language model. It is simply a FastAPI service that receives AIP RPC requests through `/rpc`; after receiving `start`, it echoes the user's input as a `Product` and waits for Leader to execute `complete`.

### 1.3.1. Minimal Directory

```text
minimal-aip/
  partner.py
  leader.py
```

Python environment setup is not covered here; as long as the runtime environment can import `fastapi`, `uvicorn`, and `acps_sdk`, it is sufficient.

### 1.3.2. `partner.py`

```python
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
from fastapi import FastAPI


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

Start this Partner:

```bash
uvicorn partner:app --host 0.0.0.0 --port 8011
```

This example demonstrates several basic points:

- Partner must receive a `TaskCommand` and return a `TaskResult`.
- Partner can decide whether to accept, reject, wait for input, wait for completion, or fail.
- `awaiting-completion` means the deliverables are ready, but Leader still needs to send `complete` before entering `completed`.
- `TaskManager` is only an in-memory example store provided by the SDK; a real service can replace it with a database, cache, or its own task table.

## 1.4. Independently Develop a Minimal Leader

The Leader below does not depend on `demo-leader`, nor does it depend on a large language model or Discovery. It directly knows the Partner's `/rpc` address, initiates tasks through `AipRpcClient`, and handles the status.

### 1.4.1. `leader.py`

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

        if task.status.state in (TaskState.Failed, TaskState.Rejected, TaskState.Canceled):
            print("任务未完成：")
            _print_data_items(task.status.dataItems)

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
```

run：

```bash
python leader.py
```

This is the responsibility of the minimal Leader: create tasks, observe their status, provide additional input when necessary, confirm outputs, or handle failures. A real Leader can build on this foundation by adding multi-Partner selection, concurrent execution, Discovery queries, result aggregation, frontend APIs, persistence, and secure communication.

## 1.5. What You Should Decide Yourself When Developing Independently

Do not start by copying the planner, prompts, and LLM pipeline from the demo into your own agent. When developing independently, first answer the following questions:

| Question | Description |
| --- | --- |
| What is the Partner's capability boundary? | When a task falls outside its capabilities, it should return `rejected` rather than remain in `working` for an extended period. |
| Does task state need to be persisted? | The example can use in-memory storage, while production services typically require a database or reliable persistent storage. |
| When should `awaiting-input` occur? | The Leader can provide additional input when information is insufficient, permissions are missing, or parameters are incomplete. |
| How should the output of `awaiting-completion` be represented? | Use `TextDataItem` for text, `FileDataItem` for files, and `StructuredDataItem` for structured results. |
| Are modifications allowed after `complete`? | Usually not; once a task enters a terminal state, it should remain idempotent. |
| Is mTLS required? | A minimal local example can start with HTTP, while deployment environments can later integrate certificates and HTTPS. |
| Is Group mode required? | For a single Partner or simple multi-Partner orchestration, start with Direct RPC; introduce RabbitMQ when shared group context is required. |

The general development path can be understood as:

```text
Minimal Direct RPC -> Multi-command state machine -> Persistent task table -> mTLS -> Multi-Partner orchestration -> Discovery / Group / UI
```

1.6. What demo-leader / demo-partner Are Suitable for
`demo-leader` and `demo-partner` are not minimal AIP frameworks. They are complex examples in ACPs designed for demonstrations and end-to-end validation. Their characteristics include:

- They rely on LLMs for intent recognition, planning, analysis, completion gating, and result aggregation.

- They rely on comprehensive system capabilities such as configurable scenarios, prompts, ACS, mTLS, RabbitMQ, and Discovery.

- They cover both Direct RPC and Group modes.

- They are suitable for demonstrating multi-Agent collaboration rather than serving as a mandatory base class for every agent project.

If your agent is rule-based, tool-based, retrieval-based, or implements a deterministic workflow, you should generally start with the minimal Leader / Partner structure described in this document and introduce SDK capabilities as needed, rather than copying a large amount of LLM orchestration code from the demo.

1.6.1. Reference demo-partner
When you need to implement "multiple configurable Partner Agents", you can refer to demo-partner:

```text
demo-partner/partners/
  main.py
  generic_runner.py
  group_handler.py
  online/
    <agent_name>/
      acs.json
      config.toml
      prompts.toml
      skills.toml
```

Points worth referencing:

- `partners/main.py`: How to scan multiple Agent directories and start each Agent on an independent port.
- `partners/generic_runner.py`: How to organize the `start` / `get` / `continue` / `complete` / `cancel` handler functions.
- `partners/group_handler.py`: How to have a Partner join a Group and broadcast task status back to MQ.
- `partners/online/*/acs.json`: How to describe Agent capabilities and endpoints.
- `partners/online/*/config.toml`: How to configure runtime parameters such as ports, mTLS, LLM profiles, and RabbitMQ.

However, if your Partner does not require an LLM, does not need multiple online Agents, and does not use the prompt / skill structure from the travel demo, you should not copy the full complexity of `generic_runner.py`.

### 1.6.2. Reference `demo-leader`

When you need to implement an "LLM-driven multi-Partner orchestration Leader", you can refer to `demo-leader`:

```text
demo-leader/leader/
  main.py
  assistant/
    api/routes.py
    core/orchestrator.py
    core/planner.py
    core/executor.py
    core/group_manager.py
    core/group_executor.py
    core/completion_gate.py
    core/aggregator.py
    services/discovery_client.py
```

Points worth referencing:

- `leader/main.py`: How to initialize core components within the FastAPI lifespan.
- `assistant/api/routes.py`: How to route user requests into the Leader orchestrator.
- `core/orchestrator.py`: How to connect the session, intent recognition, planning, execution, follow-up questions, completion confirmation, and aggregation stages.
- `core/executor.py`: How to use `AipRpcClient` to concurrently dispatch `start` commands and then poll Partner statuses.
- `core/completion_gate.py`: How to handle `awaiting-completion` and decide whether to call `complete` or `continue`.
- `core/group_manager.py` / `core/group_executor.py`: How to organize Group mode.
- `services/discovery_client.py`: How to query Partner ACS information from Discovery.

If your Leader only needs to call one or a few fixed Partners, you can directly extend the minimal Leader example in this document; there is no need to introduce the full LLM layering from `demo-leader`.

## 1.7. When to Introduce Group Mode

Group mode is suitable for scenarios such as:

- Multiple Partners need to see the same task context.
- Messages between Partners need to be broadcast to the group or sent to specific members.
- The Leader does not want to perform only a set of independent one-to-one RPC calls, but instead wants to maintain a collaborative session.

Group mode still uses `TaskCommand` and `TaskResult`, but messages are transmitted through RabbitMQ group sessions. During development, pay particular attention to the following:

- `groupId` must be propagated throughout the same group of tasks.
- `mentions` is used to indicate whether a message is intended for all members or specific members.
- After joining a group, the Partner needs to broadcast task status changes back to the group.
- The Leader is responsible for the group lifecycle and should release the group when the task ends or the session expires.

Relevant entry points:

- SDK Leader：`acps_sdk.aip.aip_group_leader`
- SDK Partner：`acps_sdk.aip.aip_group_partner`
- demo Leader：`demo-leader/leader/assistant/core/group_manager.py`、`demo-leader/leader/assistant/core/group_executor.py`
- demo Partner：`demo-partner/partners/group_handler.py`

# 1.8. How to Validate During Development

This document does not repeat the environment setup process, but after completing code changes, you should at least run tests according to the scope of the changes:

```bash
just test bootstrap
just test unit
just test integration
just test e2e
just qa
```

For an independent project, testing should focus on:

- Acceptance, rejection, and missing-parameter input waiting for `start`.
- Idempotent reads for `get`.
- `continue` should only take effect when the task is in `awaiting-input` or `awaiting-completion`.
- `complete` should only take effect when the task is in `awaiting-completion`.
- Tasks in terminal states should not be accidentally modified.
- The structure of `products` and `status.dataItems` should comply with the AIP model.

If you modify demo code, prioritize running the `demo-partner` unit and integration tests for the Partner state machine; for Leader orchestration, planning, completion gates, or aggregation logic, prioritize the `demo-leader` unit, API, integration, and e2e tests. For real cross-service integration testing and CLI-level end-to-end validation, refer back to the testing-layer description in [Development and Testing Overview](../development/development-testing-overview.md).

## 1.9. What to Read Next

- For detailed AIP SDK references, read [tutorials/aip-sdk-tutorial.md](./aip-sdk-tutorial.md).
- To understand AIP data objects, read [acps-sdk/acps_sdk/aip/aip_base_model.py](../../acps-sdk/acps_sdk/aip/aip_base_model.py).
- To understand minimal Partner RPC bindings, read [acps-sdk/acps_sdk/aip/aip_rpc_server.py](../../acps-sdk/acps_sdk/aip/aip_rpc_server.py).
- To understand minimal Leader RPC calls, read [acps-sdk/acps_sdk/aip/aip_rpc_client.py](../../acps-sdk/acps_sdk/aip/aip_rpc_client.py).
- To understand the complex Partner example, read [demo-partner/partners/main.py](../../demo-partner/partners/main.py), [demo-partner/partners/generic_runner.py](../../demo-partner/partners/generic_runner.py), and [demo-partner/partners/group_handler.py](../../demo-partner/partners/group_handler.py).
- To understand the complex Leader example, read [demo-leader/leader/assistant/core/orchestrator.py](../../demo-leader/leader/assistant/core/orchestrator.py), [demo-leader/leader/assistant/core/executor.py](../../demo-leader/leader/assistant/core/executor.py), and [demo-leader/leader/assistant/core/group_executor.py](../../demo-leader/leader/assistant/core/group_executor.py).

# 2. Further: Getting Connected

After completing agent development, you need to complete trusted registration, obtain the identity and certificates required to connect to the interconnected network, and expose agent capabilities, service endpoints, and other information through the Discovery Service (`discovery-server`).

- A Partner agent exposes its registration capabilities and access endpoints to the Discovery Service (`discovery-server`) through the trusted registration process.
- A Leader agent finds one or more required Partner agents through the Discovery Service (`discovery-server`), and then collaborates with them through the Partner agents' endpoints.

While reading this chapter, you can refer to the following resources for details:

| Keyword | Full Name | Abbreviation | Reference Document | Service / SDK Description |
|----|----|----|----|----|
| Agent Identity Code | Agent Identity Code | AIC | [ACPs-spec-AIC.md](../../acps-specs/02-ACPs-spec-AIC/ACPs-spec-AIC.md) | [acps-sdk:aic](../../acps-sdk/acps_sdk/aic/README.md) |
| Agent Capability Specification | Agent Capability Specification | ACS | [ACPs-spec-ACS.md](../../acps-specs/03-ACPs-spec-ACS/ACPs-spec-ACS.md) | [acps-sdk:acs](../../acps-sdk/acps_sdk/acs/README.md) |
| Agent Trusted Registration | Agent Trusted Registration | ATR | [ACPs-spec-ATR.md](../../acps-specs/04-ACPs-spec-ATR/ACPs-spec-ATR.md) | [registry-server](../../registry-server/README.md) |
| Certificate of Agent Identity | Certificate of Agent Identity | CAI | [ACPs-spec-ATR.md](../../acps-specs/04-ACPs-spec-ATR/ACPs-spec-ATR.md) | [ca-server](../../ca-server/README.md) |

> Note: The Discovery Service (`discovery-server`) automatically obtains agent ACS information from the Registration Service (`registry-server`). For details about this process, refer to [ACPs-spec-DSP.md](../../acps-specs/08-ACPs-spec-DSP/ACPs-spec-DSP.md).

## 2.1. What Is Agent Trusted Registration?

- **The Agent Trusted Registration (ATR) process consists of two steps:**

1. Submit the Agent Capability Specification (ACS) to the Registration Service (`registry-server`). After approval, obtain the Agent Identity Code (AIC).
2. Submit a certificate request to the Certificate Authority Service (`ca-server`) and obtain the Certificate of Agent Identity (CAI).

After obtaining the Certificate of Agent Identity (CAI) from the Certificate Authority Service (`ca-server`), save the certificate (CAI) locally on the agent. It will be used when establishing an mTLS connection.  
For example, in `demo-leader`, the certificate files are stored in `demo-leader/leader/atr`.

- **One key point: Agent Capability Specification (ACS):**

ACS contains information such as agent capabilities and access endpoints. The Discovery Service (`discovery-server`) matches ACS information against discovery requests to find agents suitable for a task. Other agents use ACS to find access endpoints.

For typical ACS examples, see [demo-leader:acs.json](../../demo-leader/leader/atr/acs.json) and [demo-partner:acs.json](../../demo-partner/partners/online/china_hotel/acs.json).

## 2.2. How to Complete Agent Trusted Registration

It is recommended to use `acps-cli` directly to complete trusted registration. For ordinary developers, the most common workflow is:

```text
Prepare acps-cli configuration -> Log in to Registry -> Save ACS draft -> Submit for review -> Wait for approval and obtain AIC -> Obtain EAB -> Apply for certificate from CA

> For detailed acps-cli usage instructions, refer to [references/cli-reference.md](../references/cli-reference.md).


