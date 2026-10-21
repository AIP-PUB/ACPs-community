/**
 * 跨语言对照：读取 scripts/fixtures/gen_oidc.py 用 Python 版 acps_sdk.oidc 生成的样例，
 * 断言 Node 版给出相同的结果（principal 内容、错误类型与文本、HTTP 状态码与响应体）。
 */

import { describe, expect, it } from "vitest";
import { ZodError } from "zod";

import {
  auditActorFromPrincipal,
  buildPrincipalId,
  buildPrincipalKey,
  canonicalJsonBytes,
  claimsToPrincipal,
  createOidcMiddleware,
  HumanPrincipalSchema,
  KeycloakClaimMappingSchema,
  normalizeAudiences,
  OidcDeviceClientConfigSchema,
  OidcProviderConfigSchema,
  OidcTokenResponse,
  OidcTokenResponseSchema,
  OidcTokenValidator,
  optionalPrincipal,
  parseMultiValueClaim,
  parseScopeClaim,
  requirePrincipal,
  requireRoles,
  requireScopes,
  type HumanPrincipal,
  type OidcFetch,
  type OidcProviderConfigInput,
} from "../../src/oidc/index.js";
import { loadPythonFixture } from "../fixtures.js";
import { fakeFetch, jsonResponse, runMiddleware } from "./helpers.js";

type ErrorExpectation = { type: string; message: string };
type ModelErrorExpectation =
  | ErrorExpectation
  | {
      type: "ValidationError";
      issues: Array<{ loc: Array<string | number>; type: string; msg: string }>;
    };

interface PrincipalExpectation {
  dump: Record<string, unknown>;
  subject: string;
  principal_key: string;
  raw_claims: Record<string, unknown>;
  audit_actor: Record<string, unknown>;
}

/** Python `model_dump()` 含值为 None 的字段，Node 版省略 undefined 字段：比较前去掉 null。 */
function withoutNone(value: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value)) {
    if (item === null) continue;
    result[key] =
      typeof item === "object" && !Array.isArray(item)
        ? withoutNone(item as Record<string, unknown>)
        : item;
  }
  return result;
}

function plain(value: unknown): unknown {
  return JSON.parse(JSON.stringify(value));
}

function expectPrincipal(principal: HumanPrincipal, expected: PrincipalExpectation): void {
  expect(plain(principal)).toEqual(withoutNone(expected.dump));
  expect(principal.subject).toBe(expected.subject);
  expect(principal.principal_key).toBe(expected.principal_key);
  expect(principal.raw_claims).toEqual(expected.raw_claims);
  expect(plain(auditActorFromPrincipal(principal))).toEqual(expected.audit_actor);
}

function expectError(error: unknown, expected: ErrorExpectation): void {
  expect(error).toBeInstanceOf(Error);
  expect({ type: (error as Error).name, message: (error as Error).message }).toEqual(expected);
}

function capture<T>(fn: () => T): { value: T } | { error: unknown } {
  try {
    return { value: fn() };
  } catch (error) {
    return { error };
  }
}

/** pydantic ValidationError ↔ ZodError：按字段路径比对；`value_error` 还要求文本一致。 */
function expectModelError(error: unknown, expected: ModelErrorExpectation): void {
  if (!("issues" in expected)) {
    expectError(error, expected);
    return;
  }
  expect(error).toBeInstanceOf(ZodError);
  const issues = (error as ZodError).issues;
  expect(issues).toHaveLength(expected.issues.length);
  for (const issue of expected.issues) {
    if (issue.type === "extra_forbidden") {
      const parent = issue.loc.slice(0, -1);
      const key = issue.loc[issue.loc.length - 1];
      expect(issues).toContainEqual(
        expect.objectContaining({ code: "unrecognized_keys", path: parent, keys: [key] }),
      );
    } else if (issue.type === "value_error") {
      expect(issues).toContainEqual(
        expect.objectContaining({ path: issue.loc, message: issue.msg }),
      );
    } else {
      expect(issues).toContainEqual(expect.objectContaining({ path: issue.loc }));
    }
  }
}

// =============================================================================
// principal.json
// =============================================================================

describe("python fixtures: principal", () => {
  const fixture = loadPythonFixture<{
    principal_ids: Array<{
      issuer: string;
      subject: string;
      principal_key: string;
      principal_id: string;
    }>;
    canonical_json: Array<{ value: unknown; hex: string }>;
    principals: Array<{
      input: Record<string, unknown>;
      dump: Record<string, unknown>;
      audit_actor: Record<string, unknown>;
    }>;
  }>("oidc", "principal");

  it.each(fixture.principal_ids)("principal id/key for $issuer / $subject", (entry) => {
    expect(buildPrincipalKey(entry)).toBe(entry.principal_key);
    expect(buildPrincipalId(entry)).toBe(entry.principal_id);
  });

  it.each(fixture.canonical_json.map((entry, index) => [index, entry] as const))(
    "canonicalJsonBytes #%i",
    (_index, entry) => {
      expect(canonicalJsonBytes(entry.value).toString("hex")).toBe(entry.hex);
    },
  );

  it.each(fixture.principals.map((entry, index) => [index, entry] as const))(
    "HumanPrincipal dump and audit actor #%i",
    (_index, entry) => {
      const principal = HumanPrincipalSchema.parse(entry.input);
      expect(plain(principal)).toEqual(withoutNone(entry.dump));
      expect(plain(auditActorFromPrincipal(principal))).toEqual(entry.audit_actor);
    },
  );
});

