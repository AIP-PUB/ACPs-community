# ACPs SDK

Agent Collaboration Protocols（智能体协作协议体系）SDK — ACPs 协议体系的 Python 实现。

目前本SDK包含以下模块：

| 模块           | 说明                                          |
| -------------- | --------------------------------------------- |
| `acps_sdk.acs` | Agent Capability Specification 智能体能力描述 |
| `acps_sdk.adp` | Agent Discovery Protocol 智能体发现协议       |
| `acps_sdk.aic` | Agent Identity Code 智能体身份码              |
| `acps_sdk.aip` | Agent Interaction Protocol 智能体交互协议     |

## 1. SDK本地开发环境

### 1.1. 开发环境搭建

建议使用 uv 安装并管理 Python 3.14，并通过 uv 管理依赖与构建，避免依赖系统 Python。

```bash
# 安装 uv（若尚未安装，推荐使用官方安装脚本）
# macOS / Linux（用户级全局安装）
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell，用户级全局安装)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 克隆仓库
git clone <仓库地址>
cd acps-sdk

# 使用 uv 安装 Python 3.14
uv python install 3.14

# 基于 uv 管理的 Python 3.14 创建并激活虚拟环境
uv venv --python 3.14 .venv
source .venv/bin/activate
# Windows PowerShell:
# .\.venv\Scripts\Activate.ps1

# 安装 SDK 运行依赖与开发依赖（含测试）
uv sync
```

`uv sync` 会同步 `[dependency-groups].dev`（pytest、OIDC 测试所需的 PyJWT 等）。若只安装运行时依赖，可使用 `uv sync --no-dev`；若目标应用需要 OIDC 能力，再额外执行 `uv sync --extra oidc`（或 `uv add 'acps-sdk[oidc]'`）。

运行测试：

```bash
uv run pytest
# 或
uv run pytest tests/
```

### 1.2. 构建和发布

```bash
uv build
```

生成的 wheel 和 sdist 将位于 `dist/` 目录。

发布到 PyPI：

```bash
uv publish --token <PyPI_TOKEN>
```

## 2. 目标项目中SDK的安装

### 2.1 使用pip

在需要使用本SDK的Python环境中，可以通过 pip 安装。

从 PyPI（发布后）:

```bash
pip install acps-sdk
```

从本地 wheel 文件:

```bash
pip install path/to/acps_sdk-2.0.0-py3-none-any.whl
```

### 2.2 使用 uv

在需要使用本SDK的项目，推荐使用 uv 管理依赖。

从 PyPI（发布后）：

```bash
uv add acps-sdk
```

从本地 wheel 文件：

```bash
uv add path/to/acps_sdk-2.0.0-py3-none-any.whl
```

从本地源码：

```bash
uv add --editable ../acps-sdk
```

## 3. SDK使用示例代码

```python
from acps_sdk.acs import AgentCapabilitySpec
from acps_sdk.aip import AipRpcClient, TaskState
from acps_sdk.aic import validate_aic_format, parse_aic
```
