/**
 * 移植自 acps-sdk/tests/oidc/test_validator_jwks.py，并补充 Node 版 discovery/JWKS 缓存语义的用例。
 */

import { generateKeyPairSync, sign as nodeSign } from "node:crypto";

import { exportJWK, generateKeyPair } from "jose";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  InvalidAccessTokenError,
  OidcProviderUnavailableError,
  OidcTokenValidator,
  type OidcCacheOptions,
  type OidcFetch,
} from "../../src/oidc/index.js";
import { MAX_TIMEOUT_MS, timeoutMilliseconds } from "../../src/oidc/transport.js";
import {
  encodeToken,
  fakeFetch,
  generateTestKey,
  jsonResponse,
  nowSeconds,
  providerFetch,
  signingJwk,
  textResponse,
  type TestKey,
} from "./helpers.js";

const ISSUER = "https://issuer.example/realms/acps-leader";
const AUDIENCE = "leader-api";
const ALLOWED_AZP = ["leader-web"];
const JWKS_URI = `${ISSUER}/protocol/openid-connect/certs`;

const DROP = Symbol("drop");

function makeClaims(
  options: {
    audience?: string | string[];
    expiresDelta?: number;
    drop?: string[];
    extra?: Record<string, unknown>;
  } = {},
): Record<string, unknown> {
  const now = nowSeconds();
  const claims: Record<string, unknown> = {
    iss: ISSUER,
    sub: "user-123",
    aud: options.audience ?? AUDIENCE,
    azp: "leader-web",
    exp: now + (options.expiresDelta ?? 300),
    iat: now,
    nbf: now,
    preferred_username: "alice",
    name: "Alice",
    email: "alice@example.com",
    email_verified: true,
    scope: "session:read session:write",
    tenant_id: "tenant-1",
    allowed_aics: ["aic-1", "aic-2"],
    resource_access: { [AUDIENCE]: { roles: ["USER", "ADMIN"] } },
    realm_access: { roles: ["REALM_ADMIN"] },
    groups: ["/leaders", "/operators"],
  };
  for (const key of options.drop ?? []) {
    delete claims[key];
  }
  for (const [key, value] of Object.entries(options.extra ?? {})) {
    if (value === DROP) delete claims[key];
    else claims[key] = value;
  }
  return claims;
}

function makeValidator(options: {
  jwk?: Record<string, unknown>;
  algorithm?: string;
  allowedAlgorithms?: string[];
  fetch?: OidcFetch;
  config?: Record<string, unknown>;
  cache?: OidcCacheOptions;
}): OidcTokenValidator {
  const fetch =
    options.fetch ?? providerFetch({ issuer: ISSUER, jwks: () => ({ keys: [options.jwk] }) });
  return new OidcTokenValidator({
    config: {
      issuer: ISSUER,
      audience: AUDIENCE,
      allowed_azp: ALLOWED_AZP,
      algorithms: options.allowedAlgorithms ?? [options.algorithm ?? "EdDSA"],
      claim_mapping: { resource_client_id: AUDIENCE },
      ...options.config,
    },
    fetch,
    cache: options.cache,
  });
}

async function eddsaSetup(
  kid = "eddsa-kid",
): Promise<{ key: TestKey; jwk: Record<string, unknown> }> {
  const key = await generateTestKey("EdDSA");
  return { key, jwk: signingJwk(key, { kid, alg: "EdDSA" }) };
}

