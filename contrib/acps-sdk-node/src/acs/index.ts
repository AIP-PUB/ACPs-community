/**
 * ACS (Agent Capability Specification) 智能体能力描述模型（对应 Python `acps_sdk.acs`）
 *
 * 本模块定义了 ACPs 协议体系中智能体能力描述的数据模型，基于 ACPs-spec-ACS-v02.02 规范。
 *
 * ```ts
 * import { AgentCapabilitySpec } from "acps-sdk/acs";
 *
 * const spec = AgentCapabilitySpec.fromDict(data); // 从对象创建
 * const fromJson = AgentCapabilitySpec.fromJson(jsonStr); // 从 JSON 字符串创建
 * const fromFile = AgentCapabilitySpec.fromFile("agent.json"); // 从 JSON 文件加载
 *
 * const json = AgentCapabilitySpec.toJson(spec); // 序列化
 * const dict = AgentCapabilitySpec.toDict(spec);
 * ```
 *
 * 除 Python `__init__` 导出的名字外，这里还导出了 `OAuth2Flow` / `OAuth2Flows`
 * （Python 只能从 `acps_sdk.acs.models` 引入），以及每个模型的 `XxxSchema` / `XxxInput`。
 */

export {
  // 核心对象
  type AgentCapabilitySpecInput,
  AgentCapabilitySpec,
  AgentCapabilitySpecSchema,
  // Provider
  type AgentProvider,
  type AgentProviderInput,
  AgentProviderSchema,
  // Capabilities
  type AgentCapabilities,
  type AgentCapabilitiesInput,
  AgentCapabilitiesSchema,
  MQProtocolVersion,
  MQProtocolVersionSchema,
  // Security
  type SecurityScheme,
  type SecuritySchemeInput,
  SecuritySchemeSchema,
  type MutualTLSSecurityScheme,
  type MutualTLSSecuritySchemeInput,
  MutualTLSSecuritySchemeSchema,
  type OpenIdConnectSecurityScheme,
  type OpenIdConnectSecuritySchemeInput,
  OpenIdConnectSecuritySchemeSchema,
  type APIKeySecurityScheme,
  type APIKeySecuritySchemeInput,
  APIKeySecuritySchemeSchema,
  type HTTPAuthSecurityScheme,
  type HTTPAuthSecuritySchemeInput,
  HTTPAuthSecuritySchemeSchema,
  type OAuth2SecurityScheme,
  type OAuth2SecuritySchemeInput,
  OAuth2SecuritySchemeSchema,
  type OAuth2Flow,
  type OAuth2FlowInput,
  OAuth2FlowSchema,
  type OAuth2Flows,
  type OAuth2FlowsInput,
  OAuth2FlowsSchema,
  // EndPoint
  type AgentEndPoint,
  type AgentEndPointInput,
  AgentEndPointSchema,
  // Skill
  type AgentSkill,
  type AgentSkillInput,
  AgentSkillSchema,
} from "./models.js";
