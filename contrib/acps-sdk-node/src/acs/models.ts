/**
 * ACS (Agent Capability Specification) 数据模型定义（对应 Python `acps_sdk.acs.models`）
 *
 * 基于 ACPs-spec-ACS-v02.02 规范定义的智能体能力描述数据模型。
 *
 * - 字段名即线上 JSON 名（lowerCamelCase，对应 Python 的 alias）。
 * - 每个模型导出 zod schema（`XxxSchema`）与解析后的类型（`Xxx`）；需要手写入参时用 `XxxInput`。
 * - Python `AgentCapabilitySpec` 上的 `to_json()` / `to_dict()` / `from_json()` / `from_dict()` /
 *   `from_file()` 放在同名伴生对象上，例如 `AgentCapabilitySpec.fromFile("agent.json")`。
 */

import { existsSync, readFileSync } from "node:fs";
import { extname } from "node:path";
import { fileURLToPath } from "node:url";

import { z } from "zod";

import { AnyDict, optional } from "../internal/schema.js";

// =============================================================================
// MQProtocolVersion 消息队列协议版本枚举
// =============================================================================

/**
 * 消息队列协议版本枚举类型。
 *
 * 定义智能体支持的各种消息队列中间件及其具体版本，用于异步消息传递、事件通知和分布式系统集成。
 * 使用 "protocol:version" 格式提供精确的技术栈定义。
 */
export const MQProtocolVersion = {
  // MQTT 版本
  MQTT_3_1_1: "mqtt:3.1.1",
  MQTT_5_0: "mqtt:5.0",
  // AMQP 版本
  AMQP_0_9_1: "amqp:0.9.1",
  AMQP_1_0: "amqp:1.0",
  // Kafka 版本
  KAFKA_2_8: "kafka:2.8",
  KAFKA_3_0: "kafka:3.0",
  KAFKA_3_1: "kafka:3.1",
  // Redis 版本
  REDIS_6_0: "redis:6.0",
  REDIS_7_0: "redis:7.0",
  REDIS_7_2: "redis:7.2",
  // RabbitMQ 版本
  RABBITMQ_3_9: "rabbitmq:3.9",
  RABBITMQ_3_10: "rabbitmq:3.10",
  RABBITMQ_3_11: "rabbitmq:3.11",
} as const;
export type MQProtocolVersion = (typeof MQProtocolVersion)[keyof typeof MQProtocolVersion];
export const MQProtocolVersionSchema = z.enum(MQProtocolVersion);

// =============================================================================
// AgentProvider 智能体服务提供者信息
// =============================================================================

/**
 * 智能体服务提供者信息对象。
 *
 * 定义智能体的开发和维护组织信息，包括组织身份、联系方式和合规资质。
 * 用于建立信任关系和提供技术支持联系渠道。
 */
export const AgentProviderSchema = z.object({
  /**
   * 智能体提供者的国家或地区代码，符合 ISO 3166-1 alpha-2 标准，如 "CN"、"US"、"GB"。
   *
   * 与 Python `Optional[str] = "CN"` 一致：缺省时为 "CN"；显式传 null 时为 undefined（序列化时省略）。
   */
  countryCode: z
    .string()
    .nullable()
    .default("CN")
    .transform((value) => value ?? undefined),
  /** 智能体提供者的组织名称，通常为公司、大学或研究机构等顶级组织。 */
  organization: optional(z.string()),
  /** 智能体提供者的具体部门或院系名称，提供更精确的组织结构信息。 */
  department: optional(z.string()),
  /** 智能体提供者的官方网站或相关文档的 URL 地址。 */
  url: optional(z.string()),
  /** 智能体提供者的法律备案信息或许可证号，用于合规性验证。 */
  license: optional(z.string()),
  /** 智能体提供者的联系人姓名，便于技术支持和沟通。 */
  name: optional(z.string()),
  /** 智能体提供者的联系人电子邮箱地址。 */
  email: optional(z.string()),
});
export type AgentProvider = z.output<typeof AgentProviderSchema>;
export type AgentProviderInput = z.input<typeof AgentProviderSchema>;

// =============================================================================
// AgentCapabilities 智能体技术能力配置
// =============================================================================

