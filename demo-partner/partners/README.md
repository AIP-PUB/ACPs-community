# 通用 Partner Agent 框架

本目录实现了一个通用的 Partner Agent 框架，采用 **"通用运行时 + 配置驱动"** 的架构。新增一个 Agent 只需编写 Prompt 和 JSON 配置文件，无需编写 Python 代码。所有 AIP 协议交互、状态机流转和异常处理都由通用框架统一维护。

## 1. 目录结构

```
partners/
├── main.py              # 主入口：扫描 online/ 目录，启动 FastAPI 服务
├── generic_runner.py    # 通用运行时：封装单个 Agent 的三阶段处理逻辑
├── group_handler.py     # Group 模式消息队列处理
├── online/              # [活跃区] 正在运行的 Agent（主程序只加载此目录）
│   ├── beijing_urban/   # 北京城区景点规划师
│   ├── beijing_rural/   # 北京郊区景点规划师
│   ├── beijing_food/    # 北京美食推荐师
│   ├── china_hotel/     # 全国酒店预订师
│   └── china_transport/ # 全国交通预定师
└── offline/             # [编辑区] 开发、维护或暂停的 Agent
```

### 每个 Agent 的配置文件

每个 Agent 目录（如 `online/beijing_urban/`）包含以下配置文件：

| 文件           | 说明                                                             |
| -------------- | ---------------------------------------------------------------- |
| `acs.json`     | ACS 定义：Agent 元数据（名称、AIC）、能力边界（Skills 列表）     |
| `config.toml`  | 运行参数：LLM 连接/Profile 配置、并发限制等                      |
| `prompts.toml` | 业务逻辑：三阶段 Prompt 模板（Decision / Analysis / Production） |

## 2. 核心处理流程

每个 Partner Agent 处理 Leader 请求的生命周期分为三个标准阶段：

1. **意图识别与准入判断（Decision）**：判断请求是否在服务范围内，决定接受或拒绝任务。
2. **需求分析与补全（Analysis）**：将自然语言转化为结构化需求数据，识别缺失信息并生成追问。
3. **内容生成与交付（Production）**：基于完整需求调用大模型生成最终交付物。

这三个阶段的具体行为完全由 `prompts.toml` 中的 Prompt 驱动，不同的 Agent 只需编写不同的 Prompt 即可实现不同的业务逻辑。

## 3. 部署模式

采用 **独立端口** 模式：每个 Agent 独立进程、独立端口运行，支持 mTLS。端口和证书在各 Agent 的 `config.toml` 中配置。

- RPC 接口：`/rpc`
- Group RPC 接口：`/group/rpc`
- 健康检查：`/health`

## 4. 运行

从 `demo-partner` 项目根目录运行（推荐）：

```bash
# 启动 Partner 服务
just dev start
```

或通过 Python 直接启动：

```bash
python -m partners.main
```

## 5. 新增 Agent 基本流程

1. 在 `offline/` 下创建新目录（如 `offline/my_agent/`）。
2. 编写三个配置文件：
   - `acs.json`：定义 Agent 身份和能力。
   - `config.toml`：配置 LLM Profile、服务端口和 mTLS 参数。实际 LLM 值通过仓库根目录 `.env` 注入，默认在线 Partner 共用 `PARTNER_LLM_FAST_*` 与 `PARTNER_LLM_DEFAULT_*` 这两组变量。
   - `prompts.toml`：编写 Decision / Analysis / Production 三个阶段的 Prompt。
3. 将目录移动到 `online/` 即可上线：
   ```bash
   mv offline/my_agent online/my_agent
   ```
4. 重启 Partner 服务使其生效（当前版本暂不支持热加载）。
