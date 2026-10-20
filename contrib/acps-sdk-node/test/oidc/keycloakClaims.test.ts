/**
 * 移植自 acps-sdk/tests/oidc/test_keycloak_claims.py。
 */

import { describe, expect, it } from "vitest";

import {
  claimsToPrincipal,
  KeycloakClaimMappingSchema,
  normalizeAudiences,
  parseMultiValueClaim,
  parseScopeClaim,
} from "../../src/oidc/index.js";

function claims(): Record<string, unknown> {
  return {
    iss: "https://issuer.example/realms/acps-monitor",
    sub: "user-123",
    aud: ["monitor-api", "account"],
    azp: "monitor-web",
    preferred_username: "alice",
    name: "Alice",
    email: "alice@example.com",
    email_verified: true,
    tenant_id: "tenant-1",
    allowed_aics: ["aic-1", "aic-2"],
    scope: "metrics:read system:query",
    groups: ["/ops", "/monitor"],
    resource_access: { "monitor-api": { roles: ["AUDITOR", "SYSTEM_READER"] } },
    realm_access: { roles: ["REALM_ADMIN"] },
  };
}

describe("test_keycloak_claims (ported)", () => {
  it("test_claim_parser_reads_client_roles_groups_and_scopes_by_default", () => {
    const principal = claimsToPrincipal({
      claims: claims(),
      claimMapping: KeycloakClaimMappingSchema.parse({ resource_client_id: "monitor-api" }),
    });

    expect(principal.roles).toEqual(["AUDITOR", "SYSTEM_READER"]);
    expect(principal.groups).toEqual(["/ops", "/monitor"]);
    expect(principal.scopes).toEqual(["metrics:read", "system:query"]);
    expect(principal.tenant_id).toBe("tenant-1");
    expect(principal.allowed_aics).toEqual(["aic-1", "aic-2"]);
  });

  it("test_claim_parser_ignores_realm_roles_unless_enabled", () => {
    const principal = claimsToPrincipal({
      claims: claims(),
      claimMapping: KeycloakClaimMappingSchema.parse({ resource_client_id: "monitor-api" }),
    });

    expect(principal.roles).not.toContain("REALM_ADMIN");
  });

  it("test_claim_parser_reads_realm_roles_when_enabled", () => {
    const principal = claimsToPrincipal({
      claims: claims(),
      claimMapping: KeycloakClaimMappingSchema.parse({
        resource_client_id: "monitor-api",
        read_realm_roles: true,
      }),
    });

    expect(principal.roles).toEqual(["AUDITOR", "SYSTEM_READER", "REALM_ADMIN"]);
  });

  it("test_parse_scope_claim_uses_oauth_space_delimiter", () => {
    expect(parseScopeClaim("session:read session:write")).toEqual([
      "session:read",
      "session:write",
    ]);
  });

  it("test_normalize_audiences_rejects_non_string_items", () => {
    expect(() => normalizeAudiences(["monitor-api", 123])).toThrow(TypeError);
    expect(() => normalizeAudiences(["monitor-api", 123])).toThrow(
      "aud claim values must be strings",
    );
  });

  it("test_parse_multi_value_claim_rejects_non_string_items", () => {
    expect(() => parseMultiValueClaim(["aic-1", 2])).toThrow(TypeError);
    expect(() => parseMultiValueClaim(["aic-1", 2])).toThrow("claim values must be strings");
  });
});

describe("claim helpers (Node additions)", () => {
  it("accept non-array iterables like Python's Iterable, but not mappings or bytes", () => {
    expect(normalizeAudiences(new Set(["a", "b", "a"]))).toEqual(["a", "b"]);
    expect(parseMultiValueClaim(new Set([" x ", "y"]))).toEqual(["x", "y"]);
    expect(() => parseMultiValueClaim(new Map([["a", "b"]]))).toThrow(
      "claim must be a string or iterable of strings",
    );
    expect(() => normalizeAudiences(Buffer.from("aud"))).toThrow(
      "aud claim must be a string or array of strings",
    );
  });

  it("drops an own __proto__ claim from raw_claims without touching the prototype", () => {
    // Python 的 dict 会原样保留 "__proto__" 键；zod 的 record 解析出于安全会丢弃它（已知差异）
    const claims = JSON.parse(
      '{"iss":"i","sub":"s","aud":"a","__proto__":{"polluted":true}}',
    ) as Record<string, unknown>;
    const principal = claimsToPrincipal({
      claims,
      claimMapping: KeycloakClaimMappingSchema.parse({}),
    });

    expect(Object.keys(principal.raw_claims)).toEqual(["iss", "sub", "aud"]);
    expect(Object.getPrototypeOf(principal.raw_claims)).toBe(Object.prototype);
    expect(({} as Record<string, unknown>).polluted).toBeUndefined();
  });

  it("read only own properties of the claims object", () => {
    const principal = claimsToPrincipal({
      claims: { iss: "i", sub: "s", aud: "a" },
      claimMapping: KeycloakClaimMappingSchema.parse({
        username_claims: ["constructor"],
        name_claims: ["toString"],
      }),
    });

    expect(principal.username).toBeUndefined();
    expect(principal.name).toBeUndefined();
  });
});