async function eddsaToken(key: TestKey, claims: Record<string, unknown>, kid = "eddsa-kid") {
  return encodeToken({ privateKey: key.privateKey, algorithm: "EdDSA", kid, claims });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("test_validator_jwks (ported)", () => {
  it("test_validator_accepts_valid_eddsa_token", async () => {
    const { key, jwk } = await eddsaSetup();
    const token = await eddsaToken(key, makeClaims());
    const validator = makeValidator({ jwk });

    const principal = await validator.validateAccessToken(token);

    expect(principal.principal_id).toBeTruthy();
    expect(principal.audiences).toEqual([AUDIENCE]);
    expect(principal.azp).toBe("leader-web");
    expect(principal.roles).toEqual(["USER", "ADMIN"]);
    expect(principal.groups).toEqual(["/leaders", "/operators"]);
    expect(principal.scopes).toEqual(["session:read", "session:write"]);
    expect(principal.allowed_aics).toEqual(["aic-1", "aic-2"]);
    await validator.close();
  });

  it.each([
    [{ iss: "https://other.example/realms/acps-leader" }, [], "Invalid issuer"],
    [{ aud: "other-api" }, [], "Audience doesn't match"],
    [{}, ["exp"], 'Token is missing the "exp" claim'],
    [{}, [], "unknown JWK kid"],
  ] as const)(
    "test_validator_rejects_basic_claim_failures %#",
    async (claimOverrides, dropClaims, message) => {
      const { key, jwk } = await eddsaSetup();
      const tokenKid = message !== "unknown JWK kid" ? "eddsa-kid" : "other-kid";
      const token = await eddsaToken(
        key,
        makeClaims({ drop: [...dropClaims], extra: claimOverrides }),
        tokenKid,
      );
      const validator = makeValidator({ jwk });

      const error = await validator.validateAccessToken(token).catch((e: unknown) => e);
      expect(error).toBeInstanceOf(InvalidAccessTokenError);
      expect((error as Error).message).toContain(message);
    },
  );

  it.each([
    [{ iat: nowSeconds() + 600 }, "The token is not yet valid"],
    [{ nbf: nowSeconds() + 600 }, "The token is not yet valid"],
    [{ exp: nowSeconds() - 60 }, "Signature has expired"],
  ])("test_validator_rejects_time_based_claim_failures %#", async (claimOverrides, message) => {
    const { key, jwk } = await eddsaSetup();
    const token = await eddsaToken(key, makeClaims({ extra: claimOverrides }));
    const validator = makeValidator({ jwk });

    await expect(validator.validateAccessToken(token)).rejects.toThrow(InvalidAccessTokenError);
    await expect(validator.validateAccessToken(token)).rejects.toThrow(message);
  });

  it("test_validator_rejects_wrong_authorized_party", async () => {
    const { key, jwk } = await eddsaSetup();
    const token = await eddsaToken(key, makeClaims({ extra: { azp: "other-web" } }));
    const validator = makeValidator({ jwk });

    await expect(validator.validateAccessToken(token)).rejects.toThrow("azp must be one of");
  });

  it.each([[AUDIENCE], [[AUDIENCE, "account"]]])(
    "test_validator_accepts_string_and_array_audiences %#",
    async (audience) => {
      const { key, jwk } = await eddsaSetup();
      const token = await eddsaToken(key, makeClaims({ audience }));
      const validator = makeValidator({ jwk });

      const principal = await validator.validateAccessToken(token);

      expect(principal.audiences).toEqual(
        typeof audience === "string" ? [AUDIENCE] : [AUDIENCE, "account"],
      );
    },
  );

  it("test_validator_rejects_id_token_style_bearer_token", async () => {
    const { key, jwk } = await eddsaSetup();
    const token = await eddsaToken(key, makeClaims({ audience: "leader-web" }));
    const validator = makeValidator({ jwk });

    await expect(validator.validateAccessToken(token)).rejects.toThrow("Audience doesn't match");
  });

  it("test_validator_rejects_disallowed_header_algorithm", async () => {
    const { key, jwk } = await eddsaSetup();
    const token = await eddsaToken(key, makeClaims());
    const validator = makeValidator({ jwk, allowedAlgorithms: ["ES256"] });

    await expect(validator.validateAccessToken(token)).rejects.toThrow("is not allowed");
  });

  it.each([
    [{ kty: "EC" }, "EdDSA access tokens require OKP/Ed25519 JWKs"],
    [{ crv: "Ed448" }, "EdDSA access tokens require OKP/Ed25519 JWKs"],
    [{ alg: "ES256" }, "JWK alg mismatch"],
    [{ use: "enc" }, "JWK use mismatch"],
  ])("test_validator_rejects_jwk_metadata_mismatches %#", async (jwkPatch, message) => {
    const { key, jwk } = await eddsaSetup();
    const token = await eddsaToken(key, makeClaims());
    const validator = makeValidator({ jwk: { ...jwk, ...jwkPatch } });

    const error = await validator.validateAccessToken(token).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(InvalidAccessTokenError);
    expect((error as Error).message).toContain(message);
  });

  it("test_validator_accepts_jwk_without_optional_alg_and_use", async () => {
    const key = await generateTestKey("EdDSA");
    const jwk = signingJwk(key, { kid: "eddsa-kid", alg: null });
    delete jwk.use;
    const token = await eddsaToken(key, makeClaims());
    const validator = makeValidator({ jwk });

    const principal = await validator.validateAccessToken(token);

    expect(principal.username).toBe("alice");
  });

  it.each(["ES256", "PS256", "RS256"] as const)(
    "test_validator_supports_explicit_fallback_algorithms %s",
    async (algorithm) => {
      const key = await generateTestKey(algorithm);
      const kid = `${algorithm.toLowerCase()}-kid`;
      const jwk = signingJwk(key, { kid, alg: algorithm });
      const token = await encodeToken({
        privateKey: key.privateKey,
        algorithm,
        kid,
        claims: makeClaims(),
      });
      const validator = makeValidator({ algorithm, jwk });

      const principal = await validator.validateAccessToken(token);

      expect(principal.email).toBe("alice@example.com");
    },
  );

  it("test_validator_rejects_invalid_discovery_json", async () => {
    const { key, jwk } = await eddsaSetup();
    const token = await eddsaToken(key, makeClaims());
    const fetch = fakeFetch((request) => {
      if (request.path.endsWith("/.well-known/openid-configuration")) {
        return textResponse(200, "{broken-json");
      }
      if (request.path.endsWith("/protocol/openid-connect/certs")) {
        return jsonResponse(200, { keys: [jwk] });
      }
      return jsonResponse(404, { detail: "not found" });
    });
    const validator = makeValidator({ fetch });

    const error = await validator.validateAccessToken(token).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(OidcProviderUnavailableError);
    expect((error as Error).message).toContain("is not valid JSON");
  });

  it("test_validator_rejects_invalid_jwks_json", async () => {
    const { key } = await eddsaSetup();
    const token = await eddsaToken(key, makeClaims());
    const fetch = fakeFetch((request) => {
      if (request.path.endsWith("/.well-known/openid-configuration")) {
        return jsonResponse(200, { issuer: ISSUER, jwks_uri: JWKS_URI });
      }
      if (request.path.endsWith("/protocol/openid-connect/certs")) {
        return textResponse(200, "{broken-json");
      }
      return jsonResponse(404, { detail: "not found" });
    });
    const validator = makeValidator({ fetch });

    const error = await validator.validateAccessToken(token).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(OidcProviderUnavailableError);
    expect((error as Error).message).toContain("is not valid JSON");
  });
});

/** 可控的单调时钟（替换 `performance.now`，缓存 TTL / 冷却 / 失败缓存都基于它）。 */
function useMonotonicClock(startSeconds = 1_000) {
  let nowMs = startSeconds * 1000;
  vi.spyOn(performance, "now").mockImplementation(() => nowMs);
  return {
    advance(seconds: number) {
      nowMs += seconds * 1000;
    },
  };
}

function countingProvider(options: {
  jwks: Array<Record<string, unknown>> | (() => Response | Promise<Response>);
  discovery?: () => Response | Promise<Response>;
}) {
  let jwksCalls = 0;
  const fetch = fakeFetch((request) => {
    if (request.path.endsWith("/.well-known/openid-configuration")) {
      return options.discovery?.() ?? jsonResponse(200, { issuer: ISSUER, jwks_uri: JWKS_URI });
    }
    const { jwks } = options;
    if (typeof jwks === "function") {
      jwksCalls += 1;
      return jwks();
    }
    const document = jwks[Math.min(jwksCalls, jwks.length - 1)];
    jwksCalls += 1;
    return jsonResponse(200, document);
  });
  const count = (suffix: string) => fetch.requests.filter((r) => r.path.endsWith(suffix)).length;
  return {
    fetch,
    discoveryCalls: () => count("/.well-known/openid-configuration"),
    jwksCalls: () => count("/certs"),
  };
}

const failedJwksMessage = `failed to load JWKS from ${JWKS_URI}`;
const failedDiscoveryMessage = `failed to load OIDC discovery document from ${ISSUER}/.well-known/openid-configuration`;

describe("discovery / JWKS cache: Python-compatible semantics", () => {
  it("caches discovery and JWKS documents across validations", async () => {
    const { key, jwk } = await eddsaSetup();
    const provider = countingProvider({ jwks: [{ keys: [jwk] }] });
    const validator = makeValidator({ fetch: provider.fetch });

    await validator.validateAccessToken(await eddsaToken(key, makeClaims()));
    await validator.validateAccessToken(await eddsaToken(key, makeClaims()));

    expect(provider.discoveryCalls()).toBe(1);
    expect(provider.jwksCalls()).toBe(1);
  });

  it("re-fetches after the TTL expires (monotonic clock)", async () => {
    const clock = useMonotonicClock();
    const { key, jwk } = await eddsaSetup();
    const provider = countingProvider({ jwks: [{ keys: [jwk] }] });
    const validator = makeValidator({
      fetch: provider.fetch,
      config: { jwks_cache_ttl_seconds: 60, discovery_cache_ttl_seconds: 120 },
    });
    const token = await eddsaToken(key, makeClaims());

    await validator.validateAccessToken(token);
    clock.advance(60); // expires_at >= now 仍视为有效
    await validator.validateAccessToken(token);
    expect([provider.discoveryCalls(), provider.jwksCalls()]).toEqual([1, 1]);

    clock.advance(0.001);
    await validator.validateAccessToken(token);
    expect([provider.discoveryCalls(), provider.jwksCalls()]).toEqual([1, 2]);

    clock.advance(60.001);
    await validator.validateAccessToken(token);
    expect([provider.discoveryCalls(), provider.jwksCalls()]).toEqual([2, 3]);
  });

  it("coalesces concurrent cache misses into a single load", async () => {
    const { key, jwk } = await eddsaSetup();
    const provider = countingProvider({ jwks: [{ keys: [jwk] }] });
    const validator = makeValidator({ fetch: provider.fetch });
    const token = await eddsaToken(key, makeClaims());

    await Promise.all([1, 2, 3, 4].map(() => validator.validateAccessToken(token)));

    expect(provider.discoveryCalls()).toBe(1);
    expect(provider.jwksCalls()).toBe(1);
  });

  it("with every hardening switched off, an unknown kid refetches JWKS once per request (Python)", async () => {
    const { key, jwk } = await eddsaSetup();
    const provider = countingProvider({ jwks: [{ keys: [jwk] }] });
    const validator = makeValidator({
      fetch: provider.fetch,
      cache: { maxStaleSeconds: 0, failureCacheSeconds: 0, jwksRefreshCooldownSeconds: 0 },
    });
    const token = await eddsaToken(key, makeClaims(), "missing-kid");

    await expect(validator.validateAccessToken(token)).rejects.toThrow(
      "unknown JWK kid: missing-kid",
    );
    expect(provider.jwksCalls()).toBe(2);
    await expect(validator.validateAccessToken(token)).rejects.toThrow("unknown JWK kid");
    expect(provider.jwksCalls()).toBe(3);
  });

  it.each([
    ["network error", () => Promise.reject(new TypeError("fetch failed"))],
    ["HTTP 503", () => Promise.resolve(jsonResponse(503, {}))],
    [
      "redirect (httpx does not follow redirects)",
      () =>
        Promise.resolve(
          new Response(null, { status: 302, headers: { Location: "https://elsewhere.example" } }),
        ),
    ],
  ])("maps discovery %s to OidcProviderUnavailableError", async (_label, respond) => {
    const { key } = await eddsaSetup();
    const validator = makeValidator({ fetch: () => respond() });

    const error = await validator
      .validateAccessToken(await eddsaToken(key, makeClaims()))
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(OidcProviderUnavailableError);
    expect((error as Error).message).toBe(failedDiscoveryMessage);
  });

  it.each([
    [
      { issuer: "https://other.example", jwks_uri: JWKS_URI },
      `OIDC discovery issuer mismatch: expected ${ISSUER}, got https://other.example`,
    ],
    [
      { issuer: `${ISSUER}///`, jwks_uri: "http://insecure.example/certs" },
      "jwks_uri must use https when require_https=true",
    ],
    [
      { issuer: ISSUER },
      `OIDC discovery document at ${ISSUER}/.well-known/openid-configuration is invalid`,
    ],
    [[ISSUER], `OIDC discovery document at ${ISSUER}/.well-known/openid-configuration is invalid`],
  ])("rejects invalid discovery documents %#", async (discovery, message) => {
    const { key } = await eddsaSetup();
    const fetch = fakeFetch(() => jsonResponse(200, discovery));
    const validator = makeValidator({ fetch });

    const error = await validator
      .validateAccessToken(await eddsaToken(key, makeClaims()))
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(OidcProviderUnavailableError);
    expect((error as Error).message).toBe(message);
  });

  it.each([
    [["not", "an", "object"], "JWKS response must be a JSON object"],
    [{ keys: "nope" }, "JWKS response must contain a keys array"],
  ])("rejects malformed JWKS documents %#", async (jwks, message) => {
    const { key } = await eddsaSetup();
    const fetch = providerFetch({ issuer: ISSUER, jwks: () => jwks as never });
    const validator = makeValidator({ fetch });

    await expect(
      validator.validateAccessToken(await eddsaToken(key, makeClaims())),
    ).rejects.toThrow(message);
  });

  it("rejects a non-https issuer unless require_https is disabled", async () => {
    const issuer = "http://127.0.0.1:8080/realms/acps-leader";
    const { key, jwk } = await eddsaSetup();
    const fetch = providerFetch({ issuer, jwks: () => ({ keys: [jwk] }) });
    const token = await eddsaToken(key, makeClaims({ extra: { iss: issuer } }));

    const strict = makeValidator({ fetch, config: { issuer } });
    await expect(strict.validateAccessToken(token)).rejects.toThrow(
      "issuer must use https when require_https=true",
    );
    expect(fetch.requests).toHaveLength(0);

    const relaxed = makeValidator({ fetch, config: { issuer, require_https: false } });
    await expect(relaxed.validateAccessToken(token)).resolves.toBeTruthy();
  });
});

describe("JWKS forced refresh: coalescing and cooldown (Node hardening)", () => {
  it("does not refetch for an unknown kid right after loading the JWKS", async () => {
    const { key, jwk } = await eddsaSetup();
    const provider = countingProvider({ jwks: [{ keys: [jwk] }] });
    const validator = makeValidator({ fetch: provider.fetch });

    await expect(
      validator.validateAccessToken(await eddsaToken(key, makeClaims(), "missing-kid")),
    ).rejects.toThrow("unknown JWK kid: missing-kid");
    expect(provider.jwksCalls()).toBe(1);
  });

  it("lets N concurrent bad-kid tokens trigger at most one fetch per cooldown", async () => {
    const clock = useMonotonicClock();
    const { key, jwk } = await eddsaSetup();
    const provider = countingProvider({ jwks: [{ keys: [jwk] }] });
    const validator = makeValidator({ fetch: provider.fetch });
    await validator.validateAccessToken(await eddsaToken(key, makeClaims()));
    expect(provider.jwksCalls()).toBe(1);
    const badTokens = await Promise.all(
      Array.from({ length: 10 }, (_, i) => eddsaToken(key, makeClaims(), `forged-${i}`)),
    );
    const validateAll = () =>
      Promise.all(badTokens.map((token) => validator.validateAccessToken(token).catch((e) => e)));

    // 冷却期内：一个都不拉
    clock.advance(29);
    for (const error of await validateAll()) {
      expect(error).toBeInstanceOf(InvalidAccessTokenError);
    }
    expect(provider.jwksCalls()).toBe(1);

    // 冷却期过后：10 个并发请求只触发一次刷新，其余合并到这次刷新
    clock.advance(2);
    for (const error of await validateAll()) {
      expect((error as Error).message).toMatch(/^unknown JWK kid: forged-/);
    }
    expect(provider.jwksCalls()).toBe(2);

    // 刚刷新过：再来一批也不拉
    await validateAll();
    expect(provider.jwksCalls()).toBe(2);
  });

  it("coalesces concurrent forced refreshes even with the cooldown disabled", async () => {
    const { key, jwk } = await eddsaSetup();
    const provider = countingProvider({ jwks: [{ keys: [jwk] }] });
    const validator = makeValidator({
      fetch: provider.fetch,
      cache: { jwksRefreshCooldownSeconds: 0 },
    });
    await validator.validateAccessToken(await eddsaToken(key, makeClaims()));
    const badToken = await eddsaToken(key, makeClaims(), "forged");

    await Promise.all(
      [1, 2, 3, 4, 5].map(() => validator.validateAccessToken(badToken).catch((e) => e)),
    );

    expect(provider.jwksCalls()).toBe(2);
  });

  it("picks up a rotated key once the cooldown has passed", async () => {
    const clock = useMonotonicClock();
    const { key: oldKey, jwk: oldJwk } = await eddsaSetup("old-kid");
    const { key: newKey, jwk: newJwk } = await eddsaSetup("new-kid");
    const provider = countingProvider({ jwks: [{ keys: [oldJwk] }, { keys: [oldJwk, newJwk] }] });
    const validator = makeValidator({ fetch: provider.fetch });
    const newToken = await eddsaToken(newKey, makeClaims(), "new-kid");

    await validator.validateAccessToken(await eddsaToken(oldKey, makeClaims(), "old-kid"));
    clock.advance(1);
    await expect(validator.validateAccessToken(newToken)).rejects.toThrow(
      "unknown JWK kid: new-kid",
    );
    expect(provider.jwksCalls()).toBe(1);

    clock.advance(30);
    const principal = await validator.validateAccessToken(newToken);
    expect(principal.username).toBe("alice");
    expect(provider.jwksCalls()).toBe(2);
  });

  it('applies the cooldown to {"keys": []} responses too', async () => {
    const clock = useMonotonicClock();
    const { key } = await eddsaSetup();
    const provider = countingProvider({ jwks: [{ keys: [] }] });
    const validator = makeValidator({ fetch: provider.fetch });
    const token = await eddsaToken(key, makeClaims());

    for (let i = 0; i < 5; i += 1) {
      await expect(validator.validateAccessToken(token)).rejects.toThrow("unknown JWK kid");
      clock.advance(5);
    }
    expect(provider.jwksCalls()).toBe(1);

    clock.advance(10);
    await expect(validator.validateAccessToken(token)).rejects.toThrow("unknown JWK kid");
    expect(provider.jwksCalls()).toBe(2);
  });

  it("surfaces a failed forced refresh as provider unavailable, keeping the cached keys usable", async () => {
    const clock = useMonotonicClock();
    const { key, jwk } = await eddsaSetup();
    let failJwks = false;
    const provider = countingProvider({
      jwks: () => (failJwks ? jsonResponse(500, {}) : jsonResponse(200, { keys: [jwk] })),
    });
    const validator = makeValidator({ fetch: provider.fetch });
    const goodToken = await eddsaToken(key, makeClaims());
    await validator.validateAccessToken(goodToken);

    clock.advance(31);
    failJwks = true;
    await expect(
      validator.validateAccessToken(await eddsaToken(key, makeClaims(), "new-kid")),
    ).rejects.toThrow(new OidcProviderUnavailableError(failedJwksMessage));
    await expect(validator.validateAccessToken(goodToken)).resolves.toBeTruthy();
    expect(provider.jwksCalls()).toBe(2);
  });
});

describe("failure caching and stale fallback (Node hardening)", () => {
  it("caches a failed load briefly, then retries", async () => {
    const clock = useMonotonicClock();
    const { key, jwk } = await eddsaSetup();
    let failDiscovery = true;
    const provider = countingProvider({
      jwks: [{ keys: [jwk] }],
      discovery: () =>
        failDiscovery
          ? jsonResponse(500, { error: "boom" })
          : jsonResponse(200, { issuer: ISSUER, jwks_uri: JWKS_URI }),
    });
    const validator = makeValidator({ fetch: provider.fetch });
    const token = await eddsaToken(key, makeClaims());

    await expect(validator.validateAccessToken(token)).rejects.toThrow(failedDiscoveryMessage);
    failDiscovery = false;
    clock.advance(4.9);
    await expect(validator.validateAccessToken(token)).rejects.toThrow(failedDiscoveryMessage);
    expect(provider.discoveryCalls()).toBe(1);

    clock.advance(0.2);
    await expect(validator.validateAccessToken(token)).resolves.toBeTruthy();
    expect(provider.discoveryCalls()).toBe(2);
  });

  it("does not make queued requests refetch a stalled IdP one by one", async () => {
    const { key } = await eddsaSetup();
    const provider = countingProvider({
      jwks: () =>
        new Promise<Response>((_resolve, reject) => {
          setTimeout(() => reject(new DOMException("timed out", "TimeoutError")), 20);
        }),
    });
    const validator = makeValidator({ fetch: provider.fetch });
    const token = await eddsaToken(key, makeClaims());

    const errors = await Promise.all(
      Array.from({ length: 8 }, () => validator.validateAccessToken(token).catch((e) => e)),
    );

    for (const error of errors) {
      expect(error).toBeInstanceOf(OidcProviderUnavailableError);
      expect((error as Error).message).toBe(failedJwksMessage);
    }
    expect(provider.jwksCalls()).toBe(1);
  });

  it("keeps using an expired JWKS while the IdP is down, up to maxStaleSeconds", async () => {
    const clock = useMonotonicClock();
    const { key, jwk } = await eddsaSetup();
    let failJwks = false;
    const provider = countingProvider({
      jwks: () => (failJwks ? jsonResponse(503, {}) : jsonResponse(200, { keys: [jwk] })),
    });
    const validator = makeValidator({
      fetch: provider.fetch,
      config: { jwks_cache_ttl_seconds: 60, discovery_cache_ttl_seconds: 100_000 },
    });
    const token = await eddsaToken(key, makeClaims());
    await validator.validateAccessToken(token);
    failJwks = true;

    // 过期 1 秒：刷新失败，继续用旧 key
    clock.advance(61);
    await expect(validator.validateAccessToken(token)).resolves.toBeTruthy();
    expect(provider.jwksCalls()).toBe(2);
    // 失败缓存期内不再请求
    clock.advance(4);
    await expect(validator.validateAccessToken(token)).resolves.toBeTruthy();
    expect(provider.jwksCalls()).toBe(2);
    // 失败缓存期过后再试一次，仍然失败，仍然兜底
    clock.advance(2);
    await expect(validator.validateAccessToken(token)).resolves.toBeTruthy();
    expect(provider.jwksCalls()).toBe(3);

    // 默认 maxStaleSeconds = 10 × TTL = 600：过期 600 秒仍可用，超过后抛出
    clock.advance(60 + 600 - 67);
    await expect(validator.validateAccessToken(token)).resolves.toBeTruthy();
    clock.advance(0.5);
    await expect(validator.validateAccessToken(token)).rejects.toThrow(
      new OidcProviderUnavailableError(failedJwksMessage),
    );

    // IdP 恢复后照常加载
    failJwks = false;
    clock.advance(5);
    await expect(validator.validateAccessToken(token)).resolves.toBeTruthy();
  });

  it("honours maxStaleSeconds: 0 (no stale fallback, like Python)", async () => {
    const clock = useMonotonicClock();
    const { key, jwk } = await eddsaSetup();
    let failJwks = false;
    const provider = countingProvider({
      jwks: () => (failJwks ? jsonResponse(503, {}) : jsonResponse(200, { keys: [jwk] })),
    });
    const validator = makeValidator({
      fetch: provider.fetch,
      config: { jwks_cache_ttl_seconds: 60 },
      cache: { maxStaleSeconds: 0 },
    });
    const token = await eddsaToken(key, makeClaims());
    await validator.validateAccessToken(token);

    failJwks = true;
    clock.advance(61);
    await expect(validator.validateAccessToken(token)).rejects.toThrow(failedJwksMessage);
  });

  it("keeps using an expired discovery document while the IdP is down", async () => {
    const clock = useMonotonicClock();
    const { key, jwk } = await eddsaSetup();
    let failDiscovery = false;
    const provider = countingProvider({
      jwks: [{ keys: [jwk] }],
      discovery: () =>
        failDiscovery
          ? jsonResponse(503, {})
          : jsonResponse(200, { issuer: ISSUER, jwks_uri: JWKS_URI }),
    });
    const validator = makeValidator({
      fetch: provider.fetch,
      config: { discovery_cache_ttl_seconds: 60 },
    });
    const token = await eddsaToken(key, makeClaims());
    await validator.validateAccessToken(token);

    failDiscovery = true;
    clock.advance(61);
    await expect(validator.validateAccessToken(token)).resolves.toBeTruthy();
    expect(provider.discoveryCalls()).toBe(2);
  });
});

describe("HTTP timeout", () => {
  function captureTimeouts(): number[] {
    const delays: number[] = [];
    const original = AbortSignal.timeout.bind(AbortSignal);
    vi.spyOn(AbortSignal, "timeout").mockImplementation((delay: number) => {
      delays.push(delay);
      return original(delay);
    });
    return delays;
  }

  it.each([
    [undefined, 5_000],
    [2.5, 2_500],
    [1.1, 1_100],
    [0.0001, 1],
    [1e9, 2_147_483_647],
    [1e12, 2_147_483_647],
  ])(
    "http_timeout_seconds=%s → AbortSignal.timeout(%i) on every request",
    async (seconds, expected) => {
      const delays = captureTimeouts();
      const warnings: Error[] = [];
      const onWarning = (warning: Error) => warnings.push(warning);
      process.on("warning", onWarning);
      const { key, jwk } = await eddsaSetup();
      const inner = providerFetch({ issuer: ISSUER, jwks: () => ({ keys: [jwk] }) });
      const signals: Array<AbortSignal | null | undefined> = [];
      const validator = makeValidator({
        fetch: (input, init) => {
          signals.push(init?.signal);
          expect(init?.redirect).toBe("manual");
          return inner(input, init);
        },
        config: seconds === undefined ? {} : { http_timeout_seconds: seconds },
      });

      try {
        await validator.validateAccessToken(await eddsaToken(key, makeClaims()));
        await new Promise((resolve) => setTimeout(resolve, 10));
      } finally {
        process.off("warning", onWarning);
      }

      expect(delays).toEqual([expected, expected]);
      expect(signals).toHaveLength(2);
      expect(warnings.filter((w) => w.name === "TimeoutOverflowWarning")).toEqual([]);
      if (expected > 1) {
        expect(signals.every((signal) => signal?.aborted === false)).toBe(true);
      }
    },
  );

  it("clamps timeoutMilliseconds to Node's timer range", () => {
    expect(timeoutMilliseconds(5)).toBe(5_000);
    expect(timeoutMilliseconds(Number.POSITIVE_INFINITY)).toBe(MAX_TIMEOUT_MS);
    expect(timeoutMilliseconds(2_147_483.647)).toBe(MAX_TIMEOUT_MS);
    expect(timeoutMilliseconds(2_147_484)).toBe(MAX_TIMEOUT_MS);
    expect(Number.isInteger(timeoutMilliseconds(0.30000000000000004))).toBe(true);
  });
});

describe("leeway end-to-end (fake Date)", () => {
  const NOW = 1_900_000_000;

  afterEach(() => {
    vi.useRealTimers();
  });

  it.each([
    ["exp = now - leeway + 1 is accepted", { exp: NOW - 29 }, null],
    ["exp = now - leeway is expired", { exp: NOW - 30 }, "Signature has expired"],
    ["exp = now - leeway - 1 is expired", { exp: NOW - 31 }, "Signature has expired"],
    ["iat = now + leeway is accepted", { iat: NOW + 30 }, null],
    [
      "iat = now + leeway + 1 is not yet valid",
      { iat: NOW + 31 },
      "The token is not yet valid (iat)",
    ],
    ["nbf = now + leeway is accepted", { nbf: NOW + 30 }, null],
    [
      "nbf = now + leeway + 1 is not yet valid",
      { nbf: NOW + 31 },
      "The token is not yet valid (nbf)",
    ],
  ])("%s (leeway_seconds=30)", async (_label, overrides, message) => {
    vi.useFakeTimers({ toFake: ["Date"], now: NOW * 1000 });
    const { key, jwk } = await eddsaSetup();
    const claims = { ...makeClaims(), iat: NOW, nbf: NOW, exp: NOW + 300, ...overrides };
    const validator = makeValidator({ jwk, config: { leeway_seconds: 30 } });

    const outcome = validator.validateAccessToken(await eddsaToken(key, claims));

    if (message === null) {
      await expect(outcome).resolves.toBeTruthy();
    } else {
      await expect(outcome).rejects.toThrow(new InvalidAccessTokenError(message));
    }
  });

  it("uses leeway_seconds from the config (leeway 0 rejects exp = now - 1)", async () => {
    vi.useFakeTimers({ toFake: ["Date"], now: NOW * 1000 });
    const { key, jwk } = await eddsaSetup();
    const token = await eddsaToken(key, { ...makeClaims(), iat: NOW, nbf: NOW, exp: NOW - 1 });

    await expect(
      makeValidator({ jwk, config: { leeway_seconds: 0 } }).validateAccessToken(token),
    ).rejects.toThrow("Signature has expired");
    await expect(
      makeValidator({ jwk, config: { leeway_seconds: 2 } }).validateAccessToken(token),
    ).resolves.toBeTruthy();
  });
});

describe("Node-specific behaviour", () => {
  it("validates the config like OidcProviderConfig(...)", () => {
    expect(() => makeValidator({ config: { issuer: " / " } })).toThrow("issuer must not be empty");
  });

  it("verifies with a JWKS entry that also carries private parameters (PyJWK accepts it)", async () => {
    const { privateKey } = await generateKeyPair("EdDSA", { crv: "Ed25519", extractable: true });
    const privateJwk = await exportJWK(privateKey);
    const jwk = { ...privateJwk, kid: "eddsa-kid", alg: "EdDSA", key_ops: ["sign"] };
    expect(jwk).toHaveProperty("d");
    const token = await encodeToken({
      privateKey,
      algorithm: "EdDSA",
      kid: "eddsa-kid",
      claims: makeClaims(),
    });
    const validator = makeValidator({ jwk });

    const principal = await validator.validateAccessToken(token);

    expect(principal.username).toBe("alice");
  });

  it("rejects RSA keys shorter than 2048 bits (jose minimum; PyJWT only warns)", async () => {
    const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 1024 });
    const jwk = {
      ...publicKey.export({ format: "jwk" }),
      kid: "rsa-1024",
      alg: "RS256",
      use: "sig",
    };
    const b64 = (value: unknown) => Buffer.from(JSON.stringify(value)).toString("base64url");
    const signingInput = `${b64({ alg: "RS256", kid: "rsa-1024", typ: "JWT" })}.${b64(makeClaims())}`;
    const signature = nodeSign("sha256", Buffer.from(signingInput), privateKey).toString(
      "base64url",
    );
    const validator = makeValidator({ algorithm: "RS256", jwk });

    const error = await validator
      .validateAccessToken(`${signingInput}.${signature}`)
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(InvalidAccessTokenError);
    expect((error as Error).message).toMatch(/2048/);
  });

  it("propagates Python's raw TypeError for non-numeric iat (PyJWT does not catch it)", async () => {
    const { key, jwk } = await eddsaSetup();
    const token = await eddsaToken(key, makeClaims({ extra: { iat: null } }));
    const validator = makeValidator({ jwk });

    const error = await validator.validateAccessToken(token).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(TypeError);
    expect(error).not.toBeInstanceOf(InvalidAccessTokenError);
  });
});
