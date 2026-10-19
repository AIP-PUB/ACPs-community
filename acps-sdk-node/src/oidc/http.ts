/**
 * 面向 OIDC bearer authentication 的 HTTP 辅助工具（对应 Python `acps_sdk.oidc.fastapi`）。
 *
 * Python 版是 FastAPI 依赖；Node 版不绑定框架，分三层：
 * 1. 依赖工厂 `requirePrincipal` / `optionalPrincipal` / `requireRoles` / `requireScopes`：返回
 *    `(source) => Promise<...>`，`source` 可以是 `Authorization` 头的值、Node `IncomingMessage`
 *    或任何带 `headers` 的请求对象；失败时抛 SDK 异常（`MissingBearerTokenError`、`InvalidAccessTokenError`、
 *    `OidcProviderUnavailableError`、`MissingRoleError`、`MissingScopeError`）；
 * 2. `oidcErrorToHttpResponse`：把上述异常映射为 FastAPI 依赖会返回的 HTTP 响应
 *    （401 + `WWW-Authenticate: Bearer` / 403 / 503，响应体 `{"detail": ...}`）；
 * 3. `createOidcMiddleware`：Express 兼容的中间件（结构化类型，不依赖 express），成功时把 principal
 *    挂到 `req.principal`，OIDC 异常直接写响应，其它异常交给 `next(error)`（等价于 FastAPI 的 500）。
 *
 * Bearer 解析与 FastAPI `HTTPBearer(auto_error=False)` 的组合行为一致：非 Bearer scheme、
 * 缺少凭据都视为“没有 token”（`require*` 返回 401 `Missing bearer token`，`optionalPrincipal` 返回 null）。
 */

import type { IncomingHttpHeaders } from "node:http";

import type { AuditActor } from "../amp/models.js";
import {
  InvalidAccessTokenError,
  MissingBearerTokenError,
  MissingRoleError,
  MissingScopeError,
  OidcAuthenticationError,
  OidcAuthorizationError,
  OidcProviderUnavailableError,
} from "./errors.js";
import type { HumanPrincipal } from "./principal.js";
import { compareCodePoints, pyStrip } from "../internal/pyCompat.js";

/** 只需要 `validateAccessToken` 的校验器形状（`OidcTokenValidator` 满足）。 */
export interface AccessTokenValidator {
  validateAccessToken(token: string): Promise<HumanPrincipal>;
}

/** Fetch API 风格的 headers（`Headers` 实例）。 */
export interface HeadersGetter {
  get(name: string): string | null;
}

/** 依赖函数可接受的输入：`Authorization` 头的值，或带 `headers` 的请求对象。 */
export type AuthorizationSource =
  | string
  | null
  | undefined
  | {
      headers: IncomingHttpHeaders | Record<string, string | string[] | undefined> | HeadersGetter;
    };

/** 依赖函数，对应 Python 版 FastAPI 依赖。 */
export type OidcDependency<T> = (source: AuthorizationSource) => Promise<T>;

function isHeadersGetter(value: unknown): value is HeadersGetter {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { get?: unknown }).get === "function"
  );
}

/** 取 `Authorization` 头；重复出现时取第一个（与 Starlette `headers.get` 一致）。 */
export function getAuthorizationHeader(source: AuthorizationSource): string | null {
  if (source === null || source === undefined) {
    return null;
  }
  if (typeof source === "string") {
    return source;
  }
  const { headers } = source;
  if (isHeadersGetter(headers)) {
    return headers.get("authorization");
  }
  const record = headers as Record<string, string | string[] | undefined>;
  let value = record.authorization;
  if (value === undefined) {
    const key = Object.keys(record).find((name) => name.toLowerCase() === "authorization");
    value = key === undefined ? undefined : record[key];
  }
  if (Array.isArray(value)) {
    return value[0] ?? null;
  }
  return value ?? null;
}

/**
 * 从 `Authorization` 头取 bearer token，没有可用 token 时返回 `null`。
 *
 * 组合了 FastAPI `get_authorization_scheme_param`（按第一个空格切分、凭据 strip）、
 * `HTTPBearer(auto_error=False)`（缺 scheme/凭据或 scheme 不是 bearer 时返回 None）
 * 与 Python 版 `_extract_bearer_token` 的规则。
 */
