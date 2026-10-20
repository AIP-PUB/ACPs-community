/**
 * 移植自 acps-sdk/tests/oidc/test_principal.py。
 */

import { inspect } from "node:util";

import { describe, expect, it } from "vitest";
import { ZodError } from "zod";

import {
  auditActorFromPrincipal,
  buildPrincipalId,
  buildPrincipalKey,
  canonicalJsonBytes,
  HumanPrincipal,
  HumanPrincipalSchema,
} from "../../src/oidc/index.js";

describe("test_principal (ported)", () => {
  it("test_principal_id_uses_structured_hash_input", () => {
    const first = buildPrincipalId({ issuer: "issuer#a", subject: "subject" });
    const second = buildPrincipalId({ issuer: "issuer", subject: "a#subject" });

    expect(first).not.toBe(second);
  });

  it("test_canonical_json_bytes_sorts_mapping_keys", () => {
    const first = canonicalJsonBytes({ b: 2, a: 1 });
    const second = canonicalJsonBytes({ a: 1, b: 2 });

    expect(first.equals(second)).toBe(true);
  });

  it("test_sensitive_principal_fields_are_excluded_from_serialization", () => {
    const principal = HumanPrincipalSchema.parse({
      issuer: "https://issuer.example/realms/acps-leader",
      subject: "raw-subject",
      principal_key: buildPrincipalKey({
        issuer: "https://issuer.example/realms/acps-leader",
        subject: "raw-subject",
      }),
      principal_id: "principal-id",
      audiences: ["leader-api"],
      roles: ["ADMIN"],
      scopes: ["session:write"],
      raw_claims: { sub: "raw-subject", token: "secret" },
    });

    // model_dump()
    const data = JSON.parse(JSON.stringify(principal)) as Record<string, unknown>;

    expect(data).not.toHaveProperty("subject");
    expect(data).not.toHaveProperty("principal_key");
    expect(data).not.toHaveProperty("raw_claims");
    // repr()
    expect(inspect(principal)).not.toContain("raw-subject");
    expect(inspect(principal)).not.toContain("secret");
    // 仍可直接读取
    expect(principal.subject).toBe("raw-subject");
    expect(principal.raw_claims).toEqual({ sub: "raw-subject", token: "secret" });
  });

  it("test_audit_actor_from_principal_does_not_leak_sensitive_fields", () => {
    const principal = HumanPrincipalSchema.parse({
      issuer: "https://issuer.example/realms/acps-leader",
      subject: "raw-subject",
      principal_key: "https://issuer.example/realms/acps-leader#raw-subject",
      principal_id: "principal-id",
      audiences: ["leader-api"],
      username: "alice",
      roles: ["ADMIN", "USER"],
      raw_claims: { sub: "raw-subject" },
    });

    const actor = auditActorFromPrincipal(principal);
    const actorPayload = JSON.parse(JSON.stringify(actor)) as Record<string, unknown>;

    expect(actor.id).toBe("principal-id");
    expect(actor.name).toBe("alice");
    expect(actor.role).toBe("ADMIN,USER");
    expect(actorPayload).not.toHaveProperty("subject");
    expect(actorPayload).not.toHaveProperty("principal_key");
    expect(actorPayload).not.toHaveProperty("raw_claims");
  });
});

describe("HumanPrincipal details (Node additions)", () => {
  const base = {
    issuer: "i",
    subject: "s",
    principal_key: "i#s",
    principal_id: "p",
    audiences: ["a"],
  };

  it("applies defaults and forbids unknown fields (extra='forbid')", () => {
    const principal = HumanPrincipalSchema.parse(base);
    expect(principal.roles).toEqual([]);
    expect(principal.allowed_aics).toEqual([]);
    expect(principal.raw_claims).toEqual({});
    expect(principal.azp).toBeUndefined();

    expect(() => HumanPrincipalSchema.parse({ ...base, extra: 1 })).toThrow(ZodError);
  });

  it("hasRole / hasScope require every listed value", () => {
    const principal = HumanPrincipalSchema.parse({ ...base, roles: ["A", "B"], scopes: ["x"] });

    expect(HumanPrincipal.hasRole(principal, "A", "B")).toBe(true);
    expect(HumanPrincipal.hasRole(principal, "A", "C")).toBe(false);
    expect(HumanPrincipal.hasRole(principal)).toBe(true);
    expect(HumanPrincipal.hasScope(principal, "x")).toBe(true);
    expect(HumanPrincipal.hasScope(principal, "y")).toBe(false);
  });

  it("re-parses an already parsed principal", () => {
    const principal = HumanPrincipalSchema.parse({ ...base, raw_claims: { sub: "s" } });
    const again = HumanPrincipalSchema.parse(principal);

    expect(again.subject).toBe("s");
    expect(again.raw_claims).toEqual({ sub: "s" });
    expect(Object.keys(again)).not.toContain("subject");
  });

  it("canonicalJsonBytes matches Python json.dumps details", () => {
    const text = (value: unknown) => canonicalJsonBytes(value).toString("utf-8");

    expect(text({ "10": 1, "2": 2, a: 3 })).toBe('{"10":1,"2":2,"a":3}');
    expect(text([1e-5, 0.0001, 1.5, NaN, Infinity])).toBe("[1e-05,0.0001,1.5,NaN,Infinity]");
    expect(text({ a: undefined, b: [undefined] })).toBe('{"b":[null]}');
    expect(text("中文\n")).toBe('"中文\\n"');
    expect(() => canonicalJsonBytes("\ud800")).toThrow("surrogates not allowed");
  });
});
