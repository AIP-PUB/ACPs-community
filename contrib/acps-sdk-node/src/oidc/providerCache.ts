/**
 * discovery / JWKS 共用的远程文档缓存（内部模块）。
 *
 * 基础语义与 Python 版 `discovery.py` / `jwks.py` 一致：按 key（issuer 或 JWKS URL）缓存，TTL 由单调时钟判断
 * （`expires_at >= now` 视为有效）；未命中时加锁、锁内复查，只由一个调用方加载。
 *
 * 在此之上做了 Python 版没有的加固（Node 版行为差异，见 `CachePolicy`）：
 * 1. **失败短暂缓存**：加载失败后 `failureCacheSeconds` 内不再请求，直接复用这次的失败结果。
 *    IdP 卡住时，排队等锁的请求不会在锁内逐个重试（Python 版会让每个请求依次等满一次超时）。
 * 2. **过期兜底**：加载失败（或处于失败缓存期）时，若旧文档过期不超过 `maxStaleSeconds`，继续使用旧文档，
 *    而不是丢弃仍然可用的 key（Python 版直接抛 `OidcProviderUnavailableError`）。
 * 3. **强制刷新的合并与冷却**（JWKS kid 未命中时使用）：本次查找之后已有别人刷新过，就直接用新文档；
 *    距上次成功加载不足 `refreshCooldownSeconds` 时不再请求。kid 在验签之前读取，没有冷却时
 *    未认证的请求可以 1:1 地驱动对 IdP 的请求（Python 版每个未知 kid 都立即重新拉取）。
 */

import { AsyncLock } from "../internal/asyncLock.js";
import { getLogger } from "../internal/logger.js";
import { monotonicSeconds } from "./transport.js";

const logger = getLogger("acps_sdk.oidc");

/** 默认的失败缓存时长（秒）。 */
export const DEFAULT_FAILURE_CACHE_SECONDS = 5;
/** 默认的过期兜底窗口：TTL 的倍数。 */
export const DEFAULT_MAX_STALE_TTL_MULTIPLIER = 10;
/** JWKS 强制刷新的默认冷却时长（秒），与 jose `createRemoteJWKSet` 的 `cooldownDuration` 一致。 */
export const DEFAULT_JWKS_REFRESH_COOLDOWN_SECONDS = 30;

/** 缓存策略。所有时长单位为秒，负数按 0 处理。 */
export interface CachePolicy {
  /** 成功加载后的有效期（Python 版的 `*_cache_ttl_seconds`） */
  ttlSeconds: number;
  /** 加载失败时还能继续使用旧文档的时长（从旧文档过期时刻算起），默认 `10 × ttlSeconds` */
  maxStaleSeconds?: number;
  /** 加载失败后多长时间内不再请求，默认 5；0 表示不缓存失败 */
  failureCacheSeconds?: number;
  /** 强制刷新的冷却时长，默认 0（discovery 不用强制刷新） */
  refreshCooldownSeconds?: number;
}

/** 缓存中的一份文档。 */
export interface CachedDocument<T> {
  readonly value: T;
  /** 成功加载的时刻（单调时钟，秒） */
  readonly fetchedAt: number;
  readonly expiresAt: number;
  /** 每次成功加载递增，用于判断“查找之后是否已被别人刷新” */
  readonly generation: number;
}

interface FailureEntry {
  error: unknown;
  until: number;
}

const nonNegative = (value: number): number => (value > 0 ? value : 0);

/** 带 TTL、失败缓存、过期兜底与刷新冷却的远程文档缓存。 */
export class RemoteDocumentCache<T> {
  private readonly ttlSeconds: number;
  private readonly maxStaleSeconds: number;
  private readonly failureCacheSeconds: number;
  private readonly refreshCooldownSeconds: number;
  private readonly entries = new Map<string, CachedDocument<T>>();
  private readonly failures = new Map<string, FailureEntry>();
  private readonly lock = new AsyncLock();
  private generation = 0;

