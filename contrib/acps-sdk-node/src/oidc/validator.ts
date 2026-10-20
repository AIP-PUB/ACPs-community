/**
 * 基于 OIDC discovery 与 JWKS 的 access token 校验器（对应 Python `acps_sdk.oidc.validator`）。
 *
 * 校验顺序与 Python 版一致：
 * 1. 未验签解析 header（失败统一为 `invalid JWT header`），检查 `alg` 白名单、拒绝 `none` / `HS*`、要求 `kid`；
 * 2. discovery → JWKS 按 kid 取 key（未命中强制刷新一次）→ 检查 JWK 的 `alg`/`use`/`kty`/`crv`；
 * 3. `jose` 验签，随后按 PyJWT 的规则校验 `exp`/`iat`/`nbf`/`iss`/`aud`（含 leeway）；
 * 4. 检查 `azp` 白名单，按 Keycloak 映射生成 `HumanPrincipal`。
 *
 * 除少数 Python 版原样外抛的情况（见 `pyInt`），所有 token 问题都以 `InvalidAccessTokenError` 抛出；
 * provider 不可用时抛 `OidcProviderUnavailableError`。
 */

import { compactVerify, errors as joseErrors, importJWK, type CryptoKey, type JWK } from "jose";
import { ZodError } from "zod";

import { formatZodError } from "../internal/schema.js";
import {
  OidcProviderConfigSchema,
  type OidcProviderConfig,
  type OidcProviderConfigInput,
} from "./config.js";
import { OidcDiscoveryCache } from "./discovery.js";
import { InvalidAccessTokenError } from "./errors.js";
import { JwksCache, type JwkObject } from "./jwks.js";
import { claimsToPrincipal } from "./keycloak.js";
import type { HumanPrincipal } from "./principal.js";
import { getOwn, PyValueError, reprStr } from "../internal/pyCompat.js";
import {
  decodeJwtPayload,
  getUnverifiedHeader,
  loadJwt,
  PyJwtError,
  rejectUnencodedPayload,
  validateJwtClaims,
} from "./pyJwt.js";
import type { OidcFetch } from "./transport.js";

/** 构造 `OidcTokenValidator` 的选项，对应 Python 的关键字参数。 */
export interface OidcTokenValidatorOptions {
  /** Provider 配置；传入未解析的对象时按 `OidcProviderConfigSchema` 解析 */
  config: OidcProviderConfigInput;
  /** 自定义 fetch，默认全局 `fetch`（对应 Python 注入的 `httpx.AsyncClient`） */
  fetch?: OidcFetch;
  /** Node 版对 discovery / JWKS 缓存的加固参数（Python 版没有，不放进与 Python 共用的 config） */
  cache?: OidcCacheOptions;
}

/**
 * discovery / JWKS 缓存的加固参数（均为秒）。TTL 仍由 config 的 `*_cache_ttl_seconds` 决定。
 * 设为 `{ maxStaleSeconds: 0, failureCacheSeconds: 0, jwksRefreshCooldownSeconds: 0 }` 即与 Python 版行为一致。
 */
export interface OidcCacheOptions {
  /** 加载失败时还能继续使用过期文档的时长（从过期时刻算起），默认各自 TTL 的 10 倍 */
  maxStaleSeconds?: number;
  /** 加载失败后多长时间内直接复用失败结果、不再请求，默认 5 */
  failureCacheSeconds?: number;
  /** kid 未命中触发 JWKS 强制刷新的冷却时长，默认 30 */
  jwksRefreshCooldownSeconds?: number;
}

const REQUIRED_CLAIMS = ["exp", "iss", "aud"] as const;

/** 只保留公钥参数，交给 `jose.importJWK`（私钥参数、`key_ops`、`ext` 不参与验签）。 */
function publicJwkFor(jwk: JwkObject): JWK {
  switch (jwk.kty) {
    case "OKP":
      return { kty: "OKP", crv: jwk.crv, x: jwk.x } as JWK;
    case "EC":
      return { kty: "EC", crv: jwk.crv, x: jwk.x, y: jwk.y } as JWK;
    default:
      return { kty: "RSA", n: jwk.n, e: jwk.e } as JWK;
  }
}

/** 为单个 Resource Server 校验 OIDC access token。 */
export class OidcTokenValidator {
  readonly config: OidcProviderConfig;
  private readonly discovery: OidcDiscoveryCache;
  private readonly jwks: JwksCache;

  constructor(options: OidcTokenValidatorOptions) {
    this.config = OidcProviderConfigSchema.parse(options.config);
    const cache = options.cache ?? {};
    this.discovery = new OidcDiscoveryCache({
      ttlSeconds: this.config.discovery_cache_ttl_seconds,
      requireHttps: this.config.require_https,
      fetch: options.fetch,
      httpTimeoutSeconds: this.config.http_timeout_seconds,
      maxStaleSeconds: cache.maxStaleSeconds,
      failureCacheSeconds: cache.failureCacheSeconds,
    });
    this.jwks = new JwksCache({
      ttlSeconds: this.config.jwks_cache_ttl_seconds,
      requireHttps: this.config.require_https,
      fetch: options.fetch,
      httpTimeoutSeconds: this.config.http_timeout_seconds,
      maxStaleSeconds: cache.maxStaleSeconds,
      failureCacheSeconds: cache.failureCacheSeconds,
      refreshCooldownSeconds: cache.jwksRefreshCooldownSeconds,
    });
  }

  /** 与 Python 版对齐的生命周期方法；全局 fetch 没有需要释放的连接池，因此为空操作。 */
  async close(): Promise<void> {
    // 无需释放资源
  }

