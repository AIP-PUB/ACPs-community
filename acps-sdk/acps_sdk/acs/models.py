"""
ACS (Agent Capability Specification) 数据模型定义

基于 ACPs-spec-ACS-v02.02 规范定义的智能体能力描述数据模型。
使用 Pydantic V2 实现类型验证和序列化。
"""

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field

# =============================================================================
# MQProtocolVersion 消息队列协议版本枚举
# =============================================================================


class MQProtocolVersion(str, Enum):
    """
    消息队列协议版本枚举类型。
    定义智能体支持的各种消息队列中间件及其具体版本，
    用于异步消息传递、事件通知和分布式系统集成。
    使用"protocol:version"格式提供精确的技术栈定义。
    """

    # MQTT 版本
    MQTT_3_1_1 = "mqtt:3.1.1"
    MQTT_5_0 = "mqtt:5.0"
    # AMQP 版本
    AMQP_0_9_1 = "amqp:0.9.1"
    AMQP_1_0 = "amqp:1.0"
    # Kafka 版本
    KAFKA_2_8 = "kafka:2.8"
    KAFKA_3_0 = "kafka:3.0"
    KAFKA_3_1 = "kafka:3.1"
    # Redis 版本
    REDIS_6_0 = "redis:6.0"
    REDIS_7_0 = "redis:7.0"
    REDIS_7_2 = "redis:7.2"
    # RabbitMQ 版本
    RABBITMQ_3_9 = "rabbitmq:3.9"
    RABBITMQ_3_10 = "rabbitmq:3.10"
    RABBITMQ_3_11 = "rabbitmq:3.11"


# =============================================================================
# AgentProvider 智能体服务提供者信息
# =============================================================================


