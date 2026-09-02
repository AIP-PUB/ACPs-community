# 测试目录说明

## 分层约定

- `unit/`：纯单元测试，只验证本仓逻辑，不依赖真实数据库或真实网络服务。
- `integration/`：验证 `registry-server` 自身运行时与测试数据库的集成行为；外部 CA / 其它 sibling 服务必须通过 fake peer、stub transport 或 contract fixture 表达，而不是要求真实 sibling 进程同时运行。
- `e2e/`：验证受管启动的临时 `registry-server` 实例的黑盒行为，覆盖 public 平面、真实 mTLS `9002` listener、认证、Agent 生命周期、Webhook CRUD 等本服务自闭环能力。

## 边界说明

- 本仓 `tests/` 默认不承载需要同时拉起多个 sibling 业务服务的真实跨服务联调 e2e。
- 对 `registry-server` 自身鉴权边界必需、且可由受管 dev-infra 稳定提供的基础设施依赖（如 Keycloak），允许在本仓补充黑盒联调用例。
- 涉及 `registry-server` 与 `ca-server`、`discovery-server` 之间真实交互的联调链路，统一放在 `acps-cli/tests/e2e/` 中验证。
- 如果某个场景需要同时拉起真实 sibling 服务、共享 sibling 仓库 `.env`、依赖跨仓拓扑协作，说明它不属于本仓 `integration` / `e2e`，而属于 `acps-cli` 的联调测试范围。

## 运行入口

- `just test unit`：纯单元层。
- `just test integration`：本服务 + 测试数据库 + fake peer。
- `just test e2e`：受管启动临时 `registry-server` 测试实例，并注入 `TEST_E2E_BASE_URL`、`TEST_E2E_MTLS_BASE_URL` 与 mTLS client 证书环境变量。
- `just test e2e -- tests/e2e/test_oidc_keycloak_flow.py`：仅运行 OIDC 黑盒用例；共享入口会按 `local -> oidc` profile 编排，并受管启动 Keycloak 与临时 `registry-server` 测试实例。
- `just test`：顺序执行 `unit -> integration -> e2e`。
- 执行黑盒 e2e 前请先通过 `just prep certs` 或 `just test bootstrap` 准备本地 `certs/` 下的开发 PKI 产物。
- 当前本仓黑盒 e2e 的目标是不依赖 sibling 仓库环境文件；如需真实 sibling 业务服务联调，请切换到 `acps-cli`。

## skip 与 warning 约定

- 缺少 sibling 服务、缺少手工联调环境、缺少外部 `.env` 一类问题，不应长期通过 `skip` 规避；这类前置条件应通过 bootstrap、fixture 或受管测试实例消化。
- `skip` 只应用于未来工作、尚未实现能力或明确不支持的平台路径。
- warning 是测试健康度的一部分；如发现 warning，应优先修复测试代码、fixture 或依赖兼容问题，而不是长期忽略。

## 调试建议

- 如需调试本仓黑盒 e2e，可优先使用 `just test e2e`，让测试入口自动拉起临时实例。
- 如需手工直连 `9002`，请使用 `certs/client.pem` / `certs/client.key` 与 `certs/trust-bundle.pem` 完成真实 mTLS 握手。
- 如需调试真实跨服务行为，请切换到 `acps-cli` 仓库，并按其 README 中的联调测试说明运行。