// =============================================================================
// keycloak.json
// =============================================================================

describe("python fixtures: keycloak", () => {
  const fixture = loadPythonFixture<{
    functions: Array<{
      fn: "normalizeAudiences" | "parseScopeClaim" | "parseMultiValueClaim";
      input: unknown;
      expected: { value: string[] } | { error: ErrorExpectation };
    }>;
    principals: Array<{
      name: string;
      claims: Record<string, unknown>;
      claim_mapping: Record<string, unknown>;
      expected: { principal: PrincipalExpectation } | { error: ErrorExpectation };
    }>;
  }>("oidc", "keycloak");
  const functions = { normalizeAudiences, parseScopeClaim, parseMultiValueClaim };

  it.each(fixture.functions)("$fn($input)", ({ fn, input, expected }) => {
    const result = capture(() => functions[fn](input));
    if ("error" in expected) {
      expect(result).toHaveProperty("error");
      expectError((result as { error: unknown }).error, expected.error);
    } else {
      expect(result).toEqual({ value: expected.value });
    }
  });

  it.each(fixture.principals)("claimsToPrincipal: $name", ({ claims, claim_mapping, expected }) => {
    const claimMapping = KeycloakClaimMappingSchema.parse(claim_mapping);
    const result = capture(() => claimsToPrincipal({ claims, claimMapping }));
    if ("error" in expected) {
      expect(result).toHaveProperty("error");
      expectError((result as { error: unknown }).error, expected.error);
    } else {
      expect(result).toHaveProperty("value");
      expectPrincipal((result as { value: HumanPrincipal }).value, expected.principal);
    }
  });
});

// =============================================================================
// models.json
// =============================================================================

describe("python fixtures: models", () => {
  const fixture = loadPythonFixture<{
    configs: Array<{
      model: "OidcProviderConfig" | "KeycloakClaimMapping" | "OidcDeviceClientConfig";
      input: Record<string, unknown>;
      expected: { value: Record<string, unknown> } | { error: ModelErrorExpectation };
    }>;
    token_responses: Array<{
      input: Record<string, unknown>;
      expected:
        | {
            value: Record<string, unknown>;
            access_token_value: string;
            refresh_token_value: string | null;
            id_token_value: string | null;
          }
        | { error: ModelErrorExpectation };
    }>;
  }>("oidc", "models");
  const schemas = {
    OidcProviderConfig: OidcProviderConfigSchema,
    KeycloakClaimMapping: KeycloakClaimMappingSchema,
    OidcDeviceClientConfig: OidcDeviceClientConfigSchema,
  };

  it.each(fixture.configs.map((entry, index) => [index, entry.model, entry] as const))(
    "config #%i %s",
    (_index, model, { input, expected }) => {
      const result = capture(() => schemas[model].parse(input));
      if ("error" in expected) {
        expect(result).toHaveProperty("error");
        expectModelError((result as { error: unknown }).error, expected.error);
      } else {
        expect(result).toHaveProperty("value");
        expect(plain((result as { value: unknown }).value)).toEqual(withoutNone(expected.value));
      }
    },
  );

  it.each(fixture.token_responses.map((entry, index) => [index, entry] as const))(
    "OidcTokenResponse #%i",
    (_index, { input, expected }) => {
      const result = capture(() => OidcTokenResponseSchema.parse(input));
      if ("error" in expected) {
        expect(result).toHaveProperty("error");
        expectModelError((result as { error: unknown }).error, expected.error);
        return;
      }
      expect(result).toHaveProperty("value");
      const response = (result as { value: OidcTokenResponse }).value;
      expect(plain(response)).toEqual(withoutNone(expected.value));
      expect(OidcTokenResponse.accessTokenValue(response)).toBe(expected.access_token_value);
      expect(OidcTokenResponse.refreshTokenValue(response)).toBe(expected.refresh_token_value);
      expect(OidcTokenResponse.idTokenValue(response)).toBe(expected.id_token_value);
    },
  );
});

// =============================================================================
// validator.json
// =============================================================================

interface ValidatorFixture {
  config: OidcProviderConfigInput;
  discovery: Record<string, unknown>;
  jwks_uri: string;
  jwks: Record<string, unknown>;
  cases: Array<{
    name: string;
    token: string;
    expected: { principal: PrincipalExpectation } | { error: ErrorExpectation };
  }>;
}

