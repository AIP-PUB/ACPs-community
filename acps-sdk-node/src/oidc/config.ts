/**
 * OIDC Provider 配置模型（对应 Python `acps_sdk.oidc.config`）。
 *
 * 字段名与 Python 版一致（snake_case，Python 版没有 alias）。`XxxSchema.parse(...)` 对应
 * Python 的 `Xxx(...)` 构造：补默认值、规范化列表、校验取值范围。
 *
 * 与 pydantic 一致的两类失败：
 * - 校验器里的 `ValueError` → zod 校验错误（`ZodError`），文本与 Python 相同；
 * - 列表字段校验器里的 `TypeError` 在 pydantic v2 里不会被包装，原样抛出；这里同样直接抛 `TypeError`。
 */

import { z } from "zod";

import { optional } from "../internal/schema.js";
import {
  iterPython,
  normalizeDistinctStrings,
  pyRstripSlash,
  pyStr,
  pyStrip,
} from "../internal/pyCompat.js";

/** 列表字段可接受的输入：单个字符串、字符串序列或 `null`。 */
export type StringListInput = string | Iterable<unknown> | null;

/**
 * 对应 Python 的 `mode="before"` 列表校验器：`None` → 空、字符串 → 单元素、Iterable → `str(item)` 逐个转换，
 * 然后 strip、去空、去重。
 */
function stringList(typeErrorMessage: string): z.ZodType<string[], StringListInput> {
  return z.preprocess((value) => {
    if (value === null) return [];
    if (typeof value === "string") return normalizeDistinctStrings([value]);
    const items = iterPython(value);
    if (items !== null) return normalizeDistinctStrings(items.map(pyStr));
    throw new TypeError(typeErrorMessage);
  }, z.array(z.string())) as unknown as z.ZodType<string[], StringListInput>;
}

function nonNegativeInt(defaultValue: number) {
  return z
    .number()
    .int()
    .refine((value) => value >= 0, "value must be >= 0")
    .default(defaultValue);
}

/** 定义如何把 Keycloak access token claims 映射为真人 principal。 */
export const KeycloakClaimMappingSchema = z.strictObject({
  /** 在 resource_access 下读取项目角色时使用的 client id。 */
  resource_client_id: optional(z.string()),
  read_client_roles: z.boolean().default(true),
  read_realm_roles: z.boolean().default(false),
  read_groups: z.boolean().default(true),
  username_claims: stringList("claim list must be a string or iterable of strings").default(() => [
    "preferred_username",
    "username",
  ]),
  name_claims: stringList("claim list must be a string or iterable of strings").default(() => [
    "name",
    "preferred_username",
  ]),
  email_claim: z.string().default("email"),
  email_verified_claim: z.string().default("email_verified"),
  tenant_claim: z.string().default("tenant_id"),
  allowed_aics_claim: z.string().default("allowed_aics"),
});
export type KeycloakClaimMapping = z.output<typeof KeycloakClaimMappingSchema>;
export type KeycloakClaimMappingInput = z.input<typeof KeycloakClaimMappingSchema>;

/** Resource Server 共用的 OIDC Provider 配置。 */
export const OidcProviderConfigSchema = z.strictObject({
  issuer: z.string().transform((value, ctx) => {
    const issuer = pyRstripSlash(pyStrip(value));
    if (!issuer) {
      ctx.addIssue({ code: "custom", message: "issuer must not be empty" });
      return z.NEVER;
    }
    return issuer;
  }),
  audience: z.string().transform((value, ctx) => {
    const audience = pyStrip(value);
    if (!audience) {
      ctx.addIssue({ code: "custom", message: "audience must not be empty" });
      return z.NEVER;
    }
    return audience;
  }),
  allowed_azp: stringList("value must be a string or iterable of strings").default(() => []),
  /** 本地 Resource Server 标识，不参与 token 验签。 */
  client_id: optional(z.string()),
  algorithms: stringList("value must be a string or iterable of strings").default(() => ["EdDSA"]),
  jwks_cache_ttl_seconds: nonNegativeInt(300),
  discovery_cache_ttl_seconds: nonNegativeInt(300),
  leeway_seconds: nonNegativeInt(30),
  require_https: z.boolean().default(true),
  http_timeout_seconds: z
    .number()
    .refine((value) => value > 0, "http_timeout_seconds must be > 0")
    .default(5),
  claim_mapping: KeycloakClaimMappingSchema.prefault({}),
});
export type OidcProviderConfig = z.output<typeof OidcProviderConfigSchema>;
export type OidcProviderConfigInput = z.input<typeof OidcProviderConfigSchema>;