class AgentProvider(BaseModel):
    """
    智能体服务提供者信息对象。
    定义智能体的开发和维护组织信息，包括组织身份、联系方式和合规资质。
    用于建立信任关系和提供技术支持联系渠道。
    """

    country_code: Optional[str] = Field(
        default="CN",
        alias="countryCode",
        description="智能体提供者的国家或地区代码。符合ISO 3166-1 alpha-2标准。",
        examples=["CN", "US", "GB"],
    )

    organization: Optional[str] = Field(
        default=None,
        description="智能体提供者的组织名称，通常为公司、大学或研究机构等顶级组织。",
        examples=["ACPs工作组"],
    )

    department: Optional[str] = Field(
        default=None,
        description="智能体提供者的具体部门或院系名称，提供更精确的组织结构信息。",
        examples=[""],
    )

    url: Optional[str] = Field(
        default=None,
        description="智能体提供者的官方网站或相关文档的URL地址。",
        examples=["https://ioa.pub"],
    )

    license: Optional[str] = Field(
        default=None,
        description="智能体提供者的法律备案信息或许可证号，用于合规性验证。",
        examples=["京ICP备2025124884号-4"],
    )

    name: Optional[str] = Field(
        default=None,
        description="智能体提供者的联系人姓名，便于技术支持和沟通。",
        examples=["张三", "李四"],
    )

    email: Optional[str] = Field(
        default=None,
        description="智能体提供者的联系人电子邮箱地址。",
        examples=["zhangsan@example.com", "lisi@example.com"],
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# AgentCapabilities 智能体技术能力配置
# =============================================================================


class AgentCapabilities(BaseModel):
    """
    智能体可选技术能力配置对象。
    定义智能体支持的高级功能特性，如实时通信、异步通知和消息队列集成。
    这些能力为智能体提供更丰富的交互方式和更强的扩展性。
    """

    streaming: bool = Field(
        ...,
        description="智能体是否支持Server Send Event（SSE）用于流式响应。启用后可以实现实时数据推送和渐进式内容生成。",
        examples=[True, False],
    )

    notification: bool = Field(
        ...,
        description="智能体是否支持异步推送通知。启用后可以主动向指定URL推送事件和状态更新。",
        examples=[True, False],
    )

    message_queue: List[MQProtocolVersion] = Field(
        default_factory=list,
        alias="messageQueue",
        description="智能体支持的消息队列能力配置。使用协议版本字符串数组进行配置。空数组表示不支持任何消息队列协议。",
        examples=[
            ["mqtt:5.0", "amqp:0.9.1", "kafka:3.0"],
            ["redis:7.2", "rabbitmq:3.11"],
            [],
        ],
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# SecurityScheme 安全方案定义
# =============================================================================


class MutualTLSSecurityScheme(BaseModel):
    """
    双向TLS认证安全方案，用于智能体间的高安全级别通信。
    要求客户端和服务端都提供有效的证书进行相互验证。
    """

    type: Literal["mutualTLS"] = Field(
        ..., description="安全方案类型，固定为'mutualTLS'。", examples=["mutualTLS"]
    )

    description: Optional[str] = Field(
        default=None,
        description="安全方案的描述信息，说明该方案的用途和特点。",
        examples=[
            "双向TLS认证，确保客户端和服务端身份可信",
            "智能体间高安全级别通信认证",
        ],
    )

    x_ca_challenge_base_url: str | None = Field(
        default=None,
        alias="x-caChallengeBaseUrl",
        description="兼容层（只读，不再增强）：旧版 challenge 流程的基础 URL 扩展字段。EAB 主链路下仅保留兼容读取语义。",
        examples=[
            "https://certs.example.com/agent-challenge",
            "https://ca.example.com/challenge/v1",
        ],
    )

    model_config = ConfigDict(populate_by_name=True)


class OpenIdConnectSecurityScheme(BaseModel):
    """
    OpenID Connect认证安全方案，基于OAuth 2.0协议的身份认证层。
    提供标准化的身份验证和用户信息获取能力。
    """

    type: Literal["openIdConnect"] = Field(
        ...,
        description="安全方案类型，固定为'openIdConnect'。",
        examples=["openIdConnect"],
    )

    description: Optional[str] = Field(
        default=None,
        description="安全方案的描述信息，说明该OIDC方案的用途和特点。",
        examples=["基于OpenID Connect的统一身份认证", "支持多身份提供商的用户认证"],
    )

    open_id_connect_url: str = Field(
        ...,
        alias="openIdConnectUrl",
        description="OpenID Connect发现文档的URL，用于自动发现认证端点和配置信息。",
        examples=[
            "https://auth.example.com/.well-known/openid-configuration",
            "https://accounts.google.com/.well-known/openid-configuration",
        ],
    )

    model_config = ConfigDict(populate_by_name=True)


class APIKeySecurityScheme(BaseModel):
    """
    API Key 安全方案，通过API密钥进行身份验证。
    """

    type: Literal["apiKey"] = Field(..., description="安全方案类型，固定为'apiKey'。")

    description: Optional[str] = Field(default=None, description="安全方案的描述信息。")

    name: str = Field(..., description="API密钥参数的名称。")

    in_location: Literal["query", "header", "cookie"] = Field(
        ..., alias="in", description="API密钥的位置：query, header 或 cookie。"
    )

    model_config = ConfigDict(populate_by_name=True)


class HTTPAuthSecurityScheme(BaseModel):
    """
    HTTP 认证安全方案，支持 Basic、Bearer 等认证方式。
    """

    type: Literal["http"] = Field(..., description="安全方案类型，固定为'http'。")

    description: Optional[str] = Field(default=None, description="安全方案的描述信息。")

    scheme: str = Field(..., description="HTTP认证方案名称，如 'basic', 'bearer' 等。")

    bearer_format: Optional[str] = Field(
        default=None,
        alias="bearerFormat",
        description="Bearer令牌的格式提示，仅当scheme为'bearer'时使用。",
    )

    model_config = ConfigDict(populate_by_name=True)


class OAuth2Flow(BaseModel):
    """OAuth2 流程配置"""

    authorization_url: Optional[str] = Field(
        default=None, alias="authorizationUrl", description="授权端点URL。"
    )

    token_url: Optional[str] = Field(
        default=None, alias="tokenUrl", description="令牌端点URL。"
    )

    refresh_url: Optional[str] = Field(
        default=None, alias="refreshUrl", description="刷新令牌端点URL。"
    )

    scopes: Dict[str, str] = Field(
        default_factory=dict, description="可用的OAuth2作用域及其描述。"
    )

    model_config = ConfigDict(populate_by_name=True)


class OAuth2Flows(BaseModel):
    """OAuth2 流程集合"""

    implicit: Optional[OAuth2Flow] = Field(
        default=None, description="隐式授权流程配置。"
    )

    password: Optional[OAuth2Flow] = Field(
        default=None, description="密码授权流程配置。"
    )

    client_credentials: Optional[OAuth2Flow] = Field(
        default=None, alias="clientCredentials", description="客户端凭证授权流程配置。"
    )

    authorization_code: Optional[OAuth2Flow] = Field(
        default=None, alias="authorizationCode", description="授权码流程配置。"
    )

    model_config = ConfigDict(populate_by_name=True)


class OAuth2SecurityScheme(BaseModel):
    """
    OAuth 2.0 安全方案。
    """

    type: Literal["oauth2"] = Field(..., description="安全方案类型，固定为'oauth2'。")

    description: Optional[str] = Field(default=None, description="安全方案的描述信息。")

    flows: OAuth2Flows = Field(..., description="OAuth2流程配置。")

    model_config = ConfigDict(populate_by_name=True)


# 安全方案联合类型
SecurityScheme = Union[
    MutualTLSSecurityScheme,
    OpenIdConnectSecurityScheme,
    APIKeySecurityScheme,
    HTTPAuthSecurityScheme,
    OAuth2SecurityScheme,
]


# =============================================================================
# AgentEndPoint 智能体服务端点配置
# =============================================================================


class AgentEndPoint(BaseModel):
    """
    智能体服务端点配置对象。
    定义智能体对外提供服务的网络访问点，包括访问地址、
    传输协议和安全认证要求。支持多端点配置以实现不同的服务模式。
    """

    url: str = Field(
        ...,
        description="""
        此端点的完整URL地址。根据传输协议类型，URL的含义有所不同：
        - JSONRPC: 固定的RPC端点URL，所有RPC调用都发送到此地址
        - HTTP_JSON: API的Base URL，实际调用时会在此基础上拼接具体的API路径
        """,
        examples=[
            "https://api.example.com/rpc",
            "https://api.example.com/v1",
            "https://beijing-agent.example.com/api/v2",
        ],
    )

    transport: str = Field(
        ...,
        description="""
        此端点支持的传输协议类型：
        - JSONRPC: 基于JSON-RPC 2.0协议的远程过程调用
        - HTTP_JSON: 基于HTTP的JSON请求/响应，RESTful风格的API调用
        """,
        examples=["JSONRPC", "HTTP_JSON"],
    )

    security: Optional[List[Dict[str, List[str]]]] = Field(
        default=None,
        description="""
        适用于此端点的安全要求配置列表。遵循 OpenAPI 3.0 安全要求对象规范。
        - 外层数组表示 "OR" 关系：满足任意一个安全要求组合即可
        - 内层对象表示 "AND" 关系：同一对象内的所有方案都必须满足
        """,
        examples=[[{"mtls": []}], [{"mtls": []}, {"oidc": ["openid", "profile"]}]],
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# AgentSkill 智能体技能定义
# =============================================================================


class AgentSkill(BaseModel):
    """
    表示智能体可以执行的某方面的独特能力或功能。
    每个技能代表智能体的一个专门化能力，具有明确的功能边界和输入输出规范。
    """

    id: str = Field(
        ...,
        description="""
        智能体技能的唯一标识符。由提供者定义，建议使用分层命名空间格式。
        推荐的命名空间方案：
        1. 点分层格式：{agent-domain}.{skill-category}.{specific-skill}
        2. 冒号分层格式：{agent-domain}:{skill-category}:{specific-skill}
        """,
        examples=[
            "beijing-urban-tour.sight-recommender",
            "beijing-urban-tour:itinerary-planner",
        ],
    )

    name: str = Field(
        ...,
        description="技能的名称，简洁明了地描述技能的主要功能。",
        examples=["北京城区旅游景点推荐", "北京城区行程规划", "文化体验优化"],
    )

    description: str = Field(
        ...,
        description="技能的详细描述，帮助客户端或用户理解其目的、功能范围和限制。",
        examples=["根据客户需求推荐北京城六区内的旅游景点，提供文化深度体验建议。"],
    )

    version: str = Field(
        ...,
        description="技能的版本号，建议遵循语义化版本控制规范。格式：MAJOR.MINOR.PATCH",
        examples=["1.0.0", "2.1.3", "1.2.0-beta.1"],
    )

    tags: List[str] = Field(
        default_factory=list,
        description="描述技能能力特征的关键词集合，用于技能发现和匹配。",
        examples=[["旅游", "景点推荐", "北京", "城区", "文化体验"]],
    )

    examples: Optional[List[str]] = Field(
        default=None,
        description="此技能可以处理的示例提示或场景，帮助用户理解如何使用该技能。",
        examples=[["推荐几个适合带小孩的北京城区景点", "不要太累的故宫周边一日游安排"]],
    )

    input_modes: Optional[List[str]] = Field(
        default=None,
        alias="inputModes",
        description="此技能支持的输入 MIME 类型集合，覆盖智能体的默认值。",
        examples=[["text/plain", "application/json"]],
    )

    output_modes: Optional[List[str]] = Field(
        default=None,
        alias="outputModes",
        description="此技能支持的输出 MIME 类型集合，覆盖智能体的默认值。",
        examples=[["text/plain", "application/json", "text/markdown"]],
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# AgentCapabilitySpec 核心对象
# =============================================================================


class AgentCapabilitySpec(BaseModel):
    """
    智能体能力描述规范的根对象。
    这是ACS（Agent Capability Specification）的核心数据结构，
    完整描述了智能体的身份信息、功能能力、技术特性和服务接口。
    用于智能体的注册、发现、匹配和协作。

    基于 ACPs-spec-ACS-v02.02 规范。
    """

    # === 身份信息（由注册服务分配和维护）===

    aic: str = Field(
        ...,
        description="智能体唯一身份标识符，由注册服务分配。",
        examples=["10001000011K912345E789ABCDEF2353"],
    )

    active: bool = Field(
        ..., description="智能体的激活状态，由注册服务维护。", examples=[True, False]
    )

    last_modified_time: str = Field(
        ...,
        alias="lastModifiedTime",
        description="智能体能力描述的最后修改时间，由注册服务提供。采用ISO 8601格式，推荐使用北京时间（UTC+8）。",
        examples=["2025-03-15T16:30:00+08:00"],
    )

    # === 协议版本 ===

    protocol_version: str = Field(
        ...,
        alias="protocolVersion",
        description="此智能体支持的ACPs协议版本，用于协议兼容性检查和版本匹配。",
        examples=["02.01"],
    )

    # === 基本描述信息 ===

    name: str = Field(
        ...,
        description="此智能体的名称，简洁明了地描述智能体的主要功能。",
        examples=["北京城区旅游规划助手", "北京郊区景点推荐代理", "文化导览专家"],
    )

    description: str = Field(
        ...,
        description="此智能体的详细描述，帮助用户和其他智能体理解其用途和限制。",
        examples=["对北京城区旅游的规划和建议。只负责城六区，不负责郊区。"],
    )

    version: str = Field(
        ...,
        description="智能体的版本号，推荐遵循语义化版本控制规范。格式：MAJOR.MINOR.PATCH",
        examples=["1.0.0", "2.1.3", "1.2.0-beta.1"],
    )

    # === 附加信息（可选）===

    icon_url: Optional[str] = Field(
        default=None,
        alias="iconUrl",
        description="智能体图标的URL地址，用于在用户界面中显示智能体标识。",
        examples=["https://example.com/icons/beijing-agent.png"],
    )

    documentation_url: Optional[str] = Field(
        default=None,
        alias="documentationUrl",
        description="智能体详细文档的URL地址，提供使用说明和API文档。",
        examples=["https://docs.example.com/agents/beijing-tour"],
    )

    web_app_url: Optional[str] = Field(
        default=None,
        alias="webAppUrl",
        description="智能体能力展示的Web应用URL，用户可以通过此地址体验智能体功能。",
        examples=["https://demo.example.com/beijing-tour"],
    )

    # === 提供者信息 ===

    provider: AgentProvider = Field(
        ..., description="智能体服务提供者的详细信息，包括组织、联系方式等。"
    )

    # === 安全方案 ===

    security_schemes: Dict[str, SecurityScheme] = Field(
        ...,
        alias="securitySchemes",
        description="""
        用于授权请求的可用安全方案声明。键是方案名称，值是对应的安全方案配置。
        遵循 OpenAPI 3.0 安全方案对象规范。
        
        目前支持的方案包括：
        - mutualTLS: 双向TLS认证，适用于智能体间的高安全级别通信
        - openIdConnect: OpenID Connect认证，适用于用户身份验证场景
        """,
    )

    # === 服务端点 ===

    end_points: List[AgentEndPoint] = Field(
        default_factory=list,
        alias="endPoints",
        description="""
        智能体端点配置列表，定义智能体可访问的服务端点信息。
        多个端点应该支持相同的业务功能，支持不同的协议和认证方式。
        如果智能体没有提供服务端点给其它智能体使用，则此字段为空数组。
        """,
    )

    # === 技术能力 ===

    capabilities: AgentCapabilities = Field(
        ..., description="智能体支持的可选能力声明，如流式响应、异步通知、消息队列等。"
    )

    # === 输入输出格式 ===

    default_input_modes: List[str] = Field(
        default_factory=list,
        alias="defaultInputModes",
        description="所有技能的默认支持输入MIME类型集合，可在每个技能的基础上覆盖。",
        examples=[["text/plain", "application/json"]],
    )

    default_output_modes: List[str] = Field(
        default_factory=list,
        alias="defaultOutputModes",
        description="所有技能的默认支持输出MIME类型集合，可在每个技能的基础上覆盖。",
        examples=[["text/plain", "application/json"]],
    )

    # === 技能列表 ===

    skills: List[AgentSkill] = Field(
        default_factory=list,
        description="智能体可以执行的技能或独特能力集合。空数组表示本智能体没有定义任何技能。",
    )

    # === 实体相关信息（可选）===

    entity_user_id: Optional[str] = Field(
        default=None,
        alias="entityUserId",
        description="实体的用户关联ID。用于将智能体实体与特定用户绑定。",
    )

    entity_meta: Optional[Dict[str, Any]] = Field(
        default=None,
        alias="entityMeta",
        description="实体的额外元数据。具体格式和内容由Agent Provider自定义。",
        examples=[{"location": "Beijing", "environment": "production"}],
    )

    model_config = ConfigDict(populate_by_name=True)

    def to_json(self, **kwargs) -> str:
        """序列化为JSON字符串"""
        return self.model_dump_json(by_alias=True, exclude_none=True, **kwargs)

    def to_dict(self, **kwargs) -> Dict[str, Any]:
        """序列化为字典"""
        return self.model_dump(by_alias=True, exclude_none=True, **kwargs)

    @classmethod
    def from_json(cls, json_str: str) -> "AgentCapabilitySpec":
        """从JSON字符串反序列化"""
        return cls.model_validate_json(json_str)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentCapabilitySpec":
        """从字典反序列化"""
        return cls.model_validate(data)

    @classmethod
    def from_file(cls, file_path: Union[str, Path]) -> "AgentCapabilitySpec":
        """
        从本地 JSON 文件反序列化 AgentCapabilitySpec。

        Args:
            file_path: JSON 文件路径，支持 str 或 Path 对象。

        Returns:
            AgentCapabilitySpec 实例。

        Raises:
            FileNotFoundError: 文件不存在时抛出。
            ValueError: 文件扩展名不是 .json 时抛出。
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {path}")

        if path.suffix.lower() != ".json":
            raise ValueError(f"不支持的文件格式: '{path.suffix}'，仅支持 .json")

        return cls.from_json(path.read_text(encoding="utf-8"))
