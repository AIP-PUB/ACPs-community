# ADP (Agent Discovery Protocol) SDK 模块

本模块实现了 ACPs 协议体系中 ADP（智能体发现协议）的共用功能代码，基于 ACPs-spec-ADP 协议规范。

## 模块结构

```
acps_sdk/adp/
├── __init__.py      # 包入口，统一导出所有公开 API
├── constants.py     # 协议常量（默认值、限制参数、API 路径等）
├── errors.py        # 错误码枚举、异常类和错误响应构建工具
├── models.py        # Pydantic V2 数据模型（请求、响应、过滤、上下文等）
├── validators.py    # 验证工具（转发链、扇出额度、过滤条件等）
└── README.md        # 本文件
```

## 功能概览

### 数据模型 (`models.py`)

使用 Pydantic V2 实现：

| 模型类                | 说明                                                       |
| --------------------- | ---------------------------------------------------------- |
| `DiscoveryRequest`    | 发现请求，包含查询类型、过滤条件、转发控制参数             |
| `DiscoveryResponse`   | 发现响应，遵循 result/error 互斥的 CommonResponse 模式     |
| `DiscoveryResult`     | 发现结果，包含 acsMap、agents 候选列表和可选的 routes 路由 |
| `DiscoveryRoute`      | 路由级结果，对应一次转发路径及其候选                       |
| `DiscoveryAgentGroup` | 按分组组织的智能体匹配结果                                 |
| `DiscoveryAgentSkill` | 单个智能体技能的匹配结果                                   |
| `DiscoveryFilter`     | 结构化过滤条件集合（支持嵌套）                             |
| `FilterCondition`     | 单个过滤条件                                               |
| `FilterOperator`      | 过滤运算符枚举                                             |
| `DiscoveryContext`    | 上下文载荷                                                 |
| `ErrorDetail`         | 错误信息对象                                               |

请求体和响应体字段通过 Pydantic alias 使用 lowerCamelCase；Python 代码中可继续使用 snake_case。`DiscoveryRequest` 和 `DiscoveryResponse` 提供 `to_json()` / `to_dict()`，默认按 camelCase 输出并排除 `None` 字段；如直接调用 Pydantic 的 `model_dump()`，需自行传入 `by_alias=True`。

| 便利方法/属性                                    | 说明                                      |
| ------------------------------------------------ | ----------------------------------------- |
| `DiscoveryRequest.from_dict()` / `from_json()`   | 从字典或 JSON 构建请求对象                |
| `DiscoveryRequest.get_effective_*()`             | 获取深度、扇出和超时的有效值（含默认值）  |
| `DiscoveryResponse.success()` / `failure()`      | 构建成功或失败响应                        |
| `DiscoveryResponse.is_success()` / `is_error()`  | 判断响应类型                              |
| `DiscoveryResponse.get_adp_error()`              | 将 `error` 字段转换为 `ADPError`          |
| `DiscoveryResult.iter_agent_skills()`            | 遍历结果技能并按 AIC 关联 `acsMap` 数据   |

### 错误处理 (`errors.py`)

- `ADPErrorCode` 枚举：定义所有 ADP 错误码（30701-50001）
- `ADPError` 异常类：可转换为响应体结构
- `make_error_response()` 工厂函数：快速构建错误响应字典
- `get_http_status_for_error()`：获取错误码对应的 HTTP 状态码
- `ADP_ERROR_NAMES` 映射：错误码到协议错误名称的映射
- `ADP_ERROR_HTTP_STATUS` 映射：错误码到 HTTP 状态码的映射

`ADPErrorCode` 和 `ADPError` 均提供 `is_redirect()`、`is_retryable()`、`is_client_error()`、`is_forward_error()` 分类判断方法。

### 验证工具 (`validators.py`)

