# Leader Agent Platform

Leader Agent Platform 是一个基于 **"通用底座 + 场景插件"** 设计理念的智能体协作平台。它作为核心协调者（Leader），负责接收用户请求、动态加载场景配置、编排多个 Partner Agent 协同工作，并最终整合结果交付给用户。通信遵循 AIP（智能体交互协议），支持 Direct RPC 和 Group（消息队列）两种模式。

## 1. 目录结构

```
leader/
├── main.py                    # 应用入口（FastAPI）
├── config.toml                # 平台级系统配置（LLM Profile、端口、发现服务等）
├── acs.json                   # Leader 自身的 ACS 定义
├── assistant/                 # 平台核心代码
│   ├── config.py              # 配置加载
│   ├── api/                   # HTTP 接口层（/api/v1/submit, /api/v1/result 等）
│   │   ├── routes.py          # 路由定义
│   │   └── schemas.py         # 请求/响应数据模型
│   ├── core/                  # 核心编排与业务逻辑
│   │   ├── orchestrator.py    # 主编排器（串联意图分析→任务编排→执行→整合）
│   │   ├── session_manager.py # Session 生命周期管理
│   │   ├── intent_analyzer.py # 意图分析（LLM-1）
│   │   ├── planner.py         # 全量规划与维度拆解（LLM-2）
│   │   ├── input_router.py    # 用户补充输入分发（LLM-4）
│   │   ├── completion_gate.py # 产出确认与冲突检测（LLM-5）
│   │   ├── aggregator.py      # 最终结果整合（LLM-6）
│   │   ├── clarification_merger.py  # 反问合并（LLM-3）
│   │   ├── history_compressor.py    # 历史上下文压缩（LLM-7）
│   │   ├── executor.py              # Direct RPC 模式任务执行
│   │   ├── group_executor.py        # Group 模式任务执行
│   │   ├── group_manager.py         # Group 模式群组管理
│   │   └── task_execution_manager.py # 任务执行管理
│   ├── llm/                   # LLM 调用封装
│   │   ├── client.py          # OpenAI 兼容客户端
│   │   └── schemas.py         # LLM 相关数据模型
│   ├── models/                # 数据模型定义
│   │   ├── session.py         # Session 模型
│   │   ├── task.py            # Task 模型
│   │   ├── partner.py         # Partner 模型
│   │   ├── intent.py          # 意图分析模型
│   │   ├── aip.py             # AIP 协议相关模型
│   │   └── exceptions.py      # 异常定义
│   └── services/              # 外部服务集成
│       ├── scenario_loader.py # 场景配置加载
│       └── discovery_client.py # ADP 发现服务客户端
└── scenario/                  # 场景配置集合
    ├── base/                  # 基础人设 + 通用场景（兜底行为）
    │   └── prompts.toml
    ├── expert/                # 专业场景插件
    │   ├── tour/              # 旅游场景
    │   │   ├── domain.toml    # 场景元数据（维度定义、一致性规则等）
    │   │   ├── prompts.toml   # 场景提示词（Intent/Planning/Aggregation）
    │   │   └── *.json         # 静态 ACS 文件（Partner 能力描述）
    │   └── divination/        # 其它场景示例
    │       ├── domain.toml
    │       └── prompts.toml
    └── offline/               # 编辑区（用于配置的原子化更新）
```

### 核心设计

- **通用底座**：`assistant/` 目录下的代码提供 Session 管理、AIP 协议通信、任务状态机、LLM 调用封装等通用能力。
- **场景插件**：`scenario/` 目录下通过配置文件定义特定领域的意图分析、任务拆解、Partner 选择和结果整合逻辑。新增场景只需在 `scenario/expert/` 下添加配置目录，无需修改平台代码。

## 2. 配置说明

### config.toml

平台级系统配置，包含以下主要部分：

