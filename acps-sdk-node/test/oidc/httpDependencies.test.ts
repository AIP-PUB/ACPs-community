/**
 * 移植自 acps-sdk/tests/oidc/test_fastapi_dependencies.py。
 *
 * Python 版用 FastAPI TestClient 调用依赖；Node 版用 Express 风格中间件 + 替身 req/res，
 * 断言同样的状态码、`WWW-Authenticate` 头与 `{"detail": ...}` 响应体。
 */

import { describe, expect, it } from "vitest";

import {
  auditActorFromPrincipal,
  createOidcMiddleware,
  extractBearerToken,
  getAuthorizationHeader,
  HumanPrincipalSchema,
  InvalidAccessTokenError,
  MissingBearerTokenError,
  MissingRoleError,
  MissingScopeError,
  oidcErrorToHttpResponse,
  OidcProviderUnavailableError,
  OidcTokenValidator,
  optionalPrincipal,
  requirePrincipal,
  requireRoles,
  requireScopes,
  type AccessTokenValidator,
  type HumanPrincipal,
  type OidcFetch,
} from "../../src/oidc/index.js";
import {
  encodeToken,
  FakeResponse,
  generateTestKey,
  nowSeconds,
  providerFetch,
  runMiddleware,
  signingJwk,
} from "./helpers.js";

const ISSUER = "https://issuer.example/realms/acps-registry";
const AUDIENCE = "registry-api";

function makeValidator(fetch: OidcFetch): OidcTokenValidator {
  return new OidcTokenValidator({
    config: {
      issuer: ISSUER,
      audience: AUDIENCE,
      allowed_azp: ["registry-web"],
      algorithms: ["EdDSA"],
      claim_mapping: { resource_client_id: AUDIENCE },
    },
    fetch,
  });
}

async function tokenAndFetch(roles: string[]): Promise<{ token: string; fetch: OidcFetch }> {
  const key = await generateTestKey("EdDSA");
  const kid = "eddsa-kid";
  const jwk = signingJwk(key, { kid, alg: "EdDSA" });
  const now = nowSeconds();
  const token = await encodeToken({
    privateKey: key.privateKey,
    algorithm: "EdDSA",
    kid,
    claims: {
      iss: ISSUER,
      sub: "user-123",
      aud: AUDIENCE,
      azp: "registry-web",
      exp: now + 300,
      iat: now,
      nbf: now,
      preferred_username: "alice",
      scope: "account:read",
      resource_access: { [AUDIENCE]: { roles } },
    },
  });
  return { token, fetch: providerFetch({ issuer: ISSUER, jwks: () => ({ keys: [jwk] }) }) };
}

const brokenFetch: OidcFetch = () => Promise.reject(new TypeError("network down"));

/** 对应 Python 测试里 `_app()` 的三个路由：中间件之后由“路由处理函数”写出 JSON。 */
async function callRoute(
  route: "principal" | "admin" | "optional",
  validator: OidcTokenValidator,
  headers: Record<string, string> = {},
) {
  const dependency =
    route === "principal"
      ? requirePrincipal(validator)
      : route === "admin"
        ? requireRoles(validator, ["ADMIN"])
        : optionalPrincipal(validator);
  const outcome = await runMiddleware(createOidcMiddleware(dependency), headers);
  if (outcome.nextCalled && outcome.nextError === undefined) {
    const principal = outcome.req.principal as HumanPrincipal | null;
    const body =
      route === "optional"
        ? { authenticated: principal !== null }
        : route === "principal"
          ? { principalId: principal?.principal_id, roles: principal?.roles }
          : { principalId: principal?.principal_id };
    outcome.res.end(JSON.stringify(body));
  }
  return outcome;
}

describe("test_fastapi_dependencies (ported)", () => {
  it("test_require_principal_returns_401_when_token_missing", async () => {
    const { fetch } = await tokenAndFetch(["USER"]);

    const { res } = await callRoute("principal", makeValidator(fetch));

    expect(res.statusCode).toBe(401);
    expect(res.headers.get("www-authenticate")).toBe("Bearer");
    expect(res.json()).toEqual({ detail: "Missing bearer token" });
  });

  it("test_require_roles_returns_403_when_role_missing", async () => {
    const { token, fetch } = await tokenAndFetch(["USER"]);

    const { res } = await callRoute("admin", makeValidator(fetch), {
      authorization: `Bearer ${token}`,
    });

    expect(res.statusCode).toBe(403);
    expect(res.body).toContain("Missing required roles");
  });

  it("test_principal_dependency_injects_authenticated_principal", async () => {
    const { token, fetch } = await tokenAndFetch(["USER", "ADMIN"]);

    const { res } = await callRoute("principal", makeValidator(fetch), {
      authorization: `Bearer ${token}`,
    });

    expect(res.statusCode).toBe(200);
    expect((res.json() as { roles: string[] }).roles).toEqual(["USER", "ADMIN"]);
  });

  it("test_optional_principal_returns_none_when_token_invalid", async () => {
    const { fetch } = await tokenAndFetch(["USER"]);

    const { res } = await callRoute("optional", makeValidator(fetch), {
      authorization: "Bearer invalid-token",
    });

    expect(res.statusCode).toBe(200);
    expect(res.json()).toEqual({ authenticated: false });
  });

  it("test_dependency_returns_503_when_discovery_or_jwks_unavailable", async () => {
    const { token } = await tokenAndFetch(["USER"]);

    const { res } = await callRoute("principal", makeValidator(brokenFetch), {
      authorization: `Bearer ${token}`,
    });

    expect(res.statusCode).toBe(503);
  });
});