| 函数                           | 说明                                                                 |
| ------------------------------ | -------------------------------------------------------------------- |
| `validate_discovery_request()` | 校验请求参数（类型/深度/扇出/过滤嵌套）                              |
| `validate_forward_chain()`     | 转发链验证（环路检测、深度检查、上一跳完整性校验）                   |
| `validate_fanout_budget()`     | 校验 `forwardFanoutRemaining` 是否足够；未设置时回退到 fan-out limit |
| `validate_trusted_target()`    | 检查目标 AIC 是否在 `forwardTrustedServers` 中；未设置则视为无限制   |
| `should_continue_forwarding()` | 判断总剩余超时是否足够覆盖单跳超时                                   |
| `build_forwarded_request()`    | 构建转发请求（追加链、更新剩余额度、调整总超时、可追加签名/信任列表） |
| `allocate_fanout_budget()`     | Fan-out 额度分配（支持均分和加权策略）                               |

### 常量 (`constants.py`)

| 常量                               | 默认值 | 说明                 |
| ---------------------------------- | ------ | -------------------- |
| `FORWARD_DEPTH_LIMIT_DEFAULT`      | 3      | 转发深度默认值       |
| `FORWARD_DEPTH_LIMIT_MAX`          | 5      | 转发深度绝对上限     |
| `FORWARD_DEPTH_LIMIT_MIN`          | 1      | 转发深度最小值       |
| `FORWARD_FANOUT_LIMIT_DEFAULT`     | 1      | 转发扇出默认值       |
| `FORWARD_FANOUT_LIMIT_MAX`         | 5      | 转发扇出绝对上限     |
| `FORWARD_FANOUT_LIMIT_MIN`         | 1      | 转发扇出最小值       |
| `FORWARD_EACH_TIMEOUT_MS_DEFAULT`  | 10000  | 单跳超时默认值 (ms)  |
| `FORWARD_TOTAL_TIMEOUT_MS_DEFAULT` | 60000  | 总超时默认值 (ms)    |
| `MAX_REDIRECT_HOPS`                | 5      | 最大连续重定向次数   |
| `FILTER_MAX_NESTING_DEPTH`         | 3      | 过滤嵌套深度建议上限 |
| `CONTEXT_MAX_PAYLOAD_BYTES`        | 2048   | 上下文建议最大字节数 |
| `DISCOVER_API_PATH`                | `/discover` | 发现 API 路径    |

此外还导出请求头常量 `HEADER_TRACE_ID`、`HEADER_SPAN_ID`、`HEADER_PARENT_SPAN_ID`，查询类型常量 `QUERY_TYPE_EXPLICIT` / `QUERY_TYPE_EXPLORATORY` / `QUERY_TYPE_TRENDING` / `QUERY_TYPE_FILTERED` / `QUERY_TYPES`，路由状态常量 `ROUTE_STATUS_OK` / `ROUTE_STATUS_TIMEOUT` / `ROUTE_STATUS_ERROR`，以及过滤逻辑常量 `FILTER_LOGIC_AND` / `FILTER_LOGIC_OR` / `FILTER_LOGIC_NOT`。

## 使用示例

### 构建发现请求

```python
from acps_sdk.adp import (
    DiscoveryRequest,
    DiscoveryFilter,
    FilterCondition,
    FilterOperator,
    DiscoveryContext,
)

# 明确查询 + 过滤条件
request = DiscoveryRequest(
    type="explicit",
    query="我需要一个可以做北京美食推荐的智能体",
    limit=5,
    filter=DiscoveryFilter(
        conditions=[
            FilterCondition(field="active", op=FilterOperator.EQ, value=True),
            FilterCondition(
                field="endPoints.transport",
                op=FilterOperator.IN,
                value=["JSONRPC"],
            ),
            FilterCondition(
                field="capabilities.streaming",
                op=FilterOperator.EQ,
                value=True,
            ),
            FilterCondition(
                field="skills.tags",
                op=FilterOperator.ANY_OF,
                value=["美食", "北京"],
            ),
        ]
    ),
    context=DiscoveryContext(
        conversation_id="conv-123",
        recent_turns=["用户偏好健康饮食"],
        user_profile={"city": "北京", "budget": "medium"},
    ),
)

# 序列化为 JSON（使用 camelCase）
json_str = request.to_json()

# 也可以序列化为字典或从 camelCase 字典反序列化
payload = request.to_dict()
same_request = DiscoveryRequest.from_dict(payload)
```

### 构建发现响应