| 配置段               | 说明                                                       |
| -------------------- | ---------------------------------------------------------- |
| `[app]`              | Leader ACS 描述文件路径，程序会从中解析 `aic` 作为自身标识 |
| `[logging]`          | 全局日志级别                                               |
| `[logging.packages]` | 指定包及其子模块的日志级别覆盖项                           |
| `[uvicorn]`          | HTTP 服务监听地址、端口和热重载开关                        |
| `[mtls]`             | Leader 访问 Partner HTTPS 端点时使用的客户端证书配置       |
| `[llm.*]`            | LLM Profile 配置，示例中包含 `fast`、`default`、`pro` 三组 |
| `[discovery]`        | ADP 发现服务地址、超时时间和返回数量限制                   |
| `[group]`            | Group 模式开关、状态探测、等待超时和重试等参数             |
| `[rabbitmq]`         | RabbitMQ 连接参数，用于 Group 模式消息通信                 |

`config.toml` 已作为正式配置提交到仓库。实际 LLM 参数通过仓库根目录 `.env` 注入；本地开发默认 OIDC 配置则直接保存在
`config.toml` 与 `web_app/runtime-config.js`，以便与 `acps-infra/dev-infra` 的 Keycloak 开箱对齐。推荐使用仓库根目录的
`just prep env` 或 `just dev start` 自动准备本地环境：

```bash
just prep env
```

至少需要填写以下变量：

- `LEADER_LLM_FAST_API_KEY`
- `LEADER_LLM_FAST_BASE_URL`
- `LEADER_LLM_FAST_MODEL`
- `LEADER_LLM_DEFAULT_API_KEY`
- `LEADER_LLM_DEFAULT_BASE_URL`
- `LEADER_LLM_DEFAULT_MODEL`
- `LEADER_LLM_PRO_API_KEY`
- `LEADER_LLM_PRO_BASE_URL`
- `LEADER_LLM_PRO_MODEL`

`[mtls]` 段固定引用 `atr/client.pem`、`atr/client.key` 和 `atr/trust-bundle.pem`，证书脚本只覆盖文件内容，不再改写 `config.toml`。

`leader/config.toml` 中仅对 LLM 敏感信息保留 `llm.*.*_env` 形式的环境变量名引用，避免把具体密钥写入仓库；端口、
OIDC issuer / audience / algorithm 等稳定的本地开发默认值直接提交到仓库维护。

### 场景配置

- `scenario/base/prompts.toml`：定义基础人设和通用场景下的提示词。
- `scenario/expert/<场景名>/domain.toml`：定义场景元数据，包括维度拆分逻辑、Partner 映射关系和跨维度一致性规则。
- `scenario/expert/<场景名>/prompts.toml`：定义场景特定的 Intent / Planning / Aggregation 提示词。
- `scenario/expert/<场景名>/*.json`：静态 ACS 文件，描述该场景优先使用的 Partner 能力。

## 3. 运行

从仓库根目录运行（推荐）：

```bash
# 后台启动 Leader API + Web UI（启动前自动准备环境）
just dev start

# 仅以前台模式观察 Leader API 启动日志
just dev start fg
```

或通过 uvicorn 直接启动：

```bash
uv run uvicorn leader.main:app --host 0.0.0.0 --port 9031 --reload
```

> **注意**：Leader 依赖 Discovery、MQ Auth、Partner 等本地联调服务；默认地址见仓库根目录 `.env.example`。Partner 可通过兄弟项目 `demo-partner` 的 `just dev` 工作流启动。

## 4. API 接口

Leader 对外提供API接口，供 Web 前端调用，以下是主要 HTTP 接口：

| 方法 | 路径                          | 说明                          |
| ---- | ----------------------------- | ----------------------------- |
| POST | `/api/v1/submit`              | 提交用户请求（创建/延续会话） |
| GET  | `/api/v1/result/{session_id}` | 轮询任务状态和结果            |

### 交互流程

1. 客户端调用 `POST /api/v1/submit` 提交用户输入，获得 `sessionId` 和 `activeTaskId`。
2. 客户端使用 `GET /api/v1/result/{session_id}` 轮询结果。
3. 响应状态包括：`pending`、`running`、`awaiting_input`（反问）、`completed`、`failed`。
4. 收到 `awaiting_input` 时，客户端再次调用 `/submit`（带 `sessionId`）提交补充信息。
