/**
 * 对应 acps-sdk/tests/oidc/test_package_exports.py。
 *
 * Python 用例验证“未安装 PyJWT 时 FastAPI 辅助仍可导入、`OidcTokenValidator` 给出安装提示”；
 * Node 版 `jose` 是硬依赖，不存在可选依赖缺失的分支，因此这里改为核对包入口导出了
 * Python `__all__` 中的每个名字（按 porting-notes 改名）。
 */

import { describe, expect, it } from "vitest";

import * as oidc from "../../src/oidc/index.js";

/** Python `acps_sdk.oidc.__all__` → Node 名称。 */
const PYTHON_ALL: Record<string, string> = {
  HumanPrincipal: "HumanPrincipal",
  KeycloakClaimMapping: "KeycloakClaimMappingSchema",
  OidcProviderConfig: "OidcProviderConfigSchema",
  OidcDeviceClientConfig: "OidcDeviceClientConfigSchema",
  OidcDeviceAuthorizationResponse: "OidcDeviceAuthorizationResponseSchema",
  OidcTokenResponse: "OidcTokenResponse",
  OidcDeviceTokenPollingStatus: "OidcDeviceTokenPollingStatus",
  OidcDeviceTokenPollingResult: "OidcDeviceTokenPollingResultSchema",
  OidcTokenRevocationResult: "OidcTokenRevocationResultSchema",
  OidcDeviceClient: "OidcDeviceClient",
  OidcDiscoveryCache: "OidcDiscoveryCache",
  OidcDiscoveryDocument: "OidcDiscoveryDocumentSchema",
  OidcError: "OidcError",
  OidcClientError: "OidcClientError",
  OidcAuthenticationError: "OidcAuthenticationError",
  OidcAuthorizationError: "OidcAuthorizationError",
  OidcProviderUnavailableError: "OidcProviderUnavailableError",
  OidcDeviceAuthorizationNotSupportedError: "OidcDeviceAuthorizationNotSupportedError",
  OidcDeviceAuthorizationDeniedError: "OidcDeviceAuthorizationDeniedError",
  OidcDeviceAuthorizationExpiredError: "OidcDeviceAuthorizationExpiredError",
  MissingBearerTokenError: "MissingBearerTokenError",
  InvalidAccessTokenError: "InvalidAccessTokenError",
  MissingRoleError: "MissingRoleError",
  MissingScopeError: "MissingScopeError",
  build_principal_key: "buildPrincipalKey",
  build_principal_id: "buildPrincipalId",
  canonical_json_bytes: "canonicalJsonBytes",
  normalize_audiences: "normalizeAudiences",
  parse_scope_claim: "parseScopeClaim",
  parse_multi_value_claim: "parseMultiValueClaim",
  claims_to_principal: "claimsToPrincipal",
  require_principal: "requirePrincipal",
  optional_principal: "optionalPrincipal",
  require_roles: "requireRoles",
  require_scopes: "requireScopes",
  audit_actor_from_principal: "auditActorFromPrincipal",
  OidcTokenValidator: "OidcTokenValidator",
};

describe("test_package_exports (adapted)", () => {
  it.each(Object.entries(PYTHON_ALL))("exports %s as %s", (_python, node) => {
    expect(oidc).toHaveProperty(node);
    expect((oidc as Record<string, unknown>)[node]).toBeDefined();
  });

  it("keeps the HTTP helpers callable without a validator dependency (fastapi.py analogue)", () => {
    expect(typeof oidc.auditActorFromPrincipal).toBe("function");
    expect(typeof oidc.requirePrincipal).toBe("function");
    expect(typeof oidc.optionalPrincipal).toBe("function");
  });

  it("error classes keep the Python hierarchy and names", () => {
    const cases: Array<[Error, Array<new (...args: never[]) => Error>]> = [
      [new oidc.MissingBearerTokenError("x"), [oidc.OidcAuthenticationError, oidc.OidcError]],
      [new oidc.InvalidAccessTokenError("x"), [oidc.OidcAuthenticationError, oidc.OidcError]],
      [new oidc.MissingRoleError("x"), [oidc.OidcAuthorizationError, oidc.OidcError]],
      [new oidc.MissingScopeError("x"), [oidc.OidcAuthorizationError, oidc.OidcError]],
      [new oidc.OidcProviderUnavailableError("x"), [oidc.OidcError]],
      [new oidc.OidcDeviceAuthorizationDeniedError("x"), [oidc.OidcClientError, oidc.OidcError]],
      [new oidc.OidcDeviceAuthorizationExpiredError("x"), [oidc.OidcClientError, oidc.OidcError]],
      [
        new oidc.OidcDeviceAuthorizationNotSupportedError("x"),
        [oidc.OidcClientError, oidc.OidcError],
      ],
    ];
    for (const [error, parents] of cases) {
      for (const parent of parents) {
        expect(error).toBeInstanceOf(parent);
      }
      expect(error.name).toBe(error.constructor.name);
    }
  });
});
