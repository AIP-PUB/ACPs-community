/**
 * JWKS 加载与 key 选择辅助工具（对应 Python `acps_sdk.oidc.jwks`）。
 *
 * 与 Python 版相同的部分：
 * - 按 JWKS URL 缓存整份文档，TTL 由单调时钟判断（`expires_at >= now` 视为有效）；
 * - 按 kid 查找未命中时强制刷新一次，刷新后仍未命中抛 `unknown JWK kid: {kid}`；
 * - 并发加载由互斥锁串行化，加锁后再查一次缓存，避免重复请求。
 *
 * Node 版加固（Python 版没有，见 `providerCache.ts`）：
 * - 强制刷新会合并（查找之后已被别人刷新则不再请求），并有冷却（默认 30 秒内最多一次）。
 *   kid 在验签之前读取，没有冷却时伪造 kid 的未认证请求可以 1:1 地驱动对 IdP 的请求；
 *   返回 `{"keys": []}` 的 JWKS 同样受冷却约束。代价是轮换出的新 key 最多要等一个冷却期才会被拉取；
 * - 加载失败会短暂缓存，并在 `maxStaleSeconds` 内继续使用过期文档。
 */

import { InvalidAccessTokenError, OidcProviderUnavailableError } from "./errors.js";
import { isPlainObject } from "../internal/pyCompat.js";
import { DEFAULT_JWKS_REFRESH_COOLDOWN_SECONDS, RemoteDocumentCache } from "./providerCache.js";
import {
  DEFAULT_HTTP_TIMEOUT_SECONDS,
  isSuccessStatus,
  sendHttpRequest,
  type OidcFetch,
} from "./transport.js";

/** JWKS 文档：至少包含 `keys` 数组。 */
export type JwksDocument = Record<string, unknown> & { keys: unknown[] };

/** 单个 JWK（未经校验的 JSON 对象）。 */
export type JwkObject = Record<string, unknown>;

/** 构造 `JwksCache` 的选项，对应 Python 的关键字参数。 */
export interface JwksCacheOptions {
  ttlSeconds: number;
  requireHttps: boolean;
  fetch?: OidcFetch;
  /** 单次请求超时（秒），默认 5 */
  httpTimeoutSeconds?: number;
  /** Node 版加固：加载失败时还能继续使用过期文档的时长（从过期时刻算起），默认 `10 × ttlSeconds` */
  maxStaleSeconds?: number;
  /** Node 版加固：加载失败后多长时间内直接复用失败结果、不再请求，默认 5 秒；0 关闭 */
  failureCacheSeconds?: number;
  /** Node 版加固：kid 未命中触发的强制刷新的冷却时长，默认 30 秒；0 表示与 Python 版一样每次都刷新 */
  refreshCooldownSeconds?: number;
}

/** 按 JWKS URL 缓存文档的 TTL cache。 */
export class JwksCache {
  private readonly requireHttps: boolean;
  private readonly fetchImpl: OidcFetch | undefined;
  private readonly httpTimeoutSeconds: number;
  private readonly cache: RemoteDocumentCache<JwksDocument>;

  constructor(options: JwksCacheOptions) {
    this.requireHttps = options.requireHttps;
    this.fetchImpl = options.fetch;
    this.httpTimeoutSeconds = options.httpTimeoutSeconds ?? DEFAULT_HTTP_TIMEOUT_SECONDS;
    this.cache = new RemoteDocumentCache(
      {
        ttlSeconds: options.ttlSeconds,
        maxStaleSeconds: options.maxStaleSeconds,
        failureCacheSeconds: options.failureCacheSeconds,
        refreshCooldownSeconds:
          options.refreshCooldownSeconds ?? DEFAULT_JWKS_REFRESH_COOLDOWN_SECONDS,
      },
      (jwksUri) => this.fetchDocument(jwksUri),
      "JWKS",
    );
  }

  async getJwk(options: { jwksUri: string; kid: string }): Promise<JwkObject> {
    const { jwksUri, kid } = options;
    this.enforceHttps(jwksUri);
    const entry = await this.cache.get(jwksUri);
    const jwk = JwksCache.findJwk(entry.value, kid);
    if (jwk !== null) {
      return jwk;
    }

    const refreshed = await this.cache.refresh(jwksUri, entry);
    const refreshedJwk = JwksCache.findJwk(refreshed.value, kid);
    if (refreshedJwk !== null) {
      return refreshedJwk;
    }

    throw new InvalidAccessTokenError(`unknown JWK kid: ${kid}`);
  }

  private async fetchDocument(jwksUri: string): Promise<JwksDocument> {
    let text: string;
    try {
      const result = await sendHttpRequest(
        this.fetchImpl,
        jwksUri,
        { method: "GET" },
        this.httpTimeoutSeconds,
      );
      if (!isSuccessStatus(result.status)) {
        throw new Error(`HTTP status ${result.status}`);
      }
      text = result.text;
    } catch (error) {
      throw new OidcProviderUnavailableError(`failed to load JWKS from ${jwksUri}`, {
        cause: error,
      });
    }

    let data: unknown;
    try {
      data = JSON.parse(text);
    } catch (error) {
      throw new OidcProviderUnavailableError(`JWKS at ${jwksUri} is not valid JSON`, {
        cause: error,
      });
    }

    if (!isPlainObject(data)) {
      throw new OidcProviderUnavailableError("JWKS response must be a JSON object");
    }
    if (!Array.isArray(data.keys)) {
      throw new OidcProviderUnavailableError("JWKS response must contain a keys array");
    }
    return data as JwksDocument;
  }

  private static findJwk(document: JwksDocument, kid: string): JwkObject | null {
    for (const key of document.keys) {
      if (isPlainObject(key) && key.kid === kid) {
        return key;
      }
    }
    return null;
  }

  private enforceHttps(jwksUri: string): void {
    if (this.requireHttps && !jwksUri.startsWith("https://")) {
      throw new OidcProviderUnavailableError("jwks_uri must use https when require_https=true");
    }
  }
}
