# ACPs SDK for Node.js

[English](README_en.md)

Agent Collaboration Protocols（智能体协作协议体系）SDK 的 Node.js / TypeScript 实现。

本包是 [`acps-sdk`](../../acps-sdk/README.md)（Python，v2.2.0）的移植：**线上格式与行为与 Python 版一致**，
API 在符合 Node 习惯的前提下与 Python 版一一对应（`snake_case` → `camelCase`，关键字参数 → 选项对象）。
两边的对应规则见 [docs/porting-notes.md](docs/porting-notes.md)。

贡献单位：北京智镜科技有限责任公司

贡献者：许红博

| 模块            | 说明                                                                                          | 对应 Python     |
| --------------- | --------------------------------------------------------------------------------------------- | --------------- |
| `acps-sdk/aic`  | Agent Identity Code 智能体身份码：格式校验、解析、CRC 校验                                    | `acps_sdk.aic`  |
| `acps-sdk/acs`  | Agent Capability Specification 智能体能力描述模型                                             | `acps_sdk.acs`  |
| `acps-sdk/adp`  | Agent Discovery Protocol 智能体发现协议模型与校验                                             | `acps_sdk.adp`  |
| `acps-sdk/aip`  | Agent Interaction Protocol：Direct RPC、流式（SSE）、异步通知、群组模式（RabbitMQ）、身份绑定 | `acps_sdk.aip`  |
| `acps-sdk/amp`  | Agent Monitoring Protocol：心跳、指标、访问、消息、系统事件、审计日志发射器与 alive 同步      | `acps_sdk.amp`  |
| `acps-sdk/oidc` | 真人用户 OIDC 认证与授权（设备授权、JWT 校验、Keycloak 声明映射）                             | `acps_sdk.oidc` |

## 环境要求

- Node.js ^20.19 或 >= 22.12（`acps-sdk/oidc` 依赖的 `jose` 只有 ESM 版本，CommonJS 方式加载需要支持 `require()` ES 模块的 Node 版本）
- 运行依赖：`zod`、`amqplib`（群组模式）、`jose`（OIDC）
- TypeScript 项目需安装 `@types/node`

## 安装

目前尚未发布到 npm，可从源码构建：

```bash
cd contrib/acps-sdk-node
npm ci
npm run build
# 在目标项目中
npm install /path/to/ACPs-community/contrib/acps-sdk-node
```

## 快速开始

### 最小 Partner（Direct RPC + mTLS）

```ts
import { readFileSync } from "node:fs";
import https from "node:https";
import {
  type CommandHandlers,
  DefaultHandlers,
  TaskManager,
  TaskState,
  createAipRpcRequestListener,
} from "acps-sdk/aip";

const PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4";

const handlers: CommandHandlers = {
  async onStart(command, task) {
    if (task) return task;
    const text = command.dataItems?.find((item) => item.type === "text");
    const created = TaskManager.createTask(command, TaskState.AwaitingCompletion);
    created.senderId = PARTNER_AIC;
    TaskManager.setProducts(created.taskId, [
      {
        id: `product-${created.taskId}`,
        name: "echo",
        dataItems: [
          { type: "text", text: `Partner 已处理：${text?.type === "text" ? text.text : ""}` },
        ],
      },
    ]);
    return TaskManager.getTask(created.taskId) ?? created;
  },
  onGet: DefaultHandlers.get,
  onCancel: DefaultHandlers.cancel,
  onComplete: DefaultHandlers.complete,
};

const rpc = createAipRpcRequestListener(handlers, { localAic: PARTNER_AIC });

https
  .createServer(
    {
      key: readFileSync("agent-key.pem"),
      cert: readFileSync("agent-cert.pem"), // serverAuth 证书
      ca: readFileSync("trust-bundle.pem"),
      requestCert: true, // mTLS：要求并校验 Leader 的客户端证书
      rejectUnauthorized: true,
    },
    (req, res) => {
      if (req.url === "/rpc" && req.method === "POST") return void rpc(req, res);
      res.statusCode = 404;
      res.end();
    },
  )
  .listen(8011);
```

身份绑定默认开启：请求的 `senderId` 必须等于客户端证书中的 AIC，返回结果的 `senderId` 必须等于本端 AIC。
**mTLS 必须在 Node 进程内终止**（不要在 nginx 等反向代理处解掉 TLS 后再转发），否则读不到对端证书。

### 最小 Leader

```ts
import { readFileSync } from "node:fs";
import { AipRpcClient, TaskState } from "acps-sdk/aip";

const client = new AipRpcClient("https://partner.example.com:8011/rpc", LEADER_AIC, {
  tls: {
    cert: readFileSync("leader-cert.pem"), // clientAuth 证书
    key: readFileSync("leader-key.pem"),
    ca: readFileSync("trust-bundle.pem"),
  },
  expectedPartnerAic: PARTNER_AIC,
});

let task = await client.startTask(sessionId, "请处理这段文本");
while (task.status.state === TaskState.Accepted || task.status.state === TaskState.Working) {
  await new Promise((r) => setTimeout(r, 1000));
  task = await client.getTask(task.taskId, sessionId);
}
if (task.status.state === TaskState.AwaitingCompletion) {
  task = await client.completeTask(task.taskId, sessionId);
}
```

流式（`AipStreamClient` / `StreamHub` + `createAipStreamRequestListener`）、异步通知
（`AipNotificationClient` / `NotificationService` + `createAipNotificationRequestListener` / `NotificationReceiver`）
与群组模式（`GroupLeader` / `GroupPartnerMqClient`）的用法与 Python 版相同，参见各模块源码注释与
[AIP SDK 教程](../acps-docs/tutorials/aip-sdk-tutorial.md)。

### 与 Web 框架集成

服务端不依赖任何 Web 框架。每个端点都提供：

- 与框架无关的核心函数（`handleRpcRequest`、`handleStreamRequest`、`handleNotificationRequest`），输入解析后的请求体和对端身份，输出响应对象；
- `createXxxRequestListener(...)`，返回 `(req, res) => Promise<void>`，可直接用于 `https.createServer`，也可挂在 Express 路由上；通知端点的 `createAipNotificationRequestListener` 自己按路径分发 `/notification/*`，返回 `Promise<boolean>`（未匹配时为 `false`，交由调用方继续路由）。

## 开发

开发工具链（vitest、tsup）需要 Node.js >= 22.18。

```bash
npm ci
npm test              # 单元测试 + 集成测试（集成测试会现场生成测试证书并启动本地 mTLS 服务）
npm run qa            # 格式、lint、类型检查、测试、构建
npm run fixtures:python   # 用 ../../acps-sdk 的 Python 版重新生成跨语言对照样例（需要 uv）
```

测试中的跨语言对照样例由 Python 版 SDK 生成（`scripts/fixtures/gen_*.py` → `test/fixtures/python/`），
Node 测试读取后逐字段比对，用来保证两边线上格式一致。

## 许可

[木兰宽松许可证，第2版](LICENSE)
