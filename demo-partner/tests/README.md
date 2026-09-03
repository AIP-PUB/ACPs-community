# Partner Agent 测试套件

## 测试分类

本项目包含三类测试，覆盖从单元到端到端的完整测试金字塔。

### 单元测试 (`tests/unit/`)

单元测试使用 Mock 对象模拟 LLM 服务，不依赖真实的外部服务。运行速度快，适合开发时频繁执行。

**测试覆盖：**

| 测试文件                   | 对应功能               | 说明                                        |
| -------------------------- | ---------------------- | ------------------------------------------- |
| test_generic_runner.py     | GenericRunner 核心流程 | Decision/Analysis/Production 阶段、并发控制 |
| test_task_state_machine.py | 任务状态机             | 状态转换、终态处理、消息历史                |
| test_config_loading.py     | 配置加载               | ACS/Config/Prompts 配置加载                 |

**运行方式：**

```bash
# 在项目根目录（demo-partner/）下运行
uv run pytest tests/unit/ -v

# 运行特定测试文件
uv run pytest tests/unit/test_generic_runner.py -v

# 运行特定测试类
python -m pytest tests/unit/test_generic_runner.py::TestDecisionStage -v

# 运行特定测试方法
python -m pytest tests/unit/test_generic_runner.py::TestDecisionStage::test_decision_accept -v
```

> **默认串行**：单元测试默认使用串行执行模式 (`-n 0`)，无需额外指定参数。

### 集成测试 (`tests/integration/`)

集成测试使用真实的 LLM 服务，通过 FastAPI TestClient 调用 Partner API。验证各组件协同工作的正确性。

> **原则**：不使用 Mock（Fallback 测试除外），使用真实 LLM API，通过 API 响应和内部状态双重验证。

**测试覆盖：**

| 测试文件                | 对应功能       | 说明                         |
| ----------------------- | -------------- | ---------------------------- |
| test_api_basics.py      | API 基础功能   | 健康检查、RPC 端点、请求验证 |
| test_beijing_food.py    | 北京餐饮 Agent | 意图识别、需求分析、内容生成 |
| test_beijing_rural.py   | 北京郊区 Agent | 意图识别、需求分析、内容生成 |
| test_beijing_urban.py   | 北京城区 Agent | 意图识别、需求分析、内容生成 |
| test_china_hotel.py     | 全国酒店 Agent | 意图识别、需求分析、内容生成 |
| test_china_transport.py | 全国交通 Agent | 意图识别、需求分析、内容生成 |

**运行方式：**

```bash
# 在项目根目录（demo-partner/）下运行
uv run pytest tests/integration/ -v

# 指定 worker 数量（推荐 4-8）
uv run pytest tests/integration/ -n 4 -v

# 串行运行（禁用并行，用于调试）
uv run pytest tests/integration/ -n 0 -v

# 运行特定 Agent 的测试
uv run pytest tests/integration/test_china_transport.py -v

# 运行特定测试类
uv run pytest tests/integration/test_china_transport.py::TestChinaTransportDecision -v
```

> **默认并行**：集成测试默认使用并行执行模式 (`-n auto`)，可显著缩短测试时间。使用 `-n 0` 可切换为串行模式用于调试。

### 端到端测试 (`tests/e2e/`)

端到端测试（E2E）是完全的黑盒测试，通过真实 HTTP 请求调用运行中的 Partner 服务，仅通过 API 响应验证业务正确性。

**核心特点：**

- 不使用 TestClient，使用真正的 HTTP 请求（httpx）
- 需要 Partner 服务实际运行（各 Agent 独立端口，如 9021-9025）
- 在同一个 task 下进行多轮操作，验证状态转移的一致性
- 必须**串行执行**，不能并行

**测试覆盖：**

| 测试文件                      | 说明                                         |
| ----------------------------- | -------------------------------------------- |
| test_state_machine_journey.py | **状态机旅程测试**（同一 task 多轮完整交互） |

**重点：状态机旅程测试 (`test_state_machine_journey.py`)**

状态机旅程测试在**同一个 task** 下进行多轮操作，验证完整的状态转移流程：

```
旅程1: 不完整请求 → AwaitingInput → 补充信息 → AwaitingCompletion → Completed
  START("帮我订火车票")
    → accepted → working → awaiting-input
  CONTINUE("北京到上海，1月30号")
    → working → awaiting-completion
  COMPLETE
    → completed

旅程2: 完整请求 → 直接完成
  START("查询明天北京到上海的高铁")
    → accepted → working → awaiting-completion
  COMPLETE
    → completed

旅程3: 超出范围 → 拒绝
  START("推荐北京的美食餐厅")
    → accepted → rejected

旅程4: 中途取消
  START → awaiting-input → CANCEL → canceled
```

