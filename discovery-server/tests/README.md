# 测试目录说明

## 分层约定

- `unit/`：纯单元测试，不依赖外部数据库或网络服务。
- `integration/`：验证 `discovery-server` 自身运行时与测试数据库、本地依赖装配的集成行为；外部 DSP / Registry 交互应优先通过 fake upstream、ASGI fake app、stub transport 或 contract fixture 表达，而不是依赖真实 sibling 进程。
- `e2e/`：验证受管启动的临时 `discovery-server` 实例的黑盒行为，覆盖本服务自闭环场景。

## 边界说明

- 本仓 `tests/` 不承载真实跨服务联调 e2e。
- 涉及 `discovery-server` 与真实 `registry-server`、`ca-server` 的联调工作流，应统一放在 `acps-cli/tests/e2e/` 中验证。
- 如果某个测试需要 sibling 仓库环境文件、真实 sibling 服务、跨仓 forwarder 拓扑或共享外部资产，它就不属于本仓 `integration` / `e2e` 的长期边界。
- 当前仍存在的存量跨边界场景，应按计划逐步迁移到 `acps-cli`，而不是继续在本仓扩展。

## 当前 e2e 入口

当前 `just test e2e` 会随机分配本地端口、受管启动临时测试实例、等待 `/acps-adp-v2/health`、注入 `TEST_E2E_BASE_URL` 后再执行黑盒测试；该路径依赖带 pgvector 的 shared PostgreSQL 测试库，但已不再依赖任何 sibling 仓库环境文件。

当前 `just test integration`、`just test e2e` 和 `just test all` 都会在各自套件启动前自动重建所需测试样本数据；完成 `just test bootstrap` 后，不需要再手工执行 `just prep seed test` 或 `just prep reseed test` 才能跑标准测试入口。

若未显式设置 `DISCOVERY_TEST_MODE`、`DISCOVERY_E2E_MODE`，标准 `integration` / `e2e` / `all` 入口会默认依次执行 CPU、GPU 两种模式；如需只跑单一模式，可在命令前覆盖对应环境变量。

`just test bootstrap`（以及 `just test check`）会强制确保测试环境已安装 `gpu` extra（`uv sync --extra gpu`）。这与"测试态 GPU 分支模型文件缺失时优雅降级跳过"是两类完全不同的场景，不要混淆：

- **gpu extra 未安装**（`torch`/`FlagEmbedding` 包本身缺失）：这是环境准备问题，`just test bootstrap` 应该在测试运行前就已经补齐；如果仍然遇到，说明本地环境绕过了 bootstrap，应执行 `uv sync --extra gpu` 补齐，而不是让测试跳过。
- **本地 embedding 模型路径/设备缺失**（`EMBEDDING_MODEL_PATH`、`EMBEDDING_DEVICES` 未配置）：这是测试态刻意不依赖真实大模型文件的设计取舍，语义匹配器会优雅降级跳过启动，黑盒用例继续验证 mode 切换、健康检查和非语义路径，这类降级是预期行为。

如需固定端口调试，可显式设置 `DISCOVERY_E2E_PORT`；未设置时默认使用随机空闲端口，和 `registry-server`、`ca-server` 的黑盒 e2e 工作流保持一致。

e2e 启动所需的大模型相关变量按以下顺序解析：

1. 显式设置的 `DISCOVERY_E2E_*` 环境变量。
2. 当前仓库根目录 `.env` 中对应的运行变量。
3. 仅用于服务启动与基础存活测试的最小占位值。

如果后续 e2e 用例需要真实调用 embedding 或 Discovery LLM，应优先通过 `DISCOVERY_E2E_*` 或当前仓库 `.env` 提供真实测试配置，而不是依赖外部 sibling 仓库资产。

测试 helper 现优先读取 `TEST_E2E_BASE_URL`，并在过渡期兼容 `DISCOVERY_E2E_BASE_URL`。

## skip 与 warning 约定

- 缺少 sibling 服务、缺少外部环境文件、缺少手工准备的 forwarder 拓扑，不应长期通过 `skip` 规避；这类前置条件应收敛到 bootstrap、fixture 或受管实例逻辑中。
- `skip` 只应用于未来工作、尚未实现能力或明确不支持的平台路径。
- warning 应视为待修复问题，优先在测试代码、fixture 和依赖兼容层闭环。

## 运行入口

- `just test unit`
- `just test integration`
- `just test e2e`
- `just test coverage`
- `just test`
- `just test all`

当前 `just test` / `just test all` 已可依次通过 `unit`、`integration`、`e2e` 三层测试入口；其中 `integration`、`e2e` 默认会按 CPU、GPU 两种模式各跑一遍，且标准入口按目录运行对应套件，不再输出其他测试层的 `deselected`。