/**
 * 智能体可选技术能力配置对象。
 *
 * 定义智能体支持的高级功能特性，如实时通信、异步通知和消息队列集成。
 */
export const AgentCapabilitiesSchema = z.object({
  /** 智能体是否支持 Server Send Event（SSE）用于流式响应。 */
  streaming: z.boolean(),
  /** 智能体是否支持异步推送通知。 */
  notification: z.boolean(),
  /** 智能体支持的消息队列协议版本列表。空数组表示不支持任何消息队列协议。 */
  messageQueue: z.array(MQProtocolVersionSchema).default(() => []),
});
export type AgentCapabilities = z.output<typeof AgentCapabilitiesSchema>;
export type AgentCapabilitiesInput = z.input<typeof AgentCapabilitiesSchema>;

// =============================================================================
// SecurityScheme 安全方案定义
// =============================================================================

/**
 * 双向 TLS 认证安全方案，用于智能体间的高安全级别通信。
 * 要求客户端和服务端都提供有效的证书进行相互验证。
 */
export const MutualTLSSecuritySchemeSchema = z.object({
  /** 安全方案类型，固定为 'mutualTLS'。 */
  type: z.literal("mutualTLS"),
  /** 安全方案的描述信息，说明该方案的用途和特点。 */
  description: optional(z.string()),
  /** 兼容层（只读，不再增强）：旧版 challenge 流程的基础 URL 扩展字段。EAB 主链路下仅保留兼容读取语义。 */
  "x-caChallengeBaseUrl": optional(z.string()),
});
export type MutualTLSSecurityScheme = z.output<typeof MutualTLSSecuritySchemeSchema>;
export type MutualTLSSecuritySchemeInput = z.input<typeof MutualTLSSecuritySchemeSchema>;

/**
 * OpenID Connect 认证安全方案，基于 OAuth 2.0 协议的身份认证层。
 * 提供标准化的身份验证和用户信息获取能力。
 */
export const OpenIdConnectSecuritySchemeSchema = z.object({
  /** 安全方案类型，固定为 'openIdConnect'。 */
  type: z.literal("openIdConnect"),
  /** 安全方案的描述信息，说明该 OIDC 方案的用途和特点。 */
  description: optional(z.string()),
  /** OpenID Connect 发现文档的 URL，用于自动发现认证端点和配置信息。 */
  openIdConnectUrl: z.string(),
});
export type OpenIdConnectSecurityScheme = z.output<typeof OpenIdConnectSecuritySchemeSchema>;
export type OpenIdConnectSecuritySchemeInput = z.input<typeof OpenIdConnectSecuritySchemeSchema>;

/** API Key 安全方案，通过 API 密钥进行身份验证。 */
export const APIKeySecuritySchemeSchema = z.object({
  /** 安全方案类型，固定为 'apiKey'。 */
  type: z.literal("apiKey"),
  /** 安全方案的描述信息。 */
  description: optional(z.string()),
  /** API 密钥参数的名称。 */
  name: z.string(),
  /** API 密钥的位置：query, header 或 cookie（Python 属性名为 `in_location`）。 */
  in: z.enum(["query", "header", "cookie"]),
});
export type APIKeySecurityScheme = z.output<typeof APIKeySecuritySchemeSchema>;
export type APIKeySecuritySchemeInput = z.input<typeof APIKeySecuritySchemeSchema>;

/** HTTP 认证安全方案，支持 Basic、Bearer 等认证方式。 */
export const HTTPAuthSecuritySchemeSchema = z.object({
  /** 安全方案类型，固定为 'http'。 */
  type: z.literal("http"),
  /** 安全方案的描述信息。 */
  description: optional(z.string()),
  /** HTTP 认证方案名称，如 'basic', 'bearer' 等。 */
  scheme: z.string(),
  /** Bearer 令牌的格式提示，仅当 scheme 为 'bearer' 时使用。 */
  bearerFormat: optional(z.string()),
});
export type HTTPAuthSecurityScheme = z.output<typeof HTTPAuthSecuritySchemeSchema>;
export type HTTPAuthSecuritySchemeInput = z.input<typeof HTTPAuthSecuritySchemeSchema>;

