/**
 * 带 TTL cache 的 OIDC discovery document 加载器（对应 Python `acps_sdk.oidc.discovery`）。
 */

import { z } from "zod";

import { optional } from "../internal/schema.js";
import { OidcProviderUnavailableError } from "./errors.js";
import { pyRstripSlash } from "../internal/pyCompat.js";
import { RemoteDocumentCache } from "./providerCache.js";
import {
  DEFAULT_HTTP_TIMEOUT_SECONDS,
  isSuccessStatus,
  sendHttpRequest,
  type OidcFetch,
} from "./transport.js";

/** OIDC discovery document 中当前实现会用到的字段子集；其它字段原样保留。 */
export const OidcDiscoveryDocumentSchema = z.looseObject({
  issuer: z.string(),
  jwks_uri: z.string(),
  authorization_endpoint: optional(z.string()),
  device_authorization_endpoint: optional(z.string()),
  token_endpoint: optional(z.string()),
  revocation_endpoint: optional(z.string()),
  end_session_endpoint: optional(z.string()),
});
export type OidcDiscoveryDocument = z.output<typeof OidcDiscoveryDocumentSchema>;
export type OidcDiscoveryDocumentInput = z.input<typeof OidcDiscoveryDocumentSchema>;

/** 构造 `OidcDiscoveryCache` 的选项，对应 Python 的关键字参数。 */
export interface OidcDiscoveryCacheOptions {
  ttlSeconds: number;
  requireHttps: boolean;
  /** 自定义 fetch，默认全局 `fetch`（对应 Python 注入的 `httpx.AsyncClient`） */
  fetch?: OidcFetch;
  /** 单次请求超时（秒），默认 5 */
  httpTimeoutSeconds?: number;
  /**
   * Node 版加固：加载失败时还能继续使用过期文档的时长（从过期时刻算起），默认 `10 × ttlSeconds`。
   * Python 版没有这一兜底。
   */
  maxStaleSeconds?: number;
  /** Node 版加固：加载失败后多长时间内直接复用失败结果、不再请求，默认 5 秒；0 关闭。 */
  failureCacheSeconds?: number;
}

/** 安全要求：`require_https` 打开时 URL 必须以 `https://` 开头。 */
export function enforceHttps(requireHttps: boolean, url: string, kind: string): void {
  if (requireHttps && !url.startsWith("https://")) {
    throw new OidcProviderUnavailableError(`${kind} must use https when require_https=true`);
  }
}

/**
 * 加载并校验 `{issuer}/.well-known/openid-configuration`。
 * `OidcDiscoveryCache` 与 `OidcDeviceClient` 共用，错误文本与 Python 版一致。
 */
export async function fetchDiscoveryDocument(options: {
  issuer: string;
  requireHttps: boolean;
  fetch: OidcFetch | undefined;
  httpTimeoutSeconds: number;
}): Promise<OidcDiscoveryDocument> {
  const { issuer, requireHttps } = options;
  enforceHttps(requireHttps, issuer, "issuer");
  const wellKnownUrl = `${issuer}/.well-known/openid-configuration`;
  let text: string;
  try {
    const result = await sendHttpRequest(
      options.fetch,
      wellKnownUrl,
      { method: "GET" },
      options.httpTimeoutSeconds,
    );
    if (!isSuccessStatus(result.status)) {
      throw new Error(`HTTP status ${result.status}`);
    }
    text = result.text;
  } catch (error) {
    throw new OidcProviderUnavailableError(
      `failed to load OIDC discovery document from ${wellKnownUrl}`,
      { cause: error },
    );
  }

  let payload: unknown;
  try {
    payload = JSON.parse(text);
  } catch (error) {
    throw new OidcProviderUnavailableError(
      `OIDC discovery document at ${wellKnownUrl} is not valid JSON`,
      { cause: error },
    );
  }

  const parsed = OidcDiscoveryDocumentSchema.safeParse(payload);
  if (!parsed.success) {
    throw new OidcProviderUnavailableError(
      `OIDC discovery document at ${wellKnownUrl} is invalid`,
      { cause: parsed.error },
    );
  }
  const document = parsed.data;
  if (pyRstripSlash(document.issuer) !== issuer) {
    throw new OidcProviderUnavailableError(
      `OIDC discovery issuer mismatch: expected ${issuer}, got ${document.issuer}`,
    );
  }
  enforceHttps(requireHttps, document.jwks_uri, "jwks_uri");
  return document;
}

/**
 * 按 issuer 缓存 discovery document 的 TTL cache。
 *
 * 并发未命中只加载一次；加载失败会短暂缓存，并在 `maxStaleSeconds` 内继续使用过期文档（见 `providerCache.ts`）。
 */
export class OidcDiscoveryCache {
  private readonly cache: RemoteDocumentCache<OidcDiscoveryDocument>;

  constructor(options: OidcDiscoveryCacheOptions) {
    const httpTimeoutSeconds = options.httpTimeoutSeconds ?? DEFAULT_HTTP_TIMEOUT_SECONDS;
    this.cache = new RemoteDocumentCache(
      {
        ttlSeconds: options.ttlSeconds,
        maxStaleSeconds: options.maxStaleSeconds,
        failureCacheSeconds: options.failureCacheSeconds,
      },
      (issuer) =>
        fetchDiscoveryDocument({
          issuer,
          requireHttps: options.requireHttps,
          fetch: options.fetch,
          httpTimeoutSeconds,
        }),
      "OIDC discovery document",
    );
  }

  async get(issuer: string): Promise<OidcDiscoveryDocument> {
    return (await this.cache.get(pyRstripSlash(issuer))).value;
  }
}