  constructor(
    policy: CachePolicy,
    private readonly load: (key: string) => Promise<T>,
    private readonly label: string,
  ) {
    this.ttlSeconds = nonNegative(policy.ttlSeconds);
    this.maxStaleSeconds = nonNegative(
      policy.maxStaleSeconds ?? this.ttlSeconds * DEFAULT_MAX_STALE_TTL_MULTIPLIER,
    );
    this.failureCacheSeconds = nonNegative(
      policy.failureCacheSeconds ?? DEFAULT_FAILURE_CACHE_SECONDS,
    );
    this.refreshCooldownSeconds = nonNegative(policy.refreshCooldownSeconds ?? 0);
  }

  /** 取有效文档；过期则加载（失败时按策略兜底或抛出加载错误）。 */
  async get(key: string): Promise<CachedDocument<T>> {
    const cached = this.lookup(key);
    if (cached !== undefined) {
      return cached;
    }
    return this.lock.runExclusive(async () => this.lookup(key) ?? this.fetch(key, true));
  }

  /**
   * 强制刷新（kid 未命中时）。`seen` 是调用方刚才查找用的文档：
   * 若之后已有新文档，或距上次成功加载不足冷却时长，直接返回当前文档而不请求。
   * 刷新失败时抛出加载错误（与 Python 版强制刷新失败的结果一致）。
   */
  async refresh(key: string, seen: CachedDocument<T>): Promise<CachedDocument<T>> {
    return this.lock.runExclusive(async () => {
      const now = monotonicSeconds();
      const current = this.entries.get(key);
      if (current !== undefined) {
        if (current.generation !== seen.generation) {
          return current;
        }
        if (now - current.fetchedAt < this.refreshCooldownSeconds) {
          return current;
        }
      }
      const failure = this.activeFailure(key, now);
      if (failure !== undefined) {
        throw failure.error;
      }
      return this.fetch(key, false);
    });
  }

  /** 不加锁的查找：有效文档、失败缓存期内的兜底文档或失败；都没有时返回 undefined（需要加载）。 */
  private lookup(key: string): CachedDocument<T> | undefined {
    const now = monotonicSeconds();
    const entry = this.entries.get(key);
    if (entry !== undefined && entry.expiresAt >= now) {
      return entry;
    }
    const failure = this.activeFailure(key, now);
    if (failure === undefined) {
      return undefined;
    }
    if (entry !== undefined && this.isServableStale(entry, now)) {
      return entry;
    }
    throw failure.error;
  }

  private async fetch(key: string, allowStale: boolean): Promise<CachedDocument<T>> {
    try {
      const value = await this.load(key);
      const now = monotonicSeconds();
      this.generation += 1;
      const entry: CachedDocument<T> = {
        value,
        fetchedAt: now,
        expiresAt: now + this.ttlSeconds,
        generation: this.generation,
      };
      this.entries.set(key, entry);
      this.failures.delete(key);
      return entry;
    } catch (error) {
      const now = monotonicSeconds();
      if (this.failureCacheSeconds > 0) {
        this.failures.set(key, { error, until: now + this.failureCacheSeconds });
      }
      const stale = this.entries.get(key);
      if (allowStale && stale !== undefined && this.isServableStale(stale, now)) {
        logger.warn(
          `failed to refresh ${this.label} for ${key}; serving the cached copy that expired ` +
            `${(now - stale.expiresAt).toFixed(1)}s ago`,
          error,
        );
        return stale;
      }
      throw error;
    }
  }

  private activeFailure(key: string, now: number): FailureEntry | undefined {
    const failure = this.failures.get(key);
    return failure !== undefined && failure.until > now ? failure : undefined;
  }

  private isServableStale(entry: CachedDocument<T>, now: number): boolean {
    return now <= entry.expiresAt + this.maxStaleSeconds;
  }
}
