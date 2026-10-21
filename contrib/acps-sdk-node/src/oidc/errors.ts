/**
 * OIDC 校验相关的异常层级（对应 Python `acps_sdk.oidc.errors`）。
 *
 * 每个类都设置了 `name`，便于日志与 `instanceof` 之外的判断；Python 的 `raise X from exc`
 * 对应 Node 的 `new X(message, { cause: exc })`。
 */

/** OIDC 相关异常的基类。 */
export class OidcError extends Error {
  override name = "OidcError";
}

/** 当 bearer token 缺失或无效时抛出。 */
export class OidcAuthenticationError extends OidcError {
  override name = "OidcAuthenticationError";
}

/** 当 principal 缺少所需权限时抛出。 */
export class OidcAuthorizationError extends OidcError {
  override name = "OidcAuthorizationError";
}

/** `OidcClientError` 的附加字段，对应 Python 的关键字参数。 */
export interface OidcClientErrorOptions extends ErrorOptions {
  /** OAuth 2.0 错误码，例如 `invalid_grant` */
  error?: string | null;
  /** OAuth 2.0 `error_description` */
  errorDescription?: string | null;
  /** HTTP 状态码 */
  statusCode?: number | null;
}

/**
 * 当 OIDC 客户端侧协议或 HTTP 交互失败时抛出。
 *
 * Python 版的 `str(exc)` 会把错误码、描述和状态码拼进文本（`msg; error=…; description=…; status_code=…`），
 * 下游 CLI 直接展示 `str(exc)`；Node 版让 `message` 等于这段拼接文本，原始消息保存在 `rawMessage`
 * （对应 Python 的 `exc.message` 属性）。
 */
export class OidcClientError extends OidcError {
  override name = "OidcClientError";
  /** 未拼接附加字段的原始消息，对应 Python 的 `exc.message` */
  readonly rawMessage: string;
  readonly error: string | null;
  readonly errorDescription: string | null;
  readonly statusCode: number | null;

  constructor(message: string, options: OidcClientErrorOptions = {}) {
    const error = options.error ?? null;
    const errorDescription = options.errorDescription ?? null;
    const statusCode = options.statusCode ?? null;
    const parts = [message];
    if (error) parts.push(`error=${error}`);
    if (errorDescription) parts.push(`description=${errorDescription}`);
    if (statusCode !== null) parts.push(`status_code=${statusCode}`);
    super(parts.join("; "), options.cause === undefined ? undefined : { cause: options.cause });
    this.rawMessage = message;
    this.error = error;
    this.errorDescription = errorDescription;
    this.statusCode = statusCode;
  }
}

/**
 * discovery 或 JWKS 不可用时抛出：加载失败（网络错误、超时、非 2xx、非 JSON、文档不合法、issuer 不符）
 * 且没有可兜底的缓存，或 provider 配置违反 `require_https`。
 *
 * Python 版的 docstring 也写着“没有可用缓存时”，但 Python 版其实从不在加载失败时使用缓存；
 * Node 版会在 `maxStaleSeconds` 内继续使用过期文档，超出后才抛出本异常。
 */
export class OidcProviderUnavailableError extends OidcError {
  override name = "OidcProviderUnavailableError";
}

/** 当请求里没有 bearer token 时抛出。 */
export class MissingBearerTokenError extends OidcAuthenticationError {
  override name = "MissingBearerTokenError";
}

/** 当 bearer token 未通过 access-token 校验时抛出。 */
export class InvalidAccessTokenError extends OidcAuthenticationError {
  override name = "InvalidAccessTokenError";
}

/** 当 provider 未暴露 Device Authorization endpoint 时抛出。 */
export class OidcDeviceAuthorizationNotSupportedError extends OidcClientError {
  override name = "OidcDeviceAuthorizationNotSupportedError";
}

/** 当终端用户拒绝授权时抛出。 */
export class OidcDeviceAuthorizationDeniedError extends OidcClientError {
  override name = "OidcDeviceAuthorizationDeniedError";
}

/** 当 device_code 已过期时抛出。 */
export class OidcDeviceAuthorizationExpiredError extends OidcClientError {
  override name = "OidcDeviceAuthorizationExpiredError";
}

/** 当 principal 缺少 required roles 时抛出。 */
export class MissingRoleError extends OidcAuthorizationError {
  override name = "MissingRoleError";
}

/** 当 principal 缺少 required scopes 时抛出。 */
export class MissingScopeError extends OidcAuthorizationError {
  override name = "MissingScopeError";
}