const validatorFixture = loadPythonFixture<ValidatorFixture>("oidc", "validator");

function fixtureProvider(): OidcFetch {
  return fakeFetch((request) => {
    if (request.path.endsWith("/.well-known/openid-configuration")) {
      return jsonResponse(200, validatorFixture.discovery);
    }
    if (request.url === validatorFixture.jwks_uri) {
      return jsonResponse(200, validatorFixture.jwks);
    }
    return jsonResponse(404, { detail: "not found" });
  });
}

/**
 * 只有错误文本不同的样例：这些文本来自 Python `json.loads` / UTF-8 解码器的异常信息，
 * Node 版保留相同的前缀 `Invalid payload string: ` 与异常类型。
 */
const MESSAGE_PREFIX_ONLY = new Set(["payload_not_json", "payload_invalid_utf8"]);

/**
 * 已知行为差异：Python 用宽松的 base64 解码（忽略 `.` 等非字母表字符），payload 段里夹了 `.` 的 4 段 token
 * 只要签名覆盖的字节相同就会被接受；jose 只接受恰好 3 段的 compact JWS。正常的 IdP 不会签发这种 token，
 * Node 版按签名失败拒绝。（带 `=` 填充的段两边都接受，见 `lenient_padded_header`。）
 */
const KNOWN_DIVERGENCES: Record<string, ErrorExpectation> = {
  lenient_dot_inside_payload: {
    type: "InvalidAccessTokenError",
    message: "Signature verification failed",
  },
};

describe("python fixtures: validator", () => {
  it.each(validatorFixture.cases)(
    "validateAccessToken: $name",
    async ({ name, token, expected }) => {
      const validator = new OidcTokenValidator({
        config: validatorFixture.config,
        fetch: fixtureProvider(),
      });
      const outcome = await validator.validateAccessToken(token).then(
        (principal) => ({ principal }),
        (error: unknown) => ({ error }),
      );

      const divergence = KNOWN_DIVERGENCES[name];
      if (divergence !== undefined) {
        expect(expected).toHaveProperty("principal");
        expect(outcome).toHaveProperty("error");
        expectError((outcome as { error: unknown }).error, divergence);
        return;
      }
      if ("error" in expected) {
        expect(outcome).toHaveProperty("error");
        const error = (outcome as { error: Error }).error;
        if (MESSAGE_PREFIX_ONLY.has(name)) {
          expect(error.name).toBe(expected.error.type);
          expect(error.message.startsWith("Invalid payload string: ")).toBe(true);
          expect(expected.error.message.startsWith("Invalid payload string: ")).toBe(true);
        } else {
          expectError(error, expected.error);
        }
      } else {
        expect(outcome).toHaveProperty("principal");
        expectPrincipal((outcome as { principal: HumanPrincipal }).principal, expected.principal);
      }
    },
  );
});

// =============================================================================
// http.json
// =============================================================================

describe("python fixtures: http dependencies", () => {
  const fixture = loadPythonFixture<{
    required_roles: string[];
    required_scopes: string[];
    cases: Array<{
      name: string;
      provider: "ok" | "broken";
      endpoint: "principal" | "optional" | "roles" | "scopes";
      authorization: string | null;
      expected: { status: number; body: unknown; www_authenticate: string | null };
    }>;
  }>("oidc", "http");

  it.each(fixture.cases)("$name", async ({ provider, endpoint, authorization, expected }) => {
    const fetch: OidcFetch =
      provider === "ok" ? fixtureProvider() : () => Promise.reject(new TypeError("network down"));
    const validator = new OidcTokenValidator({ config: validatorFixture.config, fetch });
    const dependency = {
      principal: requirePrincipal(validator),
      optional: optionalPrincipal(validator),
      roles: requireRoles(validator, fixture.required_roles),
      scopes: requireScopes(validator, fixture.required_scopes),
    }[endpoint];

    const outcome = await runMiddleware(
      createOidcMiddleware(dependency),
      authorization === null ? {} : { authorization },
    );
    if (outcome.nextCalled) {
      expect(outcome.nextError).toBeUndefined();
      // 与 gen_oidc.py 里的 FastAPI 路由处理函数一致
      const principal = outcome.req.principal as HumanPrincipal | null;
      const body =
        endpoint === "optional"
          ? { authenticated: principal !== null }
          : endpoint === "principal"
            ? { principalId: principal?.principal_id, roles: principal?.roles }
            : { principalId: principal?.principal_id };
      outcome.res.end(JSON.stringify(body));
    }

    expect({
      status: outcome.res.statusCode,
      body: outcome.res.json(),
      www_authenticate: outcome.res.headers.get("www-authenticate") ?? null,
    }).toEqual(expected);
  });
});