/** OAuth2 流程配置 */
export const OAuth2FlowSchema = z.object({
  /** 授权端点 URL。 */
  authorizationUrl: optional(z.string()),
  /** 令牌端点 URL。 */
  tokenUrl: optional(z.string()),
  /** 刷新令牌端点 URL。 */
  refreshUrl: optional(z.string()),
  /** 可用的 OAuth2 作用域及其描述。 */
  scopes: z.record(z.string(), z.string()).default(() => ({})),
});
export type OAuth2Flow = z.output<typeof OAuth2FlowSchema>;
export type OAuth2FlowInput = z.input<typeof OAuth2FlowSchema>;

/** OAuth2 流程集合 */
export const OAuth2FlowsSchema = z.object({
  /** 隐式授权流程配置。 */
  implicit: optional(OAuth2FlowSchema),
  /** 密码授权流程配置。 */
  password: optional(OAuth2FlowSchema),
  /** 客户端凭证授权流程配置。 */
  clientCredentials: optional(OAuth2FlowSchema),
  /** 授权码流程配置。 */
  authorizationCode: optional(OAuth2FlowSchema),
});
export type OAuth2Flows = z.output<typeof OAuth2FlowsSchema>;
export type OAuth2FlowsInput = z.input<typeof OAuth2FlowsSchema>;

/** OAuth 2.0 安全方案。 */
export const OAuth2SecuritySchemeSchema = z.object({
  /** 安全方案类型，固定为 'oauth2'。 */
  type: z.literal("oauth2"),
  /** 安全方案的描述信息。 */
  description: optional(z.string()),
  /** OAuth2 流程配置。 */
  flows: OAuth2FlowsSchema,
});
export type OAuth2SecurityScheme = z.output<typeof OAuth2SecuritySchemeSchema>;
export type OAuth2SecuritySchemeInput = z.input<typeof OAuth2SecuritySchemeSchema>;

/**
 * 安全方案联合类型。
 *
 * 五种方案的 `type` 字面量都是必填且互不相同，因此按 `type` 判别与 pydantic 的 smart-mode Union
 * 接受/拒绝的输入完全一致（只是校验失败时的错误结构不同）。
 */
export const SecuritySchemeSchema = z.discriminatedUnion("type", [
  MutualTLSSecuritySchemeSchema,
  OpenIdConnectSecuritySchemeSchema,
  APIKeySecuritySchemeSchema,
  HTTPAuthSecuritySchemeSchema,
  OAuth2SecuritySchemeSchema,
]);
export type SecurityScheme = z.output<typeof SecuritySchemeSchema>;
export type SecuritySchemeInput = z.input<typeof SecuritySchemeSchema>;

// =============================================================================
// AgentEndPoint 智能体服务端点配置
// =============================================================================

/**
 * 智能体服务端点配置对象。
 *
 * 定义智能体对外提供服务的网络访问点，包括访问地址、传输协议和安全认证要求。
 * 支持多端点配置以实现不同的服务模式。
 */
export const AgentEndPointSchema = z.object({
  /**
   * 此端点的完整 URL 地址。根据传输协议类型，URL 的含义有所不同：
   * - JSONRPC: 固定的 RPC 端点 URL，所有 RPC 调用都发送到此地址
   * - HTTP_JSON: API 的 Base URL，实际调用时会在此基础上拼接具体的 API 路径
   */
  url: z.string(),
  /**
   * 此端点支持的传输协议类型：
   * - JSONRPC: 基于 JSON-RPC 2.0 协议的远程过程调用
   * - HTTP_JSON: 基于 HTTP 的 JSON 请求/响应，RESTful 风格的 API 调用
   */
  transport: z.string(),
  /**
   * 适用于此端点的安全要求配置列表。遵循 OpenAPI 3.0 安全要求对象规范。
   * - 外层数组表示 "OR" 关系：满足任意一个安全要求组合即可
   * - 内层对象表示 "AND" 关系：同一对象内的所有方案都必须满足
   */
  security: optional(z.array(z.record(z.string(), z.array(z.string())))),
});
export type AgentEndPoint = z.output<typeof AgentEndPointSchema>;
export type AgentEndPointInput = z.input<typeof AgentEndPointSchema>;