  /** 校验 bearer access token，并返回规范化后的 principal。 */
  async validateAccessToken(token: string): Promise<HumanPrincipal> {
    let header: Record<string, unknown>;
    try {
      header = getUnverifiedHeader(token);
    } catch (error) {
      throw new InvalidAccessTokenError("invalid JWT header", { cause: error });
    }

    const algorithm = getOwn(header, "alg");
    if (typeof algorithm !== "string" || !algorithm) {
      throw new InvalidAccessTokenError("JWT header alg is required");
    }
    if (!this.config.algorithms.includes(algorithm)) {
      throw new InvalidAccessTokenError(`JWT algorithm ${reprStr(algorithm)} is not allowed`);
    }
    if (algorithm === "none" || algorithm.startsWith("HS")) {
      throw new InvalidAccessTokenError(`JWT algorithm ${reprStr(algorithm)} is not allowed`);
    }

    const kid = getOwn(header, "kid");
    if (typeof kid !== "string" || !kid) {
      throw new InvalidAccessTokenError("JWT header kid is required");
    }

    const discovery = await this.discovery.get(this.config.issuer);
    const jwk = await this.jwks.getJwk({ jwksUri: discovery.jwks_uri, kid });
    OidcTokenValidator.validateJwkMetadata(jwk, algorithm, kid);

    let key: CryptoKey | Uint8Array;
    try {
      key = await importJWK(publicJwkFor(jwk), algorithm);
    } catch (error) {
      throw new InvalidAccessTokenError(`failed to construct JWK for kid ${kid}`, {
        cause: error,
      });
    }

    const claims = await this.decode(token, key, algorithm);
    this.validateAuthorizedParty(claims);
    try {
      return claimsToPrincipal({ claims, claimMapping: this.config.claim_mapping });
    } catch (error) {
      if (error instanceof TypeError || error instanceof PyValueError) {
        throw new InvalidAccessTokenError(error.message, { cause: error });
      }
      if (error instanceof ZodError) {
        throw new InvalidAccessTokenError(formatZodError(error), { cause: error });
      }
      throw error;
    }
  }

  /** 对应 Python 版的 `jwt.decode(...)`：验签 + PyJWT claim 校验。 */
  private async decode(
    token: string,
    key: CryptoKey | Uint8Array,
    algorithm: string,
  ): Promise<Record<string, unknown>> {
    try {
      const loaded = loadJwt(token);
      rejectUnencodedPayload(loaded.header);
      await this.verifySignature(token, key, algorithm);
      const claims = decodeJwtPayload(loaded.payload);
      validateJwtClaims(claims, {
        require: REQUIRED_CLAIMS,
        audience: this.config.audience,
        issuer: this.config.issuer,
        leeway: this.config.leeway_seconds,
      });
      return claims;
    } catch (error) {
      if (error instanceof PyJwtError) {
        throw new InvalidAccessTokenError(error.message, { cause: error });
      }
      throw error;
    }
  }

  private async verifySignature(
    token: string,
    key: CryptoKey | Uint8Array,
    algorithm: string,
  ): Promise<void> {
    try {
      await compactVerify(token, key, { algorithms: [algorithm] });
    } catch (error) {
      // header 与三段编码已按 PyJWT 规则检查过；jose 剩下的失败（签名不符，或 jose 更严格的
      // 编码检查）在 Python 版里都表现为签名验证失败。
      if (
        error instanceof joseErrors.JWSSignatureVerificationFailed ||
        error instanceof joseErrors.JWSInvalid
      ) {
        throw new PyJwtError("Signature verification failed");
      }
      throw new InvalidAccessTokenError((error as Error).message, { cause: error });
    }
  }

  private validateAuthorizedParty(claims: Record<string, unknown>): void {
    const allowedAzp = this.config.allowed_azp;
    if (allowedAzp.length === 0) {
      return;
    }
    const azp = getOwn(claims, "azp");
    if (typeof azp !== "string" || !allowedAzp.includes(azp)) {
      throw new InvalidAccessTokenError(`azp must be one of: ${allowedAzp.join(", ")}`);
    }
  }

  private static validateJwkMetadata(jwk: JwkObject, algorithm: string, kid: string): void {
    const jwkAlg = getOwn(jwk, "alg");
    if (jwkAlg !== undefined && jwkAlg !== null && jwkAlg !== algorithm) {
      throw new InvalidAccessTokenError(`JWK alg mismatch for kid ${kid}`);
    }
    const jwkUse = getOwn(jwk, "use");
    if (jwkUse !== undefined && jwkUse !== null && jwkUse !== "sig") {
      throw new InvalidAccessTokenError(`JWK use mismatch for kid ${kid}`);
    }

    const kty = getOwn(jwk, "kty");
    const crv = getOwn(jwk, "crv");
    if (algorithm === "EdDSA") {
      if (kty !== "OKP" || crv !== "Ed25519") {
        throw new InvalidAccessTokenError("EdDSA access tokens require OKP/Ed25519 JWKs");
      }
      return;
    }
    if (algorithm === "ES256") {
      if (kty !== "EC" || crv !== "P-256") {
        throw new InvalidAccessTokenError("ES256 access tokens require EC/P-256 JWKs");
      }
      return;
    }
    if (algorithm === "PS256" || algorithm === "RS256") {
      if (kty !== "RSA") {
        throw new InvalidAccessTokenError(`${algorithm} access tokens require RSA JWKs`);
      }
      return;
    }
    throw new InvalidAccessTokenError(`unsupported JWT algorithm ${reprStr(algorithm)}`);
  }
}
