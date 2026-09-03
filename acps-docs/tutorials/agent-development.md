[首页](../README.md)

# ACPs 智能体快速开发指南

本文面向想要基于 ACPs 开发 Leader / Partner 智能体的开发者。它只讲 AIP 交互模型和代码结构，不重复开发环境搭建、打包、部署内容。

环境准备请看 [快速开始](../getting-started/README.md) 和 [开发与测试总览](../development/development-testing-overview.md)。

## 目录

1. [快速开发支持 AIP 交互的智能体](#1-快速开发支持-aip-交互的智能体)
2. [进一步：开始互联](#2-进一步开始互联)  
    2.1 [智能体可信注册是什么](#21-智能体可信注册是什么)
    2.2 [如何完成智能体可信注册](#22-如何完成智能体可信注册)
3 [再进一步：发现智能体](#3-再进一步发现智能体)

# 1. 快速开发支持 AIP 交互的智能体

AIP 是 Agent Interaction Protocol，用来描述智能体之间如何下发任务、返回状态、交换内容和完成协作。  

> AIP 协议完整定义请看 [ACPs AIP 协议规范](../../acps-specs/07-ACPs-spec-AIP/ACPs-spec-AIP.md)。

本章的重点是独立开发：先用 `acps-sdk` 写一个最小 Partner 和最小 Leader，理解 AIP 的任务状态机；  
再把 `demo-partner` / `demo-leader` 作为复杂样例参考。  
两个 demo 都是大模型驱动的旅游/任务编排示例，只代表一类智能体，不应被理解成所有 ACPs 智能体都必须继承的通用框架。  

## 1.1. 最重要的几件事

### 1.1.1. 两个角色

| 角色 | 责任 |
| --- | --- |
| Leader | 接收用户输入，选择 Partner，创建任务，轮询状态，决定继续、完成或取消 |
| Partner | 接收 Leader 的任务命令，执行自己的能力，返回任务状态、问题或产出物 |

Leader 与 Partner 是协议角色，不是固定代码框架。一个最简单的 Leader 可以只是一个 Python 脚本；一个最简单的 Partner 可以只是一个 FastAPI 服务。复杂系统才需要规划器、发现服务、群组管理、持久化、Web UI 或大模型。

### 1.1.2. 两种主要交互模式

Direct RPC 模式是最容易理解的模式：

```text
Leader -> TaskCommand(start) -> Partner /rpc
Leader <- TaskResult(accepted / working / awaiting-input / awaiting-completion / completed ...)
Leader -> TaskCommand(get / continue / complete / cancel) -> Partner /rpc
```

Group 模式用于多方群组协作：

```text
Leader 创建 group -> Partner 加入 group -> 各成员通过 RabbitMQ 发布和接收 TaskCommand / TaskResult
```

两种模式复用同一组核心 AIP 数据对象。差异主要在传输路径：Direct RPC 走 Partner 的 HTTP RPC 端点，Group 模式走 MQ 群组会话。独立开发时建议先跑通 Direct RPC，再接入 Group 模式。

### 1.1.3. 核心数据对象

AIP SDK 的基础模型在 `acps_sdk.aip.aip_base_model` 中，常用对象如下：

| 对象 | 说明 |
| --- | --- |
| `Message` | 所有 AIP 消息的基类，包含 `id`、`sentAt`、`senderRole`、`senderId`、`sessionId`、`groupId`、`dataItems` |
| `TaskCommand` | Leader 发给 Partner 的任务命令，包含 `command` 和 `taskId` |
| `TaskResult` | Partner 返回给 Leader 的任务状态和产出 |
| `TaskStatus` | 当前任务状态，以及状态附带的 `dataItems` |
| `Product` | Partner 产出物 |
| `TextDataItem` / `FileDataItem` / `StructuredDataItem` | 文本、文件、结构化数据 |

常用命令：

| 命令 | 含义 |
| --- | --- |
| `start` | 创建并启动任务 |
| `get` | 获取任务当前状态 |
| `continue` | 对等待输入或等待确认的任务继续补充信息 |
| `complete` | 确认 Partner 的产出物，结束任务 |
| `cancel` | 取消任务 |

常用状态：

| 状态 | 含义 |
| --- | --- |
| `accepted` | Partner 已接受任务 |
| `working` | Partner 正在处理 |
| `awaiting-input` | Partner 需要 Leader 或用户补充信息 |
| `awaiting-completion` | Partner 已生成产出，等待 Leader 确认 |
| `completed` | 任务完成 |
| `failed` / `rejected` / `canceled` | 失败、拒绝、取消 |

### 1.1.4. 最小状态机

最小 Partner 可以不做异步后台任务，收到 `start` 后直接产出结果并进入 `awaiting-completion`：

```text
start -> awaiting-completion -> complete -> completed
```

稍复杂一点的 Partner 会经历：

```text
start -> accepted -> working -> awaiting-input -> continue -> working -> awaiting-completion -> complete -> completed
```

写代码时要记住：`accepted` 和 `working` 是过程状态，Leader 通常继续轮询；`awaiting-input` 和 `awaiting-completion` 是稳定等待状态，需要 Leader 或用户下一步动作；`completed`、`failed`、`rejected`、`canceled` 是终态。

## 1.2. 先读 SDK 的哪几处

开发 AIP 代码时，建议先读这些文件：

| 文件 | 重点 |
| --- | --- |
| [acps-sdk/acps_sdk/aip/aip_base_model.py](../../acps-sdk/acps_sdk/aip/aip_base_model.py) | AIP 基础对象、命令、状态和 data item |
| [acps-sdk/acps_sdk/aip/aip_rpc_model.py](../../acps-sdk/acps_sdk/aip/aip_rpc_model.py) | JSON-RPC 请求和响应包裹结构 |
| [acps-sdk/acps_sdk/aip/aip_rpc_client.py](../../acps-sdk/acps_sdk/aip/aip_rpc_client.py) | Leader 侧 Direct RPC 客户端 |
| [acps-sdk/acps_sdk/aip/aip_rpc_server.py](../../acps-sdk/acps_sdk/aip/aip_rpc_server.py) | Partner 侧命令处理框架、`CommandHandlers`、`TaskManager`、`add_aip_rpc_router` |
| [acps-sdk/acps_sdk/aip/aip_group_leader.py](../../acps-sdk/acps_sdk/aip/aip_group_leader.py) | Leader 侧 Group 模式客户端 |
| [acps-sdk/acps_sdk/aip/aip_group_partner.py](../../acps-sdk/acps_sdk/aip/aip_group_partner.py) | Partner 侧 Group 模式客户端 |

SDK 的边界很清楚：它提供协议对象、传输客户端和基础处理框架；业务上的意图识别、工具调用、规则判断、结果聚合、状态持久化由你的 Leader / Partner 自己实现。

## 1.3. 独立开发一个最小 Partner

下面的 Partner 不依赖 `demo-partner`，也不依赖大模型。它只是一个 FastAPI 服务，通过 `/rpc` 接收 AIP RPC 请求；收到 `start` 后把用户输入回显成一个 `Product`，等待 Leader 执行 `complete`。

### 1.3.1. 最小目录

```text
minimal-aip/
  partner.py
  leader.py
```

这里不展开 Python 环境创建；只要运行环境能导入 `fastapi`、`uvicorn` 和 `acps_sdk` 即可。

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

启动这个 Partner：

```bash
uvicorn partner:app --host 0.0.0.0 --port 8011
```

这个例子演示了几个基本点：

- Partner 必须接收 `TaskCommand` 并返回 `TaskResult`。
- Partner 可以自己决定接受、拒绝、等待输入、等待完成或失败。
- `awaiting-completion` 表示产出物已经准备好，但还需要 Leader 发送 `complete` 才进入 `completed`。
- `TaskManager` 只是 SDK 提供的内存示例存储；真实服务可以换成数据库、缓存或自己的任务表。

## 1.4. 独立开发一个最小 Leader

下面的 Leader 不依赖 `demo-leader`，也不依赖大模型或 Discovery。它直接知道 Partner 的 `/rpc` 地址，通过 `AipRpcClient` 发起任务并处理状态。

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

运行：

```bash
python leader.py
```

这就是最小 Leader 的职责：创建任务、观察状态、在需要时补充输入、确认产出或处理失败。真实 Leader 可以在这个基础上增加多 Partner 选择、并发执行、Discovery 查询、结果聚合、前端 API、持久化和安全通信。

## 1.5. 独立开发时应该自己决定什么

不要一开始就把 demo 里的规划器、提示词和大模型链路照搬到自己的智能体里。独立开发时，先回答这些问题：

| 问题 | 说明 |
| --- | --- |
| Partner 的能力边界是什么 | 不属于能力范围时应返回 `rejected`，而不是长时间 `working` |
| 任务状态是否需要持久化 | 示例可以用内存，生产服务通常需要数据库或可靠存储 |
| `awaiting-input` 何时出现 | 信息不足、权限不足、参数缺失时可以让 Leader 补充 |
| `awaiting-completion` 的产出如何表达 | 文本用 `TextDataItem`，文件用 `FileDataItem`，结构化结果用 `StructuredDataItem` |
| `complete` 后是否还允许修改 | 通常不允许；进入终态后应保持幂等 |
| 是否需要 mTLS | 本地最小示例可以先用 HTTP，部署态再接入证书和 HTTPS |
| 是否需要 Group 模式 | 单 Partner 或简单多 Partner 编排先用 Direct RPC；需要共享群组上下文时再引入 RabbitMQ |

可以把通用开发路线理解成：

```text
最小 Direct RPC -> 多命令状态机 -> 持久化任务表 -> mTLS -> 多 Partner 编排 -> Discovery / Group / UI
```

## 1.6. demo-leader / demo-partner 适合参考什么

`demo-leader` 和 `demo-partner` 不是最小 AIP 框架。它们是 ACPs 中面向演示和端到端验证的复杂样例，特点是：

- 依赖 LLM 做意图识别、规划、分析、完成闸门和结果聚合。
- 依赖配置化场景、提示词、ACS、mTLS、RabbitMQ、Discovery 等完整系统能力。
- 同时覆盖 Direct RPC 和 Group 模式。
- 适合演示多 Agent 协作，而不是作为所有智能体项目的必选基类。

如果你的智能体是规则型、工具型、检索型、确定性工作流，通常应该先从本文的最小 Leader / Partner 结构开始，按需引入 SDK 能力，而不是从 demo 复制大量 LLM 编排代码。

### 1.6.1. 参考 demo-partner

当你要写“多个配置化 Partner Agent”时，可以参考 `demo-partner`：

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

值得参考的点：

- `partners/main.py`：如何扫描多个 Agent 目录，并为每个 Agent 启动独立端口。
- `partners/generic_runner.py`：如何组织 `start` / `get` / `continue` / `complete` / `cancel` 处理函数。
- `partners/group_handler.py`：如何让 Partner 加入 Group，并把任务状态广播回 MQ。
- `partners/online/*/acs.json`：如何描述 Agent 能力与 endpoint。
- `partners/online/*/config.toml`：如何把端口、mTLS、LLM profile、RabbitMQ 等运行参数配置化。

但如果你的 Partner 不需要 LLM，不需要多个在线 Agent，也不需要旅游示例的 prompt / skill 结构，就不要照搬 `generic_runner.py` 的全部复杂度。

### 1.6.2. 参考 demo-leader

当你要写“大模型驱动的多 Partner 编排 Leader”时，可以参考 `demo-leader`：

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

值得参考的点：

- `leader/main.py`：如何在 FastAPI lifespan 中初始化核心组件。
- `assistant/api/routes.py`：如何把用户请求接入 Leader 编排器。
- `core/orchestrator.py`：如何串联 session、意图识别、规划、执行、反问、完成确认和聚合。
- `core/executor.py`：如何用 `AipRpcClient` 并发下发 `start`，再轮询 Partner 状态。
- `core/completion_gate.py`：如何处理 `awaiting-completion`，决定 `complete` 或 `continue`。
- `core/group_manager.py` / `core/group_executor.py`：如何组织 Group 模式。
- `services/discovery_client.py`：如何从 Discovery 查询 Partner ACS。

如果你的 Leader 只需要固定调用一个或几个 Partner，可以直接使用本文最小 Leader 示例扩展；没有必要引入 demo-leader 的完整 LLM 分层。

## 1.7. Group 模式何时引入

Group 模式适合这些场景：

- 多个 Partner 需要看见同一组任务上下文。
- Partner 之间的消息需要通过群组广播或点名传递。
- Leader 不希望只做一组独立的一对一 RPC，而是希望维护一个协作会话。

Group 模式中仍然使用 `TaskCommand` 和 `TaskResult`，但消息通过 RabbitMQ 群组会话传递。开发时要特别注意：

- `groupId` 要贯穿同一组任务。
- `mentions` 用来表达消息面向所有成员还是指定成员。
- Partner 加入 group 后，需要把任务状态变化广播回 group。
- Leader 要负责 group 生命周期，任务结束或 session 过期时释放 group。

相关入口：

- SDK Leader：`acps_sdk.aip.aip_group_leader`
- SDK Partner：`acps_sdk.aip.aip_group_partner`
- demo Leader：`demo-leader/leader/assistant/core/group_manager.py`、`demo-leader/leader/assistant/core/group_executor.py`
- demo Partner：`demo-partner/partners/group_handler.py`

## 1.8. 开发时怎么验证

本文不重复环境搭建，但代码改动完成后至少应按影响范围跑测试：

```bash
just test bootstrap
just test unit
just test integration
just test e2e
just qa
```

如果是独立项目，测试重点应覆盖：

- `start` 的接受、拒绝、缺参等待输入。
- `get` 的幂等读取。
- `continue` 只在 `awaiting-input` 或 `awaiting-completion` 生效。
- `complete` 只在 `awaiting-completion` 生效。
- 终态任务不会被意外改写。
- `products` 和 `status.dataItems` 的结构符合 AIP 模型。

如果改的是 demo 代码，Partner 状态机优先跑 `demo-partner` 的单元和集成测试；Leader 编排、规划、完成闸门或聚合逻辑优先跑 `demo-leader` 的单元、API、集成和 e2e 测试。跨服务真实联调和 CLI 层端到端验证，请回到 [开发与测试总览](../development/development-testing-overview.md) 中的测试分层说明。

## 1.9. 下一步读什么  

- 详细 AIP SDK 参考，读 [tutorials/aip-sdk-tutorial.md](./aip-sdk-tutorial.md)
- 要理解 AIP 数据对象，读 [acps-sdk/acps_sdk/aip/aip_base_model.py](../../acps-sdk/acps_sdk/aip/aip_base_model.py)。
- 要理解最小 Partner RPC 绑定，读 [acps-sdk/acps_sdk/aip/aip_rpc_server.py](../../acps-sdk/acps_sdk/aip/aip_rpc_server.py)。
- 要理解最小 Leader RPC 调用，读 [acps-sdk/acps_sdk/aip/aip_rpc_client.py](../../acps-sdk/acps_sdk/aip/aip_rpc_client.py)。
- 要理解复杂 Partner 示例，读 [demo-partner/partners/main.py](../../demo-partner/partners/main.py)、[demo-partner/partners/generic_runner.py](../../demo-partner/partners/generic_runner.py) 和 [demo-partner/partners/group_handler.py](../../demo-partner/partners/group_handler.py)。
- 要理解复杂 Leader 示例，读 [demo-leader/leader/assistant/core/orchestrator.py](../../demo-leader/leader/assistant/core/orchestrator.py)、[demo-leader/leader/assistant/core/executor.py](../../demo-leader/leader/assistant/core/executor.py) 和 [demo-leader/leader/assistant/core/group_executor.py](../../demo-leader/leader/assistant/core/group_executor.py)。

# 2. 进一步：开始互联

完成智能体开发后，需要完成可信注册，取得接入互联网络的身份和证书，并通过发现服务（discovery-server）对外暴露智能体能力、服务端点（endpoint）等信息。

- 一个 partner 智能体通过可信注册过程，将自己的注册能力和访问端点（endpoints）暴露给发现服务（discovery-server）；
- 一个 leader 智能体通过发现服务（discovery-server）找到需要的一个或多个 partner 智能体，然后通过 partner 智能体的 endpoint 与之展开协作。

阅读本章过程中，如需了解细节，可以参考如下内容：

|关键词|全称|缩写|参考文档|服务/SDK说明|
|----|----|----|----|----|
|智能体身份码|Agent Identity Code|AIC|[ACPs-spec-AIC.md](../../acps-specs/02-ACPs-spec-AIC/ACPs-spec-AIC.md)|[acps-sdk:aic](../../acps-sdk/acps_sdk/aic/README.md)|
|智能体能力描述|Agent Capability Specification|ACS|[ACPs-spec-ACS.md](../../acps-specs/03-ACPs-spec-ACS/ACPs-spec-ACS.md)|[acps-sdk:acs](../../acps-sdk/acps_sdk/acs/README.md)|
|智能体可信注册|Agent Trusted Registration|ATR|[ACPs-spec-ATR.md](../../acps-specs/04-ACPs-spec-ATR/ACPs-spec-ATR.md)|[registry-server](../../registry-server/README.md)|
|智能体身份证书|Certificate of Agent Identity|CAI|[ACPs-spec-ATR.md](../../acps-specs/04-ACPs-spec-ATR/ACPs-spec-ATR.md)|[ca-server](../../ca-server/README.md)|

> 注：发现服务（discovery-server）自动从注册服务（registry-server）处获得智能体的ACS信息，了解这个过程可以参考 [ACPs-spec-DSP.md](../../acps-specs/08-ACPs-spec-DSP/ACPs-spec-DSP.md)  

## 2.1. 智能体可信注册是什么

- **智能体可信注册（ATR）过程分为两个步骤：**

1. 向注册服务（registry-server）提交智能体能力描述（ACS），批准后获得智能体身份码（AIC）；
2. 证书授权服务（ca-server）提交证书申请，获得智能体身份证书（CAI）。

在证书授权服务（ca-server）获得智能体身份证书（CAI）后，将证书（CAI）保存在智能体本地，将在建立mTLS链接时使用该证书。  
以 demo-leader 为例，证书文件将保存在 `demo-leader/leader/atr`  

- **一个要点：智能体能力描述（ACS）:**

ACS 包含智能体能力、访问端点等信息，智能体发现服务（discovery-server）通过 ACS 与发现请求匹配，找到适合任务的智能体。其它智能体通过 ACS 找到访问端点。

典型的 ACS 示例，参见 [demo-leader:acs.json](../../demo-leader/leader/atr/acs.json) 及 [demo-partner:acs.json](../../demo-partner/partners/online/china_hotel/acs.json)

## 2.2. 如何完成智能体可信注册

推荐直接使用 `acps-cli` 完成可信注册。对普通开发者来说，最常见的路径是：

```text
准备 acps-cli 配置 -> 登录 Registry -> 保存 ACS 草稿 -> 提交审核 -> 等待审核通过并拿到 AIC -> 获取 EAB -> 向 CA 申请证书
```

> acps-cli 详细使用说明参考：[references/cli-reference.md](../references/cli-reference.md)

### 2.2.1. 可信注册的完整步骤

#### 1） 登录 Registry

先登录 Registry。若账号还不存在，`auth login` 可以结合参数自动注册普通用户。  

#### 2） 提交 ACS 草稿并发起审核  

1. 准备好本地 ACS 文件后，用 `agent save` 创建或更新草稿。

2. 如果这个 ACS 代表的是本体（ontology）而不是普通实体智能体，需要加 `--ontology`。

3. 返回结果里会包含草稿对应的 `agent_id`。拿到这个 UUID 后，提交审核。

   提交后，普通开发者通常不能自己批准审核，而是等待平台管理员处理。等待期间可以反复检查状态。

4. 待服务端完成批准并更新了 ACS，再把最新状态同步回本地文件。

   这一阶段的目标是让 ACS 审核通过，并在本地 ACS 中确认已经拿到 AIC。

#### 3） 获取 EAB 并申请证书

1. 当 Agent 已通过审核并具备 AIC 后，先从 Registry 获取 EAB 凭证。

2. 然后用EAB凭证 向 证书授权服务（ca-server）申请证书。

3. 拿到证书文件后，把它们接到你的智能体本地文件夹。

- 如果你注册的是 本体智能体（ontology），并且后续要基于该 ontology 派生实体对象，可以使用 `entity derive`。这一步需要提供本体证书材料。

#### 5） 管理员命令

审核动作由管理侧命令完成：

```bash
acps-cli admin registry ...
```

普通开发者只需要知道：自己的 `agent submit` 之后，必须等管理员批准，获得 AIC 和 EAB 后才能申请证书。

### 2.2.2. 一条最常见的最小命令链

如果你要把“注册一个普通智能体并拿到证书”压缩成一条最小操作清单，通常就是：

```bash
uv run acps-cli --config ./acps-cli.toml auth login --username alice --password 'S3cret!'
uv run acps-cli --config ./acps-cli.toml agent save --acs-file ./acs.json --json
uv run acps-cli --config ./acps-cli.toml agent submit --agent-id <AGENT_UUID> --json
uv run acps-cli --config ./acps-cli.toml agent check --acs-file ./acs.json --json
uv run acps-cli --config ./acps-cli.toml cert eab fetch --aic <AIC> --output ./private/eab.json --json
uv run acps-cli --config ./acps-cli.toml cert issue --aic <AIC> --eab-file ./private/eab.json --usage clientAuth
```

# 3. 再进一步：发现智能体

一个 leader 智能体可以根据能力需求，通过智能体发现过程（Agent Discovery Protocol，ADP），找到适合任务的 partner 智能体。

## 3.1. 获取发现服务接口

发现服务 discovery-server 通过 RESTful 接口访问，接口定义见 [智能体发现（Discovery）API](../../acps-specs/06-ACPs-spec-ADP/ACPs-spec-ADP.md#4-智能体发现discoveryapi)  

discovery-server 实现提供了在线文档，服务默认端口配置是 9005，常见的访问地址如：
`http://your-discovery-server:9005/docs#/`  

注册、发现、协作，三个动作构成了智能体互联协作的最简模式。

## 3.2. 智能体发现接口示例

如下是一个最简的请求示例：  

- 请求

```bash
curl -X 'POST' \
  'http://bupt.ioa.pub:9005/acps-adp-v2/discover' \
  -H 'accept: application/json' \
  -H 'Content-Type: application/json' \
  -d '{
  "type": "explicit",
  "query": "我想去旅游",
  "limit": 5
}'
```

发现过程实现可参考 `demo-leader`

---

## 4. 再下一步：可观测性（AMP）

AIP 解决「智能体怎么协作」；若还要让协作过程可被查询（访问日志、心跳存活、审计等），请阅读 [在 Agent 中接入 AMP 可观测性](./amp-agent-observability.md)。  
那篇只讲开发者如何写 Emitter、如何查询，不讲如何搭建 AMP 基础架构。
