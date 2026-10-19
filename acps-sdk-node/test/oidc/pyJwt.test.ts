/**
 * PyJWT `_validate_claims` 移植的边界用例（时间判断与 leeway）。
 *
 * PyJWT 的规则：`iat > now + leeway` / `nbf > now + leeway` 为未生效，`exp <= now - leeway` 为已过期；
 * 这里用固定的 `now` 逐个检查边界两侧，确保 leeway 的方向与包含关系都与 Python 一致。
 */

import { describe, expect, it } from "vitest";

import { PyJwtError, validateJwtClaims } from "../../src/oidc/pyJwt.js";

const NOW = 1_900_000_000;
const ISSUER = "https://issuer.example";
const AUDIENCE = "api";

function check(
  overrides: Record<string, unknown>,
  leeway: number,
  now: number = NOW,
): string | null {
  const payload = { iss: ISSUER, aud: AUDIENCE, exp: NOW + 3600, ...overrides };
  try {
    validateJwtClaims(payload, {
      require: ["exp", "iss", "aud"],
      audience: AUDIENCE,
      issuer: ISSUER,
      leeway,
      now,
    });
    return null;
  } catch (error) {
    expect(error).toBeInstanceOf(PyJwtError);
    return (error as Error).message;
  }
}

describe("validateJwtClaims leeway boundaries", () => {
  it.each([
    [{ exp: NOW - 29 }, null],
    [{ exp: NOW - 30 }, "Signature has expired"],
    [{ exp: NOW - 31 }, "Signature has expired"],
    [{ iat: NOW + 29 }, null],
    [{ iat: NOW + 30 }, null],
    [{ iat: NOW + 31 }, "The token is not yet valid (iat)"],
    [{ nbf: NOW + 30 }, null],
    [{ nbf: NOW + 31 }, "The token is not yet valid (nbf)"],
  ])("leeway=30: %j → %s", (overrides, expected) => {
    expect(check(overrides, 30)).toBe(expected);
  });

  it.each([
    [{ exp: NOW + 1 }, null],
    [{ exp: NOW }, "Signature has expired"],
    [{ iat: NOW }, null],
    [{ iat: NOW + 1 }, "The token is not yet valid (iat)"],
    [{ nbf: NOW }, null],
    [{ nbf: NOW + 1 }, "The token is not yet valid (nbf)"],
  ])("leeway=0: %j → %s", (overrides, expected) => {
    expect(check(overrides, 0)).toBe(expected);
  });

  it("compares integer claims against a fractional now like Python (int vs float)", () => {
    // now - leeway = NOW - 29.5：exp = NOW - 30 已过期，exp = NOW - 29 未过期
    expect(check({ exp: NOW - 30 }, 30, NOW + 0.5)).toBe("Signature has expired");
    expect(check({ exp: NOW - 29 }, 30, NOW + 0.5)).toBeNull();
    // int(NOW + 30.9) = NOW + 30 <= now + leeway
    expect(check({ nbf: NOW + 30.9 }, 30)).toBeNull();
  });

  it("checks iat, then nbf, then exp, then iss, then aud (PyJWT order)", () => {
    const allBad = {
      iat: NOW + 999,
      nbf: NOW + 999,
      exp: NOW - 999,
      iss: "other",
      aud: "other",
    };
    expect(check(allBad, 0)).toBe("The token is not yet valid (iat)");
    expect(check({ ...allBad, iat: NOW }, 0)).toBe("The token is not yet valid (nbf)");
    expect(check({ ...allBad, iat: NOW, nbf: NOW }, 0)).toBe("Signature has expired");
    expect(check({ ...allBad, iat: NOW, nbf: NOW, exp: NOW + 1 }, 0)).toBe("Invalid issuer");
    expect(check({ aud: "other" }, 0)).toBe("Audience doesn't match");
  });
});