export function extractBearerToken(authorization: string | null | undefined): string | null {
  if (!authorization) {
    return null;
  }
  const space = authorization.indexOf(" ");
  const scheme = space === -1 ? authorization : authorization.slice(0, space);
  const credentials = space === -1 ? "" : pyStrip(authorization.slice(space + 1));
  if (!scheme || !credentials) {
    return null;
  }
  if (scheme.toLowerCase() !== "bearer") {
    return null;
  }
  return credentials;
}

/**
 * 拒绝单个字符串的 `Iterable<string>`。Python 版 `require_roles(validator, "ADMIN")` 会把字符串当作
 * 字符序列（要求角色 A、D、M、I、N），这几乎总是调用错误；Node 版在类型和运行时都拒绝它。
 */
export type NonStringIterable<T extends Iterable<string>> = T extends string ? never : T;

function sortedDistinct(values: Iterable<string>, parameter: string): string[] {
  if (typeof values === "string") {
    throw new TypeError(`${parameter} must be an iterable of strings, not a single string`);
  }
  return Array.from(new Set(Array.from(values).filter((value) => value))).sort(compareCodePoints);
}

/** 返回一个要求请求中必须携带有效真人 principal 的依赖。 */
export function requirePrincipal(validator: AccessTokenValidator): OidcDependency<HumanPrincipal> {
  return async (source) => {
    const token = extractBearerToken(getAuthorizationHeader(source));
    if (token === null) {
      throw new MissingBearerTokenError("Missing bearer token");
    }
    return validator.validateAccessToken(token);
  };
}

/** 返回一个可选 principal 依赖；token 缺失或无效时返回 `null`，provider 不可用时仍抛异常（503）。 */
export function optionalPrincipal(
  validator: AccessTokenValidator,
): OidcDependency<HumanPrincipal | null> {
  return async (source) => {
    const token = extractBearerToken(getAuthorizationHeader(source));
    if (token === null) {
      return null;
    }
    try {
      return await validator.validateAccessToken(token);
    } catch (error) {
      if (error instanceof InvalidAccessTokenError) {
        return null;
      }
      throw error;
    }
  };
}

/** 返回一个要求具备全部指定 roles 的依赖。 */
export function requireRoles<T extends Iterable<string>>(
  validator: AccessTokenValidator,
  requiredRoles: NonStringIterable<T>,
): OidcDependency<HumanPrincipal> {
  const expected = sortedDistinct(requiredRoles, "requiredRoles");
  const principalDependency = requirePrincipal(validator);
  return async (source) => {
    const principal = await principalDependency(source);
    const roles = new Set(principal.roles);
    const missing = expected.filter((role) => !roles.has(role));
    if (missing.length > 0) {
      throw new MissingRoleError(`Missing required roles: ${missing.join(", ")}`);
    }
    return principal;
  };
}

/** 返回一个要求具备全部指定 scopes 的依赖。 */
export function requireScopes<T extends Iterable<string>>(
  validator: AccessTokenValidator,
  requiredScopes: NonStringIterable<T>,
): OidcDependency<HumanPrincipal> {
  const expected = sortedDistinct(requiredScopes, "requiredScopes");
  const principalDependency = requirePrincipal(validator);
  return async (source) => {
    const principal = await principalDependency(source);
    const scopes = new Set(principal.scopes);
    const missing = expected.filter((scope) => !scopes.has(scope));
    if (missing.length > 0) {
      throw new MissingScopeError(`Missing required scopes: ${missing.join(", ")}`);
    }
    return principal;
  };
}

/** 根据真人 principal 构造脱敏后的 AMP audit actor（不含 subject、principal_key、raw_claims）。 */
export function auditActorFromPrincipal(principal: HumanPrincipal): AuditActor {
  const actor: AuditActor = { id: principal.principal_id, type: "human" };
  const name = principal.name || principal.username || principal.email;
  if (name !== undefined) {
    actor.name = name;
  }
  const role = [...principal.roles].sort(compareCodePoints).join(",");
  if (role) {
    actor.role = role;
  }
  return actor;
}

