/**
 * 供 ACPs 各服务复用的 OIDC 辅助能力（对应 Python `acps_sdk.oidc`）。
 *
 * 导出与 Python `__all__` 一一对应（按 porting-notes 改名）；pydantic 模型额外导出 `XxxSchema` 与 `XxxInput`。
 * FastAPI 依赖由框架无关的 `./http.ts` 替代，另外提供 bearer 解析、HTTP 映射与 Express 兼容中间件。
 */

export {
  OidcDeviceAuthorizationResponseSchema,
  OidcDeviceClient,
  OidcDeviceClientConfigSchema,
  OidcDeviceTokenPollingResultSchema,
  OidcDeviceTokenPollingStatus,
  OidcDeviceTokenPollingStatusSchema,
  OidcTokenResponse,
  OidcTokenResponseSchema,
  OidcTokenRevocationResultSchema,
  SecretStr,
} from "./client.js";
export type {
  OidcDeviceAuthorizationResponse,
  OidcDeviceAuthorizationResponseInput,
  OidcDeviceClientConfig,
  OidcDeviceClientConfigInput,
  OidcDeviceClientOptions,
  OidcDeviceTokenPollingResult,
  OidcDeviceTokenPollingResultInput,
  OidcTokenResponseInput,
  OidcTokenRevocationResult,
} from "./client.js";

export { KeycloakClaimMappingSchema, OidcProviderConfigSchema } from "./config.js";
export type {
  KeycloakClaimMapping,
  KeycloakClaimMappingInput,
  OidcProviderConfig,
  OidcProviderConfigInput,
  StringListInput,
} from "./config.js";

export { OidcDiscoveryCache, OidcDiscoveryDocumentSchema } from "./discovery.js";
export type {
  OidcDiscoveryCacheOptions,
  OidcDiscoveryDocument,
  OidcDiscoveryDocumentInput,
} from "./discovery.js";

export {
  InvalidAccessTokenError,
  MissingBearerTokenError,
  MissingRoleError,
  MissingScopeError,
  OidcAuthenticationError,
  OidcAuthorizationError,
  OidcClientError,
  OidcDeviceAuthorizationDeniedError,
  OidcDeviceAuthorizationExpiredError,
  OidcDeviceAuthorizationNotSupportedError,
  OidcError,
  OidcProviderUnavailableError,
} from "./errors.js";
export type { OidcClientErrorOptions } from "./errors.js";

export {
  auditActorFromPrincipal,
  createOidcMiddleware,
  extractBearerToken,
  getAuthorizationHeader,
  oidcErrorToHttpResponse,
  optionalPrincipal,
  requirePrincipal,
  requireRoles,
  requireScopes,
  sendOidcErrorResponse,
} from "./http.js";
export type {
  AccessTokenValidator,
  AuthorizationSource,
  HeadersGetter,
  NonStringIterable,
  OidcAuthenticatedRequest,
  OidcDependency,
  OidcHttpErrorResponse,
  OidcMiddleware,
  OidcMiddlewareRequest,
  OidcMiddlewareResponse,
} from "./http.js";

export {
  claimsToPrincipal,
  normalizeAudiences,
  parseMultiValueClaim,
  parseScopeClaim,
} from "./keycloak.js";

export {
  buildPrincipalId,
  buildPrincipalKey,
  canonicalJsonBytes,
  HumanPrincipal,
  HumanPrincipalSchema,
} from "./principal.js";
export type { HumanPrincipalInput } from "./principal.js";

export type { OidcFetch } from "./transport.js";

export { OidcTokenValidator } from "./validator.js";
export type { OidcCacheOptions, OidcTokenValidatorOptions } from "./validator.js";