**运行方式：**

```bash
# 在项目根目录（demo-partner/）下运行
# 先启动 Partner 服务
just dev start

# 运行 E2E 测试（默认串行执行）
uv run pytest tests/e2e/ -v -s

# 运行特定测试类
uv run pytest tests/e2e/test_state_machine_journey.py::TestStateMachineJourney_ChinaTransport -v -s

# 运行状态一致性测试（验证所有 Agent）
uv run pytest tests/e2e/test_state_machine_journey.py::TestStateTransitionConsistency -v -s
```

> **默认串行**：E2E 测试默认使用串行执行模式 (`-n 0`)，因为状态机测试需要在同一 task 下进行多轮操作，必须保证执行顺序。

## 快速开始

### 依赖安装

```bash
# 安装依赖（在项目根目录 demo-partner/ 下执行）
uv sync
```

### 运行全部测试

```bash
# 在项目根目录（demo-partner/）下运行

# 1. 运行单元测试（默认串行，无需外部服务）
uv run pytest tests/unit/ -v

# 2. 运行集成测试（默认并行，需要 LLM API 配置）
uv run pytest tests/integration/ -v

# 3. 运行 E2E 测试（默认串行，需要启动 Partner 服务）
just dev start
uv run pytest tests/e2e/ -v -s
```

### 带覆盖率报告

```bash
# 运行全部测试并生成覆盖率报告
uv run pytest tests/ --cov=partners --cov-report=html -v

# 查看覆盖率报告
open htmlcov/index.html
```

## 测试命令汇总

| 测试类型 | 命令                               | 默认模式 | 外部依赖     |
| -------- | ---------------------------------- | -------- | ------------ |
| 单元测试 | `uv run pytest tests/unit/ -v`     | 串行     | 无           |
| 集成测试 | `uv run pytest tests/integration/` | **并行** | LLM API      |
| E2E 测试 | `uv run pytest tests/e2e/ -v -s`   | 串行     | Partner 服务 |

> **说明**：pytest 配置集中在项目根目录的 `pyproject.toml` 中，默认开启 `--strict-markers`。可通过命令行 `-n auto` / `-n 0` 灵活切换并行或串行模式。

## 注意事项

1. **集成测试** 需要有效的 LLM 配置（各 Agent 的 `config.toml`）
2. **E2E 测试** 需要 Partner 服务运行（各 Agent 独立端口，见各 Agent 的 `config.toml`）
3. 测试运行时间较长时，可使用 `-x` 参数在首次失败时停止
4. 使用 `--tb=short` 简化错误输出
5. 使用 `-s` 显示 print 和 logging 输出（E2E 测试推荐）

## 状态转移规范

**关键原则：**

- `Rejected` = 请求**超出服务范围**（能力不匹配），是终态
- `AwaitingInput` = 请求**在范围内但信息不完整**，需要补充信息
- 在范围内的请求，即使信息不完整，也不应该进入 `Rejected` 状态

## 测试 Fixtures 说明

### 单元测试 Fixtures (`unit/conftest.py`)

- `mock_llm_responses`: Mock LLM 响应工厂
- `mock_openai_client_factory`: 可配置响应的 Mock OpenAI 客户端
- `test_agent_dir`: 临时测试 Agent 目录（含配置文件）
- `message_factory`: 创建测试消息
- `rpc_request_factory`: 创建测试 RPC 请求

### 集成测试 Fixtures (`integration/conftest.py`)

- `client`: FastAPI TestClient 实例
- `online_agents`: 所有在线 Agent 名称列表
- `rpc_call`: 执行 RPC 调用的辅助函数
- `wait_for_state`: 等待任务达到指定状态
- `continue_task`: 发送 CONTINUE 命令
- `get_task_context`: 获取任务内部上下文
- `assert_rpc_success`, `assert_task_state`, `assert_task_has_product`: 验证辅助

### E2E 测试 Fixtures (`e2e/conftest.py`)

- `http_client`: httpx HTTP 客户端（真实 HTTP 请求）
- `available_agents`: 可用的 Agent 列表
- `unique_ids`: 生成唯一的 task_id 和 session_id
- `rpc_helper`: RPC 调用辅助类，提供 start/get/continue_task/complete/cancel/poll_until 方法