// =============================================================================
// AgentSkill 智能体技能定义
// =============================================================================

/**
 * 表示智能体可以执行的某方面的独特能力或功能。
 * 每个技能代表智能体的一个专门化能力，具有明确的功能边界和输入输出规范。
 */
export const AgentSkillSchema = z.object({
  /**
   * 智能体技能的唯一标识符。由提供者定义，建议使用分层命名空间格式：
   * 1. 点分层格式：{agent-domain}.{skill-category}.{specific-skill}
   * 2. 冒号分层格式：{agent-domain}:{skill-category}:{specific-skill}
   */
  id: z.string(),
  /** 技能的名称，简洁明了地描述技能的主要功能。 */
  name: z.string(),
  /** 技能的详细描述，帮助客户端或用户理解其目的、功能范围和限制。 */
  description: z.string(),
  /** 技能的版本号，建议遵循语义化版本控制规范。格式：MAJOR.MINOR.PATCH */
  version: z.string(),
  /** 描述技能能力特征的关键词集合，用于技能发现和匹配。 */
  tags: z.array(z.string()).default(() => []),
  /** 此技能可以处理的示例提示或场景，帮助用户理解如何使用该技能。 */
  examples: optional(z.array(z.string())),
  /** 此技能支持的输入 MIME 类型集合，覆盖智能体的默认值。 */
  inputModes: optional(z.array(z.string())),
  /** 此技能支持的输出 MIME 类型集合，覆盖智能体的默认值。 */
  outputModes: optional(z.array(z.string())),
});
export type AgentSkill = z.output<typeof AgentSkillSchema>;
export type AgentSkillInput = z.input<typeof AgentSkillSchema>;

// =============================================================================
// AgentCapabilitySpec 核心对象
// =============================================================================

/**
 * 智能体能力描述规范的根对象。
 *
 * 这是 ACS（Agent Capability Specification）的核心数据结构，
 * 完整描述了智能体的身份信息、功能能力、技术特性和服务接口。
 * 用于智能体的注册、发现、匹配和协作。
 *
 * 基于 ACPs-spec-ACS-v02.02 规范。
 */
export const AgentCapabilitySpecSchema = z.object({
  // === 身份信息（由注册服务分配和维护）===

  /** 智能体唯一身份标识符，由注册服务分配。 */
  aic: z.string(),
  /** 智能体的激活状态，由注册服务维护。 */
  active: z.boolean(),
  /** 智能体能力描述的最后修改时间，由注册服务提供。采用 ISO 8601 格式，推荐使用北京时间（UTC+8）。 */
  lastModifiedTime: z.string(),

  // === 协议版本 ===

  /** 此智能体支持的 ACPs 协议版本，用于协议兼容性检查和版本匹配，如 "02.01"。 */
  protocolVersion: z.string(),

  // === 基本描述信息 ===

  /** 此智能体的名称，简洁明了地描述智能体的主要功能。 */
  name: z.string(),
  /** 此智能体的详细描述，帮助用户和其他智能体理解其用途和限制。 */
  description: z.string(),
  /** 智能体的版本号，推荐遵循语义化版本控制规范。格式：MAJOR.MINOR.PATCH */
  version: z.string(),

  // === 附加信息（可选）===

  /** 智能体图标的 URL 地址，用于在用户界面中显示智能体标识。 */
  iconUrl: optional(z.string()),
  /** 智能体详细文档的 URL 地址，提供使用说明和 API 文档。 */
  documentationUrl: optional(z.string()),
  /** 智能体能力展示的 Web 应用 URL，用户可以通过此地址体验智能体功能。 */
  webAppUrl: optional(z.string()),

  // === 提供者信息 ===

  /** 智能体服务提供者的详细信息，包括组织、联系方式等。 */
  provider: AgentProviderSchema,

  // === 安全方案 ===

  /**
   * 用于授权请求的可用安全方案声明。键是方案名称，值是对应的安全方案配置。
   * 遵循 OpenAPI 3.0 安全方案对象规范。
   */
  securitySchemes: z.record(z.string(), SecuritySchemeSchema),

  // === 服务端点 ===

  /**
   * 智能体端点配置列表，定义智能体可访问的服务端点信息。
   * 如果智能体没有提供服务端点给其它智能体使用，则此字段为空数组。
   */
  endPoints: z.array(AgentEndPointSchema).default(() => []),

  // === 技术能力 ===

  /** 智能体支持的可选能力声明，如流式响应、异步通知、消息队列等。 */
  capabilities: AgentCapabilitiesSchema,

  // === 输入输出格式 ===

  /** 所有技能的默认支持输入 MIME 类型集合，可在每个技能的基础上覆盖。 */
  defaultInputModes: z.array(z.string()).default(() => []),
  /** 所有技能的默认支持输出 MIME 类型集合，可在每个技能的基础上覆盖。 */
  defaultOutputModes: z.array(z.string()).default(() => []),

  // === 技能列表 ===

  /** 智能体可以执行的技能或独特能力集合。空数组表示本智能体没有定义任何技能。 */
  skills: z.array(AgentSkillSchema).default(() => []),

  // === 实体相关信息（可选）===

  /** 实体的用户关联 ID。用于将智能体实体与特定用户绑定。 */
  entityUserId: optional(z.string()),
  /** 实体的额外元数据。具体格式和内容由 Agent Provider 自定义。 */
  entityMeta: optional(AnyDict),
});
export type AgentCapabilitySpec = z.output<typeof AgentCapabilitySpecSchema>;
export type AgentCapabilitySpecInput = z.input<typeof AgentCapabilitySpecSchema>;

