# ACS SDK — 智能体能力描述模型

ACS（Agent Capability Specification）模块提供基于 **ACPs-spec-ACS-v02.02** 规范的 Python 数据模型，使用 Pydantic V2 实现类型验证与序列化。

## 核心模型

| 模型                  | 说明                                                       |
| --------------------- | ---------------------------------------------------------- |
| `AgentCapabilitySpec` | ACS 根对象，完整描述智能体的身份、能力、端点、安全方案和技能 |
| `AgentProvider`       | 智能体服务提供者信息（组织、联系方式、资质）               |
| `AgentCapabilities`   | 技术能力配置（流式响应、异步通知、消息队列）               |
| `MQProtocolVersion`   | 消息队列协议版本枚举（如 MQTT、AMQP、Kafka、Redis、RabbitMQ） |
| `AgentEndPoint`       | 服务端点配置（URL、传输协议、安全要求）                    |
| `AgentSkill`          | 智能体技能定义（功能边界、输入输出规范）                   |

## 快速使用

```python
from acps_sdk.acs import AgentCapabilitySpec

# 从字典创建
spec = AgentCapabilitySpec.from_dict(data)

# 从 JSON 字符串创建
spec = AgentCapabilitySpec.from_json(json_str)

# 从 JSON 文件加载
spec = AgentCapabilitySpec.from_file("agent.json")

# 序列化
json_str = spec.to_json()
data_dict = spec.to_dict()
```

## 字段别名

模型使用 `populate_by_name=True` 配置，同时支持 Python 风格（`snake_case`）和协议风格（`camelCase`）字段名。

`AgentCapabilitySpec.to_json()` 和 `AgentCapabilitySpec.to_dict()` 默认使用 `camelCase` 别名，并排除值为 `None` 的字段。若直接调用 Pydantic 的 `model_dump()` / `model_dump_json()`，需要显式传入 `by_alias=True` 才会输出协议风格字段名。

## 参考

- [ACPs-spec-ACS-v02.02](../../../acps-specs/03-ACPs-spec-ACS/ACPs-spec-ACS.md) - 智能体能力描述