/** OIDC 异常对应的 HTTP 响应。 */
export interface OidcHttpErrorResponse {
  statusCode: 401 | 403 | 503;
  /** 响应体 `{"detail": detail}` */
  detail: string;
  headers: Record<string, string>;
}

/**
 * 把依赖抛出的 OIDC 异常映射为 FastAPI 依赖会返回的响应；不是 OIDC 认证/授权/可用性异常时返回 `null`。
 *
 * - `OidcAuthenticationError`（缺 token、token 无效）→ 401，带 `WWW-Authenticate: Bearer`；
 * - `OidcAuthorizationError`（缺 role/scope）→ 403；
 * - `OidcProviderUnavailableError` → 503。
 */
export function oidcErrorToHttpResponse(error: unknown): OidcHttpErrorResponse | null {
  if (error instanceof OidcAuthenticationError) {
    return { statusCode: 401, detail: error.message, headers: { "WWW-Authenticate": "Bearer" } };
  }
  if (error instanceof OidcAuthorizationError) {
    return { statusCode: 403, detail: error.message, headers: {} };
  }
  if (error instanceof OidcProviderUnavailableError) {
    return { statusCode: 503, detail: error.message, headers: {} };
  }
  return null;
}

/** Express / `node:http` 兼容的最小响应形状。 */
export interface OidcMiddlewareResponse {
  statusCode: number;
  setHeader(name: string, value: string): unknown;
  end(chunk: string): unknown;
}

/** Express 兼容的最小请求形状。 */
export interface OidcMiddlewareRequest {
  headers: IncomingHttpHeaders;
}

/** 中间件成功后在请求对象上可读到的字段。 */
export interface OidcAuthenticatedRequest {
  principal?: HumanPrincipal | null;
}

export type OidcMiddleware = (
  req: OidcMiddlewareRequest,
  res: OidcMiddlewareResponse,
  next: (error?: unknown) => void,
) => void;

/** 把 `OidcHttpErrorResponse` 写成 FastAPI `HTTPException` 的 JSON 响应。 */
export function sendOidcErrorResponse(
  res: OidcMiddlewareResponse,
  response: OidcHttpErrorResponse,
): void {
  res.statusCode = response.statusCode;
  res.setHeader("Content-Type", "application/json");
  for (const [name, value] of Object.entries(response.headers)) {
    res.setHeader(name, value);
  }
  res.end(JSON.stringify({ detail: response.detail }));
}

/**
 * 在 promise 链之外调用 `next`：下游处理函数同步抛出的异常会成为普通的未捕获异常
 * （与 `node:http` 请求监听器里同步抛错一样；Express 自己会在 `next` 内部捕获它），
 * 而不会被这里的 promise 链接住后再调用一次 `next(error)`。
 */
function callNext(next: (error?: unknown) => void, args: [] | [unknown]): void {
  queueMicrotask(() => {
    next(...args);
  });
}

/**
 * Express 兼容中间件：`app.get("/admin", createOidcMiddleware(requireRoles(validator, ["ADMIN"])), handler)`。
 *
 * - 成功：设置 `req.principal`（`optionalPrincipal` 时可能为 `null`）后调用一次 `next()`；
 * - OIDC 认证 / 授权 / provider 不可用：写出 401 / 403 / 503 响应，不调用 `next`；写响应本身失败时调用 `next(error)`；
 * - 其它异常：调用 `next(error)`（Express 默认返回 500，等价于 FastAPI 的未处理异常）。
 *
 * 任何情况下 `next` 至多被调用一次。
 */
export function createOidcMiddleware(
  dependency: OidcDependency<HumanPrincipal | null>,
): OidcMiddleware {
  return (req, res, next) => {
    void dependency(req).then(
      (principal) => {
        (req as OidcMiddlewareRequest & OidcAuthenticatedRequest).principal = principal;
        callNext(next, []);
      },
      (error: unknown) => {
        const response = oidcErrorToHttpResponse(error);
        if (response === null) {
          callNext(next, [error]);
          return;
        }
        try {
          sendOidcErrorResponse(res, response);
        } catch (sendError) {
          callNext(next, [sendError]);
        }
      },
    );
  };
}