/** Python `pathlib.Path.suffix`：以点开头的隐藏文件名、以点结尾的文件名都没有后缀。 */
function pathSuffix(filePath: string): string {
  const ext = extname(filePath);
  return ext === "." ? "" : ext;
}

/** AgentCapabilitySpec 的方法（对应 Python `AgentCapabilitySpec` 上的实例方法与类方法）。 */
export const AgentCapabilitySpec = {
  /** 序列化为 JSON 字符串（camelCase 字段名，省略值为空的字段）。 */
  toJson(spec: AgentCapabilitySpec): string {
    return JSON.stringify(spec);
  },

  /** 序列化为普通对象（camelCase 字段名，省略值为空的字段；返回深拷贝）。 */
  toDict(spec: AgentCapabilitySpec): Record<string, unknown> {
    return JSON.parse(JSON.stringify(spec)) as Record<string, unknown>;
  },

  /**
   * 从 JSON 字符串反序列化。
   *
   * @throws SyntaxError JSON 格式错误
   * @throws z.ZodError 校验不通过
   */
  fromJson(json: string): AgentCapabilitySpec {
    return AgentCapabilitySpecSchema.parse(JSON.parse(json));
  },

  /**
   * 从普通对象反序列化。
   *
   * @throws z.ZodError 校验不通过
   */
  fromDict(data: unknown): AgentCapabilitySpec {
    return AgentCapabilitySpecSchema.parse(data);
  },

  /**
   * 从本地 JSON 文件反序列化 AgentCapabilitySpec（同步读取，与 Python 版一致）。
   *
   * @param filePath JSON 文件路径，支持字符串或 `file:` URL
   * @throws Error 文件不存在时抛出（`code` 为 "ENOENT"，对应 Python FileNotFoundError）
   * @throws Error 文件扩展名不是 .json 时抛出（对应 Python ValueError）
   * @throws SyntaxError JSON 格式错误
   * @throws z.ZodError 校验不通过
   */
  fromFile(filePath: string | URL): AgentCapabilitySpec {
    const path = filePath instanceof URL ? fileURLToPath(filePath) : filePath;
    if (!existsSync(path)) {
      throw Object.assign(new Error(`文件不存在: ${path}`), { code: "ENOENT" });
    }

    const suffix = pathSuffix(path);
    if (suffix.toLowerCase() !== ".json") {
      throw new Error(`不支持的文件格式: '${suffix}'，仅支持 .json`);
    }

    return AgentCapabilitySpec.fromJson(readFileSync(path, "utf-8"));
  },
} as const;