```python
from acps_sdk.adp import (
    DiscoveryResponse,
    DiscoveryResult,
    DiscoveryRoute,
    DiscoveryAgentGroup,
    DiscoveryAgentSkill,
)

response = DiscoveryResponse.success(
    result=DiscoveryResult(
        acs_map={
            "AIC-AGENT-FOOD-01": {
                "name": "BeijingFoodGuide",
                "description": "北京美食推荐智能体",
            }
        },
        agents=[
            DiscoveryAgentGroup(
                group="美食推荐",
                agent_skills=[
                    DiscoveryAgentSkill(
                        aic="AIC-AGENT-FOOD-01",
                        skill_id="skill-beijing-food",
                        ranking=1,
                    ),
                ],
            )
        ],
        routes=[
            DiscoveryRoute(
                forward_chain=["AIC-DS-A"],
                agent_groups=[
                    DiscoveryAgentGroup(
                        group="美食推荐",
                        agent_skills=[
                            DiscoveryAgentSkill(
                                aic="AIC-AGENT-FOOD-01",
                                skill_id="skill-beijing-food",
                                ranking=1,
                            ),
                        ],
                    )
                ],
                status="ok",
                duration_ms=150,
            )
        ],
    )
)

for aic, acs_data, skill, group in response.result.iter_agent_skills():
    print(f"[{group}] {aic} -> skill={skill.skill_id}, acs={acs_data.get('name')}")
```

### 转发链验证与构建

```python
from acps_sdk.adp import (
    DiscoveryRequest,
    validate_forward_chain,
    validate_fanout_budget,
    validate_trusted_target,
    build_forwarded_request,
    allocate_fanout_budget,
    ADPError,
)

request = DiscoveryRequest.from_dict({
    "type": "explicit",
    "query": "翻译智能体",
    "forwardDepthLimit": 3,
    "forwardFanoutLimit": 4,
    "forwardChain": ["AIC-DS-A"],
})

# 验证转发链
try:
    validate_forward_chain(
        request,
        current_server_aic="AIC-DS-B",
        sender_aic="AIC-DS-A",
    )
except ADPError as e:
    print(f"转发链验证失败: {e.to_error_body()}")

# 验证扇出额度
# request 未设置 forwardFanoutRemaining 时，会使用 forwardFanoutLimit 作为有效剩余额度
validate_fanout_budget(request, required_branches=3)

# 分配额度
budgets = allocate_fanout_budget(total_remaining=4, branch_count=3)
# -> [0, 0, 1]

# 构建转发请求
forwarded = build_forwarded_request(
    original=request,
    current_server_aic="AIC-DS-B",
    fanout_remaining_for_branch=budgets[0],
    elapsed_ms=200,
    trusted_servers=["AIC-DS-B", "AIC-DS-C", "AIC-DS-D"],
)

print(forwarded.forward_chain)  # ["AIC-DS-A", "AIC-DS-B"]
print(forwarded.forward_fanout_remaining)  # 0
```

### 错误处理

```python
from acps_sdk.adp import (
    DiscoveryResponse,
    ADPError,
    ADPErrorCode,
    make_error_response,
    get_http_status_for_error,
)

# 使用工厂函数
error_body = make_error_response(
    ADPErrorCode.FORWARD_FANOUT_EXCEEDED,
    message="Fan-out budget exhausted",
    data={"availableBudget": 1, "requiredBranches": 3},
)
http_status = get_http_status_for_error(ADPErrorCode.FORWARD_FANOUT_EXCEEDED)  # 508

# 使用异常类
try:
    raise ADPError(
        code=ADPErrorCode.FORWARD_LOOP_DETECTED,
        message="检测到转发环路",
        data={"forwardChain": ["AIC-A", "AIC-B", "AIC-A"]},
    )
except ADPError as e:
    http_status = e.http_status  # 508
    response_body = e.to_response_dict()
    print(e.is_forward_error())  # True

error_response = DiscoveryResponse.failure(40001, "MissingQuery")
adp_error = error_response.get_adp_error()
if adp_error and adp_error.is_client_error():
    print(f"客户端参数错误: {adp_error.message}")
```

## 参考

- [ACPs-spec-ADP-v02.02](../../../acps-specs/06-ACPs-spec-ADP/ACPs-spec-ADP.md) - 智能体发现过程
