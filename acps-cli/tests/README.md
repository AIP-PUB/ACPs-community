# acps-cli Tests

`acps-cli` 的测试分成三层：

- `tests/unit/`：纯本地单元测试，使用 mock/stub，不依赖外部服务。
- `tests/integration/`：验证 CLI 参数、配置、输出，以及面向单个服务的命令契约。
- `tests/e2e/`：验证真实跨服务工作流、状态传播和多服务联调拓扑。

其中 monitor 相关测试采用两层补位：

- `tests/integration/test_monitor_cli_live.py`：真实 monitor-server + 直写读模型，用于验证 CLI 与 Query API 契约。
- `tests/e2e/test_monitor_query_workflow.py`：真实 CLI + real writer 链路，用于验证 Kafka / 存储 / Query API 全链路。

分界原则：

- 只要一个场景主要是在验证 CLI 自身行为，或者只涉及单个服务的真实接口契约，它应进入 `tests/integration/`。
- 只要一个场景需要同时启动或串联多个 sibling 服务，验证跨服务旅程或状态传播，它应进入 `tests/e2e/`。

运行约定：

```bash
just test unit
```

- `unit` 可以直接运行，无需额外准备。

```bash
just test bootstrap
just test integration
just test e2e
just test
```

- `integration`、`e2e`、`all` 会按需隐含执行 `just test bootstrap`；如需提前预热环境，仍可手工执行一次。
- monitor live integration / e2e 所需的 infra 与 schema，会在 pytest 需要受管启动 monitor-server 时按 `tests/_local_services.py` 的统一规则按需准备；若你选择手工托管 monitor-server，请按 README 中与其它业务服务一致的 `just dev start` 方式启动，并覆盖对应测试环境变量。
- 若当前 shell 未设置 `DOCKER_CONFIG`，测试 bootstrap 与本地自动托管会改用 `acps-cli/.tmp/docker-public-config/` 拉取 public dev-infra 镜像，避免 Docker credential helper 卡住。
- 使用默认本地地址时，如目标服务暂未启动，测试夹具会按既定规则自动托管所需 sibling 服务。
- 更完整的环境准备、默认端口和自动托管说明，请参考 [../README.md](../README.md) 的“测试”章节。
