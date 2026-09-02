# Web App 前端

本目录是 Demo 应用的前端 Web 界面，使用原生 JavaScript 实现，提供与 Leader Agent 的交互界面。

## 1. 目录结构

```
web_app/
├── index.html # 主页面
├── app.js # 前端逻辑（API 调用、轮询、消息渲染）
├── config.js # 运行时配置（后端地址、轮询间隔等）
├── styles.css # 样式表
└── webserver.py # 静态文件服务器（基于 Python 标准库）
```

## 2. 技术栈

采用"零依赖"策略，无构建工具和第三方运行时库：

- **框架**：原生 JavaScript (ES2020)
- **样式**：手写 CSS
- **HTTP**：原生 `fetch`
- **构建**：无（直接静态服务）

## 3. 交互流程

1. 用户在输入框中输入自然语言请求。
2. 前端调用 `POST /api/v1/submit` 提交请求到 Leader。
3. 前端通过 `GET /api/v1/result/{session_id}` 轮询任务状态。
4. 根据响应状态（`pending` / `running` / `awaiting_input` / `completed` / `failed`）更新界面。
5. 支持 Direct RPC 和 Group 两种执行模式切换。

## 4. 配置

本地开发默认配置放在 `runtime-config.js`，`config.js` 负责提供兜底值并与运行时覆盖合并。部署态可由
安装层 Ansible 通过 Jinja 模板重写 `runtime-config.js`。

| 配置项 | 默认值 | 说明 |
| ---------------- | ----------------------- | ---------------- |
| `backendBase` | `http://localhost:9031` | Leader 后端地址 |
| `apiVersion` | `v1` | API 版本 |
| `pollInterval` | `5000` | 轮询间隔（毫秒） |
| `maxPollRetries` | `60` | 最大轮询次数 |

## 5. 运行

从仓库根目录运行（推荐）：

```bash
# 一体化本地开发（Leader API + Web UI）
just dev start
```

或单独启动静态文件服务器：

```bash
uv run python web_app/webserver.py # 默认 127.0.0.1:9030
uv run python web_app/webserver.py --port 4000 # 指定端口
```

启动后在浏览器访问 `http://localhost:9030`。

> **注意**：前端需要 Leader 服务（默认 `http://localhost:9031`）已启动才能正常工作。本地开发默认启用
> OIDC，并通过 `runtime-config.js` 指向 `acps-infra/dev-infra` 提供的 Keycloak：
> `http://localhost:9080/realms/acps-leader`。
