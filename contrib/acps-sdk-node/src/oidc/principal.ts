/**
 * 从 OIDC access token 派生出的真人 principal 抽象（对应 Python `acps_sdk.oidc.principal`）。
 */

import { createHash } from "node:crypto";

import { z } from "zod";

import { AnyDict, optional } from "../internal/schema.js";
import { compareCodePoints, pyFloatRepr, PyValueError } from "../internal/pyCompat.js";

const LONE_SURROGATE = /\p{Cs}/u;

const SHORT_ESCAPES: Record<string, string> = {
  '"': '\\"',
  "\\": "\\\\",
  "\b": "\\b",
  "\f": "\\f",
  "\n": "\\n",
  "\r": "\\r",
  "\t": "\\t",
};

/**
 * Python `json.dumps(ensure_ascii=False)` 的字符串编码：只转义 `"`、`\`、\b \f \n \r \t 与其它
 * U+0000–U+001F（`\u00xx`），其余字符（含孤立代理项）原样输出。
 */
function encodeCanonicalString(value: string): string {
  let out = '"';
  for (const ch of value) {
    const short = SHORT_ESCAPES[ch];
    if (short !== undefined) {
      out += short;
    } else if (ch < " ") {
      out += `\\u${ch.charCodeAt(0).toString(16).padStart(4, "0")}`;
    } else {
      out += ch;
    }
  }
  return `${out}"`;
}

function encodeCanonicalNumber(value: number): string {
  if (Number.isNaN(value)) return "NaN";
  if (!Number.isFinite(value)) return value > 0 ? "Infinity" : "-Infinity";
  // JSON 解析后无法区分 1 与 1.0；整数值按 Python int 输出（大整数也写全位数，不用指数）。
  return Number.isInteger(value) ? BigInt(value).toString() : pyFloatRepr(value);
}

function encodeCanonical(value: unknown, inArray: boolean): string | undefined {
  if (
    value !== null &&
    typeof value === "object" &&
    typeof (value as { toJSON?: unknown }).toJSON === "function"
  ) {
    value = (value as { toJSON: () => unknown }).toJSON();
  }
  switch (typeof value) {
    case "string":
      return encodeCanonicalString(value);
    case "number":
      return encodeCanonicalNumber(value);
    case "bigint":
      return value.toString();
    case "boolean":
      return value ? "true" : "false";
    case "undefined":
    case "function":
    case "symbol":
      // 与 JSON.stringify 一致：对象里跳过该键，数组里写 null。
      return inArray ? "null" : undefined;
    default:
      break;
  }
  if (value === null) {
    return "null";
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => encodeCanonical(item, true) ?? "null").join(",")}]`;
  }
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record).sort(compareCodePoints);
  const parts: string[] = [];
  for (const key of keys) {
    const encoded = encodeCanonical(record[key], false);
    if (encoded !== undefined) {
      parts.push(`${encodeCanonicalString(key)}:${encoded}`);
    }
  }
  return `{${parts.join(",")}}`;
}

/**
 * 把小型 JSON 值稳定序列化为字节串，便于做 hashing。
 *
 * 等价于 Python `json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")`：
 * 键按码点排序、数字按 Python 格式输出、`NaN`/`Infinity` 写成 Python 的字面量；
 * 含孤立代理项时与 Python `.encode("utf-8")` 一样失败。
 */
export function canonicalJsonBytes(value: unknown): Buffer {
  const text = encodeCanonical(value, true) ?? "null";
  const match = LONE_SURROGATE.exec(text);
  if (match !== null) {
    const position = Array.from(text.slice(0, match.index)).length;
    const code = (match[0].codePointAt(0) ?? 0).toString(16);
    throw new PyValueError(
      `'utf-8' codec can't encode character '\\u${code}' in position ${position}: surrogates not allowed`,
    );
  }
  return Buffer.from(text, "utf-8");
}

/** 构造可读的复合身份键：`{issuer}#{subject}`。 */
export function buildPrincipalKey(options: { issuer: string; subject: string }): string {
  return `${options.issuer}#${options.subject}`;
}

/** 对 issuer/sub 组合做结构化 hashing，避免分隔符歧义。 */
export function buildPrincipalId(options: { issuer: string; subject: string }): string {
  const payload = canonicalJsonBytes(["oidc-principal-v1", options.issuer, options.subject]);
  return createHash("sha256").update(payload).digest("hex");
}

/** 序列化与 `inspect` 时排除的敏感字段（Python 的 `Field(repr=False, exclude=True)`）。 */
const EXCLUDED_FIELDS = ["subject", "principal_key", "raw_claims"] as const;

/**
 * 用于 API 授权与审计的规范化真人 principal（`extra="forbid"`）。
 *
 * `HumanPrincipalSchema.parse(input)` 对应 Python 的 `HumanPrincipal(...)`。解析结果里
 * `subject`、`principal_key`、`raw_claims` 是**不可枚举**属性：可以正常读取，但
 * `JSON.stringify`、对象展开与 `util.inspect` / `console.log` 都不会包含它们，
 * 对应 Python 的 `model_dump()` 与 `repr()` 排除这三个字段。
 */
export const HumanPrincipalSchema = z
  .strictObject({
    issuer: z.string(),
    /** 原始 `sub`，不参与序列化 */
    subject: z.string(),
    /** `{issuer}#{subject}`，不参与序列化 */
    principal_key: z.string(),
    principal_id: z.string(),
    audiences: z.array(z.string()),
    azp: optional(z.string()),
    username: optional(z.string()),
    name: optional(z.string()),
    email: optional(z.string()),
    email_verified: optional(z.boolean()),
    tenant_id: optional(z.string()),
    allowed_aics: z.array(z.string()).default(() => []),
    roles: z.array(z.string()).default(() => []),
    scopes: z.array(z.string()).default(() => []),
    groups: z.array(z.string()).default(() => []),
    /** 已验签的原始 claims，不参与序列化 */
    raw_claims: AnyDict.default(() => ({})),
  })
  .transform((principal) => {
    for (const field of EXCLUDED_FIELDS) {
      Object.defineProperty(principal, field, { enumerable: false });
    }
    return principal;
  });
export type HumanPrincipal = z.output<typeof HumanPrincipalSchema>;
export type HumanPrincipalInput = z.input<typeof HumanPrincipalSchema>;

/** HumanPrincipal 的方法（对应 Python `HumanPrincipal` 上的实例方法）。 */
export const HumanPrincipal = {
  /** 是否具备全部指定 roles。 */
  hasRole(principal: HumanPrincipal, ...requiredRoles: string[]): boolean {
    const roleSet = new Set(principal.roles);
    return requiredRoles.every((role) => roleSet.has(role));
  },

  /** 是否具备全部指定 scopes。 */
  hasScope(principal: HumanPrincipal, ...requiredScopes: string[]): boolean {
    const scopeSet = new Set(principal.scopes);
    return requiredScopes.every((scope) => scopeSet.has(scope));
  },
};