describe("framework-agnostic helpers (Node additions)", () => {
  const principal = HumanPrincipalSchema.parse({
    issuer: "i",
    subject: "s",
    principal_key: "i#s",
    principal_id: "p",
    audiences: ["a"],
    roles: ["USER"],
    scopes: ["read"],
  });
  const stubValidator = (result: HumanPrincipal | Error): AccessTokenValidator => ({
    validateAccessToken: (token) => {
      expect(token).toBe("tok");
      return result instanceof Error ? Promise.reject(result) : Promise.resolve(result);
    },
  });

  it.each([
    [undefined, null],
    ["", null],
    ["Bearer tok", "tok"],
    ["bearer   tok  ", "tok"],
    ["BeArEr tok en", "tok en"],
    ["Bearer", null],
    ["Bearer ", null],
    ["Bearer\ttok", null],
    [" Bearer tok", null],
    ["Basic dXNlcjpwYXNz", null],
  ])("extractBearerToken(%j) === %j", (header, expected) => {
    expect(extractBearerToken(header)).toBe(expected);
  });

  it("reads the Authorization header from strings, Node requests and Fetch Headers", () => {
    expect(getAuthorizationHeader("Bearer x")).toBe("Bearer x");
    expect(getAuthorizationHeader(null)).toBeNull();
    expect(getAuthorizationHeader({ headers: { authorization: "Bearer a" } })).toBe("Bearer a");
    expect(getAuthorizationHeader({ headers: { Authorization: ["Bearer b", "Bearer c"] } })).toBe(
      "Bearer b",
    );
    expect(getAuthorizationHeader({ headers: new Headers({ Authorization: "Bearer d" }) })).toBe(
      "Bearer d",
    );
    expect(getAuthorizationHeader({ headers: {} })).toBeNull();
  });

  it("dependencies throw SDK error types", async () => {
    await expect(requirePrincipal(stubValidator(principal))(undefined)).rejects.toThrow(
      MissingBearerTokenError,
    );
    await expect(requirePrincipal(stubValidator(principal))("Bearer tok")).resolves.toBe(principal);
    await expect(
      requireRoles(stubValidator(principal), ["ADMIN", "", "USER", "AUDITOR", "ADMIN"])(
        "Bearer tok",
      ),
    ).rejects.toThrow(new MissingRoleError("Missing required roles: ADMIN, AUDITOR"));
    await expect(
      requireScopes(stubValidator(principal), ["write", "read"])("Bearer tok"),
    ).rejects.toThrow(new MissingScopeError("Missing required scopes: write"));
    await expect(requireScopes(stubValidator(principal), ["read"])("Bearer tok")).resolves.toBe(
      principal,
    );
  });

  it("optionalPrincipal swallows only InvalidAccessTokenError", async () => {
    await expect(optionalPrincipal(stubValidator(principal))(undefined)).resolves.toBeNull();
    await expect(
      optionalPrincipal(stubValidator(new InvalidAccessTokenError("bad")))("Bearer tok"),
    ).resolves.toBeNull();
    await expect(
      optionalPrincipal(stubValidator(new OidcProviderUnavailableError("down")))("Bearer tok"),
    ).rejects.toThrow(OidcProviderUnavailableError);
  });

  it("maps SDK errors to FastAPI-equivalent HTTP responses", () => {
    expect(oidcErrorToHttpResponse(new MissingBearerTokenError("Missing bearer token"))).toEqual({
      statusCode: 401,
      detail: "Missing bearer token",
      headers: { "WWW-Authenticate": "Bearer" },
    });
    expect(oidcErrorToHttpResponse(new InvalidAccessTokenError("Invalid issuer"))?.statusCode).toBe(
      401,
    );
    expect(oidcErrorToHttpResponse(new MissingRoleError("x"))).toEqual({
      statusCode: 403,
      detail: "x",
      headers: {},
    });
    expect(oidcErrorToHttpResponse(new OidcProviderUnavailableError("down"))?.statusCode).toBe(503);
    expect(oidcErrorToHttpResponse(new TypeError("boom"))).toBeNull();
  });

  it("middleware passes unexpected errors to next(error) and sets req.principal", async () => {
    const boom = new TypeError("boom");
    const failed = await runMiddleware(
      createOidcMiddleware(requirePrincipal(stubValidator(boom))),
      {
        authorization: "Bearer tok",
      },
    );
    expect(failed.nextCalled).toBe(true);
    expect(failed.nextError).toBe(boom);
    expect(failed.res.body).toBeNull();

    const ok = await runMiddleware(
      createOidcMiddleware(requirePrincipal(stubValidator(principal))),
      {
        authorization: "Bearer tok",
      },
    );
    expect(ok.nextCalled).toBe(true);
    expect(ok.nextError).toBeUndefined();
    expect(ok.req.principal).toBe(principal);

    const anonymous = await runMiddleware(
      createOidcMiddleware(optionalPrincipal(stubValidator(principal))),
    );
    expect(anonymous.req.principal).toBeNull();
  });

  /**
   * 运行中间件并在 50ms 内记录所有 `next` 调用，同时接住 `next` 内部抛出的未捕获异常
   * （下游处理函数同步抛错时，中间件不会吞掉它，而是让它成为普通的未捕获异常）。
   */
  async function runAndRecordNext(
    dependency: Parameters<typeof createOidcMiddleware>[0],
    onNext: (error?: unknown) => void,
  ) {
    const calls: unknown[][] = [];
    const uncaught: unknown[] = [];
    const saved = process.listeners("uncaughtException");
    process.removeAllListeners("uncaughtException");
    process.on("uncaughtException", (error) => uncaught.push(error));
    const res = new FakeResponse();
    try {
      createOidcMiddleware(dependency)(
        { headers: { authorization: "Bearer tok" } },
        res,
        (...args: unknown[]) => {
          calls.push(args);
          onNext(...(args as [unknown?]));
        },
      );
      await new Promise((resolve) => setTimeout(resolve, 50));
    } finally {
      process.removeAllListeners("uncaughtException");
      for (const listener of saved) {
        process.on("uncaughtException", listener);
      }
    }
    return { calls, uncaught, res };
  }

  it("calls next exactly once when the downstream handler throws synchronously", async () => {
    const handlerError = new Error("handler exploded");
    const { calls, uncaught } = await runAndRecordNext(
      requirePrincipal(stubValidator(principal)),
      (error) => {
        if (error === undefined) {
          throw handlerError;
        }
      },
    );

    expect(calls).toEqual([[]]);
    expect(uncaught).toEqual([handlerError]);
  });

  it("calls next(error) exactly once when the downstream error handler throws", async () => {
    const boom = new TypeError("boom");
    const { calls, uncaught } = await runAndRecordNext(
      requirePrincipal(stubValidator(boom)),
      () => {
        throw new Error("error handler exploded");
      },
    );

    expect(calls).toEqual([[boom]]);
    expect(uncaught).toHaveLength(1);
  });

  it("routes a failure to write the OIDC error response to next(error), once", async () => {
    const writeError = new Error("ERR_HTTP_HEADERS_SENT");
    const calls: unknown[][] = [];
    const res = new FakeResponse();
    res.setHeader = () => {
      throw writeError;
    };

    createOidcMiddleware(requirePrincipal(stubValidator(principal)))(
      { headers: {} },
      res,
      (...args: unknown[]) => calls.push(args),
    );
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(calls).toEqual([[writeError]]);
  });

  it("rejects a bare string for requiredRoles / requiredScopes", () => {
    // @ts-expect-error a single string would be iterated character by character
    expect(() => requireRoles(stubValidator(principal), "ADMIN")).toThrow(
      new TypeError("requiredRoles must be an iterable of strings, not a single string"),
    );
    // @ts-expect-error same for scopes
    expect(() => requireScopes(stubValidator(principal), "read")).toThrow(TypeError);
    expect(() => requireRoles(stubValidator(principal), new Set(["ADMIN"]))).not.toThrow();
  });

  it("auditActorFromPrincipal falls back name → username → email and sorts roles", () => {
    expect(
      auditActorFromPrincipal(
        HumanPrincipalSchema.parse({
          issuer: "i",
          subject: "s",
          principal_key: "i#s",
          principal_id: "p",
          audiences: [],
          email: "e@example.com",
          roles: ["b", "A", "a"],
        }),
      ),
    ).toEqual({ id: "p", type: "human", name: "e@example.com", role: "A,a,b" });
    expect(auditActorFromPrincipal(principal)).toEqual({ id: "p", type: "human", role: "USER" });
  });
});
