/**
 * ACPs SDK - Agent Collaboration Protocols（智能体协作协议体系）Node.js SDK
 *
 * 提供 ACPs 协议体系的 TypeScript 实现，与 Python 版 `acps-sdk` 对齐：
 * - ACS (Agent Capability Specification) 智能体能力描述
 * - ADP (Agent Discovery Protocol) 智能体发现协议
 * - AIC (Agent Identity Code) 智能体身份码
 * - AIP (Agent Interaction Protocol) 智能体交互协议
 * - AMP (Agent Monitoring Protocol) 监控日志发射器
 * - OIDC (OpenID Connect) 真人认证授权共用能力
 *
 * 各子模块从子路径引入，例如 `import { AipRpcClient } from "acps-sdk/aip"`、
 * `import { HeartbeatEmitter } from "acps-sdk/amp"`。与 Python 版顶层包一样，根入口只导出版本号与公共配置。
 */

export const VERSION = "2.2.0";

export { setLoggerFactory } from "./internal/logger.js";
export type { Logger, LoggerFactory } from "./internal/logger.js";
export type {
  HttpRequestOptions,
  HttpResponseData,
  HttpStreamResponse,
  HttpStreamTransport,
  HttpTransport,
  TlsClientOptions,
} from "./internal/http.js";
