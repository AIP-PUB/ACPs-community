/**
 * Keycloak access token claim 解析辅助工具（对应 Python `acps_sdk.oidc.keycloak`）。
 *
 * 失败时抛 `TypeError`，文本与 Python 版一致；`OidcTokenValidator` 会把它包装成
 * `InvalidAccessTokenError`。claim 读取只看自有属性（与 Python `dict.get` 一致）。
 */

import type { KeycloakClaimMapping } from "./config.js";
import {
  buildPrincipalId,
  buildPrincipalKey,
  HumanPrincipalSchema,
  type HumanPrincipal,
} from "./principal.js";
import {
  getOwn,
  isPlainObject,
  iterNonMappingSequence,
  normalizeDistinctStrings,
  pySplitWhitespace,
  pyStrip,
} from "../internal/pyCompat.js";

function requireStringIterable(values: unknown[], claimName: string): string[] {
  const result: string[] = [];
  for (const value of values) {
    if (typeof value !== "string") {
      const label = claimName !== "claim" ? `${claimName} claim` : "claim";
      throw new TypeError(`${label} values must be strings`);
    }
    result.push(value);
  }
  return result;
}

/** 把 aud claim 规范化为稳定的字符串数组。 */
export function normalizeAudiences(value: unknown): string[] {
  let items: string[];
  if (typeof value === "string") {
    items = [value];
  } else {
    const sequence = iterNonMappingSequence(value);
    if (sequence === null) {
      throw new TypeError("aud claim must be a string or array of strings");
    }
    items = requireStringIterable(sequence, "aud");
  }
  return normalizeDistinctStrings(items);
}

/** 解析 OAuth 2.0 的 scope claim（空白分隔）。 */
export function parseScopeClaim(value: unknown): string[] {
  if (value === null || value === undefined) {
    return [];
  }
  if (typeof value !== "string") {
    throw new TypeError("scope claim must be a string");
  }
  return normalizeDistinctStrings(pySplitWhitespace(value));
}

/** 解析可能是单字符串（可逗号分隔）或字符串数组的自定义 claim。 */
export function parseMultiValueClaim(value: unknown): string[] {
  if (value === null || value === undefined) {
    return [];
  }
  if (typeof value === "string") {
    if (value.includes(",")) {
      return normalizeDistinctStrings(value.split(",").map(pyStrip));
    }
    return normalizeDistinctStrings([value]);
  }
  const sequence = iterNonMappingSequence(value);
  if (sequence !== null) {
    return normalizeDistinctStrings(requireStringIterable(sequence, "claim"));
  }
  throw new TypeError("claim must be a string or iterable of strings");
}

function requireStringClaim(claims: Record<string, unknown>, key: string): string {
  const value = getOwn(claims, key);
  if (typeof value !== "string" || !pyStrip(value)) {
    throw new TypeError(`${key} claim must be a non-empty string`);
  }
  return value;
}

function optionalStringClaim(claims: Record<string, unknown>, key: string): string | undefined {
  const value = getOwn(claims, key);
  if (value === null || value === undefined) {
    return undefined;
  }
  if (typeof value !== "string") {
    throw new TypeError(`${key} claim must be a string`);
  }
  return pyStrip(value) || undefined;
}

function optionalBoolClaim(claims: Record<string, unknown>, key: string): boolean | undefined {
  const value = getOwn(claims, key);
  if (value === null || value === undefined) {
    return undefined;
  }
  if (typeof value !== "boolean") {
    throw new TypeError(`${key} claim must be a boolean`);
  }
  return value;
}

function firstStringClaim(
  claims: Record<string, unknown>,
  keys: Iterable<string>,
): string | undefined {
  for (const key of keys) {
    const value = getOwn(claims, key);
    if (typeof value === "string" && pyStrip(value)) {
      return pyStrip(value);
    }
  }
  return undefined;
}

/** 把已验签的 Keycloak claims 转换为规范化 principal。 */
export function claimsToPrincipal(options: {
  claims: Record<string, unknown>;
  claimMapping: KeycloakClaimMapping;
}): HumanPrincipal {
  const { claims, claimMapping } = options;
  const issuer = requireStringClaim(claims, "iss");
  const subject = requireStringClaim(claims, "sub");
  const audiences = normalizeAudiences(getOwn(claims, "aud"));
  const azp = optionalStringClaim(claims, "azp");

  const roles: string[] = [];
  if (claimMapping.read_client_roles && claimMapping.resource_client_id) {
    const resourceAccess = getOwn(claims, "resource_access");
    if (isPlainObject(resourceAccess)) {
      const clientClaims = getOwn(resourceAccess, claimMapping.resource_client_id);
      if (isPlainObject(clientClaims)) {
        roles.push(...parseMultiValueClaim(getOwn(clientClaims, "roles")));
      }
    }
  }
  if (claimMapping.read_realm_roles) {
    const realmAccess = getOwn(claims, "realm_access");
    if (isPlainObject(realmAccess)) {
      roles.push(...parseMultiValueClaim(getOwn(realmAccess, "roles")));
    }
  }

  let groups: string[] = [];
  if (claimMapping.read_groups) {
    groups = parseMultiValueClaim(getOwn(claims, "groups"));
  }

  const username = firstStringClaim(claims, claimMapping.username_claims);
  const name = firstStringClaim(claims, claimMapping.name_claims);
  const email = optionalStringClaim(claims, claimMapping.email_claim);
  const emailVerified = optionalBoolClaim(claims, claimMapping.email_verified_claim);
  const tenantId = optionalStringClaim(claims, claimMapping.tenant_claim);
  const allowedAics = parseMultiValueClaim(getOwn(claims, claimMapping.allowed_aics_claim));
  const scopes = parseScopeClaim(getOwn(claims, "scope"));

  return HumanPrincipalSchema.parse({
    issuer,
    subject,
    principal_key: buildPrincipalKey({ issuer, subject }),
    principal_id: buildPrincipalId({ issuer, subject }),
    audiences,
    azp,
    username,
    name,
    email,
    email_verified: emailVerified,
    tenant_id: tenantId,
    allowed_aics: allowedAics,
    roles: normalizeDistinctStrings(roles),
    scopes,
    groups,
    raw_claims: { ...claims },
  });
}
