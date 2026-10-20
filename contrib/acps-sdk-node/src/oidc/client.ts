/**
 * OIDC Device Authorization Grant 协议 helper（对应 Python `acps_sdk.oidc.client`）。
 *
 * 与 Python 版的差异：Python 的 `OidcDeviceClient` 基于同步 `httpx.Client`；Node 没有同步 HTTP，
 * 所有网络方法返回 `Promise`，其余行为（请求字段、错误分类与文本、轮询状态）一致。
 */

import { inspect } from "node:util";

import { z } from "zod";

import { optional } from "../internal/schema.js";
import { enforceHttps, fetchDiscoveryDocument, type OidcDiscoveryDocument } from "./discovery.js";
import {
  OidcClientError,
  OidcDeviceAuthorizationDeniedError,
  OidcDeviceAuthorizationExpiredError,
  OidcDeviceAuthorizationNotSupportedError,
} from "./errors.js";
import {
  getOwn,
  isPlainObject,
  iterPython,
  pyRstripSlash,
  pySplitWhitespace,
  pyStr,
  pyStrip,
} from "../internal/pyCompat.js";
import {
  isSuccessStatus,
  sendHttpRequest,
  type OidcFetch,
  type OidcHttpResult,
} from "./transport.js";

// =============================================================================
// SecretStr
// =============================================================================

const MASK = "**********";

/**
 * pydantic `SecretStr` 的对应物：`toString()`、`JSON.stringify`、`util.inspect` 都只输出掩码，
 * 原值只能通过 `getSecretValue()` 取得。
 */
export class SecretStr {
  readonly #value: string;

  constructor(value: string) {
    this.#value = value;
  }

  getSecretValue(): string {
    return this.#value;
  }

  toString(): string {
    return this.#value ? MASK : "";
  }

  toJSON(): string {
    return this.toString();
  }

  [inspect.custom](): string {
    return `SecretStr('${this.toString()}')`;
  }
}

const SecretStrSchema = z.union([
  z.instanceof(SecretStr),
  z.string().transform((value) => new SecretStr(value)),
]);

// =============================================================================
// 配置与响应模型
// =============================================================================

/** OIDC Device Grant 客户端配置。 */
export const OidcDeviceClientConfigSchema = z.strictObject({
  issuer: z.string().transform((value, ctx) => {
    const issuer = pyRstripSlash(pyStrip(value));
    if (!issuer) {
      ctx.addIssue({ code: "custom", message: "issuer must not be empty" });
      return z.NEVER;
    }
    return issuer;
  }),
  client_id: z.string().transform((value, ctx) => {
    const clientId = pyStrip(value);
    if (!clientId) {
      ctx.addIssue({ code: "custom", message: "client_id must not be empty" });
      return z.NEVER;
    }
    return clientId;
  }),
  scopes: (
    z.unknown().transform((value, ctx) => {
      let values: string[];
      if (value === null) {
        return [];
      } else if (typeof value === "string") {
        values = pySplitWhitespace(value);
      } else {
        const items =
          value instanceof Uint8Array || value instanceof ArrayBuffer ? null : iterPython(value);
        if (items === null) {
          ctx.addIssue({
            code: "custom",
            message: "scopes must be a string or iterable of strings",
          });
          return z.NEVER;
        }
        values = items.map(pyStr);
      }
      const seen = new Set<string>();
      const result: string[] = [];
      for (const rawValue of values) {
        const scope = pyStrip(rawValue);
        if (!scope || seen.has(scope)) continue;
        seen.add(scope);
        result.push(scope);
      }
      return result;
    }) as unknown as z.ZodType<string[], string | Iterable<unknown> | null>
  ).default(() => []),
  http_timeout_seconds: z
    .number()
    .refine((value) => value > 0, "http_timeout_seconds must be > 0")
    .default(5),
  require_https: z.boolean().default(true),
});
export type OidcDeviceClientConfig = z.output<typeof OidcDeviceClientConfigSchema>;
export type OidcDeviceClientConfigInput = z.input<typeof OidcDeviceClientConfigSchema>;

const positiveInt = z
  .number()
  .int()
  .refine((value) => value > 0, "value must be > 0");

/** Device Authorization endpoint 响应（`extra="allow"`：其它字段原样保留）。 */
export const OidcDeviceAuthorizationResponseSchema = z.looseObject({
  device_code: SecretStrSchema,
  user_code: z.string(),
  verification_uri: z.string(),
  verification_uri_complete: optional(z.string()),
  expires_in: positiveInt,
  interval: positiveInt.default(5),
  expires_at: z.union([z.date(), z.iso.datetime({ offset: true }).transform((v) => new Date(v))]),
});
export type OidcDeviceAuthorizationResponse = z.output<
  typeof OidcDeviceAuthorizationResponseSchema
>;
export type OidcDeviceAuthorizationResponseInput = z.input<
  typeof OidcDeviceAuthorizationResponseSchema
>;

const nonNegativeOptionalInt = optional(
  z
    .number()
    .int()
    .refine((value) => value >= 0, "value must be >= 0"),
);

/**
 * Token endpoint 成功响应（`extra="allow"`：其它字段原样保留）。
 * `OidcTokenResponseSchema.parse(input)` 对应 Python 的 `OidcTokenResponse(...)` / `model_validate`。
 */
export const OidcTokenResponseSchema = z.looseObject({
  access_token: SecretStrSchema,
  token_type: z
    .unknown()
    .optional()
    .transform((value, ctx) => {
      if (value === undefined) {
        return "Bearer" as const;
      }
      const rawValue = value === null || value === "" ? "Bearer" : pyStrip(pyStr(value));
      if (rawValue.toLowerCase() !== "bearer") {
        ctx.addIssue({ code: "custom", message: "token_type must be Bearer" });
        return z.NEVER;
      }
      return "Bearer" as const;
    }),
  expires_in: nonNegativeOptionalInt,
  refresh_token: optional(SecretStrSchema),
  refresh_expires_in: nonNegativeOptionalInt,
  scope: optional(z.string()),
  id_token: optional(SecretStrSchema),
});
export type OidcTokenResponse = z.output<typeof OidcTokenResponseSchema>;
export type OidcTokenResponseInput = z.input<typeof OidcTokenResponseSchema>;

/**
 * OidcTokenResponse 的方法（对应 Python `OidcTokenResponse` 上的 `access_token_value` 等属性）。
 * token 字段是 `SecretStr`，`JSON.stringify` 与 `util.inspect` 不会泄露原值。
 */
export const OidcTokenResponse = {
  accessTokenValue(response: OidcTokenResponse): string {
    return response.access_token.getSecretValue();
  },

  refreshTokenValue(response: OidcTokenResponse): string | null {
    return response.refresh_token === undefined ? null : response.refresh_token.getSecretValue();
  },

  idTokenValue(response: OidcTokenResponse): string | null {
    return response.id_token === undefined ? null : response.id_token.getSecretValue();
  },
};

/** Device token polling 的非终止状态。 */
export const OidcDeviceTokenPollingStatus = {
  SUCCESS: "success",
  AUTHORIZATION_PENDING: "authorization_pending",
  SLOW_DOWN: "slow_down",
} as const;
export type OidcDeviceTokenPollingStatus =
  (typeof OidcDeviceTokenPollingStatus)[keyof typeof OidcDeviceTokenPollingStatus];
export const OidcDeviceTokenPollingStatusSchema = z.enum(
  Object.values(OidcDeviceTokenPollingStatus) as [
    OidcDeviceTokenPollingStatus,
    ...OidcDeviceTokenPollingStatus[],
  ],
);

/** 单次轮询 token endpoint 的结果。 */
export const OidcDeviceTokenPollingResultSchema = z.strictObject({
  status: OidcDeviceTokenPollingStatusSchema,
  interval: z
    .number()
    .int()
    .refine((value) => value > 0, "interval must be > 0"),
  token_response: optional(OidcTokenResponseSchema),
});
export type OidcDeviceTokenPollingResult = z.output<typeof OidcDeviceTokenPollingResultSchema>;
export type OidcDeviceTokenPollingResultInput = z.input<typeof OidcDeviceTokenPollingResultSchema>;

/** Token revocation 的结构化结果。 */
export const OidcTokenRevocationResultSchema = z.strictObject({
  attempted: z.boolean(),
  revoked: z.boolean(),
  reason: optional(z.string()),
});
export type OidcTokenRevocationResult = z.output<typeof OidcTokenRevocationResultSchema>;

// =============================================================================
// OidcDeviceClient
// =============================================================================

/** 构造 `OidcDeviceClient` 的选项，对应 Python 的关键字参数。 */
export interface OidcDeviceClientOptions {
  /** 自定义 fetch，默认全局 `fetch`（对应 Python 注入的 `httpx.Client`） */
  fetch?: OidcFetch;
}

type OAuthError = { error?: string; error_description?: string };

const DISCOVERY_ENDPOINT_FIELDS = [
  "authorization_endpoint",
  "device_authorization_endpoint",
  "token_endpoint",
  "revocation_endpoint",
  "end_session_endpoint",
] as const;

/** Python `isinstance(value, int)`（bool 是 int 的子类）。 */
function asPyInt(value: unknown): number | null {
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "number" && Number.isInteger(value)) return value;
  return null;
}

/** OIDC Device Authorization Grant 客户端（公开客户端，不发送 client_secret）。 */
export class OidcDeviceClient {
  private readonly clientConfig: OidcDeviceClientConfig;
  private readonly fetchImpl: OidcFetch | undefined;
  private discoveryDocument: OidcDiscoveryDocument | null = null;

  constructor(config: OidcDeviceClientConfigInput, options: OidcDeviceClientOptions = {}) {
    this.clientConfig = OidcDeviceClientConfigSchema.parse(config);
    this.fetchImpl = options.fetch;
  }

  /** 与 Python 版对齐的生命周期方法；全局 fetch 没有需要释放的连接池，因此为空操作。 */
  close(): void {
    // 无需释放资源
  }

  get config(): OidcDeviceClientConfig {
    return this.clientConfig;
  }

  /** 读取并缓存 discovery document（同一个 client 只加载一次）。 */
  async getDiscoveryDocument(): Promise<OidcDiscoveryDocument> {
    if (this.discoveryDocument === null) {
      this.discoveryDocument = await this.fetchDiscoveryDocument();
    }
    return this.discoveryDocument;
  }

  /** 发起 Device Authorization 请求。 */
  async startDeviceAuthorization(): Promise<OidcDeviceAuthorizationResponse> {
    const document = await this.getDiscoveryDocument();
    const endpoint = this.requiredEndpoint(
      document.device_authorization_endpoint,
      "device_authorization_endpoint",
      "OIDC provider does not expose device_authorization_endpoint",
      OidcDeviceAuthorizationNotSupportedError,
    );
    const payload: Record<string, string> = { client_id: this.clientConfig.client_id };
    const scope = this.clientConfig.scopes.join(" ");
    if (scope) {
      payload.scope = scope;
    }
    const responsePayload = await this.postForm(endpoint, payload, "device authorization endpoint");
    const expiresIn = this.requiredPositiveInt(responsePayload, "expires_in");
    const interval = this.optionalPositiveInt(responsePayload, "interval", 5);
    const parsed = OidcDeviceAuthorizationResponseSchema.safeParse({
      ...responsePayload,
      expires_in: expiresIn,
      interval,
      expires_at: new Date(Date.now() + expiresIn * 1000),
    });
    if (!parsed.success) {
      throw new OidcClientError("OIDC device authorization response is invalid", {
        cause: parsed.error,
      });
    }
    return parsed.data;
  }

  /** 轮询一次 token endpoint。`authorization_pending` / `slow_down` 返回结构化结果，其它错误抛异常。 */
  async pollDeviceToken(
    deviceCode: string,
    options: { interval?: number } = {},
  ): Promise<OidcDeviceTokenPollingResult> {
    const interval = options.interval ?? 5;
    const document = await this.getDiscoveryDocument();
    const endpoint = this.requiredEndpoint(
      document.token_endpoint,
      "token_endpoint",
      "OIDC provider does not expose token_endpoint",
      OidcClientError,
    );
    const response = await this.requestForm(endpoint, {
      grant_type: "urn:ietf:params:oauth:grant-type:device_code",
      client_id: this.clientConfig.client_id,
      device_code: deviceCode,
    });
    if (isSuccessStatus(response.status)) {
      const tokenResponse = this.parseTokenResponse(response);
      return OidcDeviceTokenPollingResultSchema.parse({
        status: OidcDeviceTokenPollingStatus.SUCCESS,
        interval,
        token_response: tokenResponse,
      });
    }

    const oauthError = this.parseOauthError(response);
    const errorCode = oauthError.error;
    const errorDescription = oauthError.error_description;
    if (errorCode === OidcDeviceTokenPollingStatus.AUTHORIZATION_PENDING) {
      return OidcDeviceTokenPollingResultSchema.parse({
        status: OidcDeviceTokenPollingStatus.AUTHORIZATION_PENDING,
        interval,
      });
    }
    if (errorCode === OidcDeviceTokenPollingStatus.SLOW_DOWN) {
      return OidcDeviceTokenPollingResultSchema.parse({
        status: OidcDeviceTokenPollingStatus.SLOW_DOWN,
        interval: interval + 5,
      });
    }
    if (errorCode === "access_denied") {
      throw new OidcDeviceAuthorizationDeniedError("OIDC device authorization was denied", {
        error: errorCode,
        errorDescription,
        statusCode: response.status,
      });
    }
    if (errorCode === "expired_token") {
      throw new OidcDeviceAuthorizationExpiredError("OIDC device authorization expired", {
        error: errorCode,
        errorDescription,
        statusCode: response.status,
      });
    }
    throw this.buildOauthError("OIDC device token polling failed", response, oauthError);
  }

  /** 用 refresh_token 换取新 token。 */
  async refreshToken(refreshToken: string): Promise<OidcTokenResponse> {
    const document = await this.getDiscoveryDocument();
    const endpoint = this.requiredEndpoint(
      document.token_endpoint,
      "token_endpoint",
      "OIDC provider does not expose token_endpoint",
      OidcClientError,
    );
    const response = await this.requestForm(endpoint, {
      grant_type: "refresh_token",
      client_id: this.clientConfig.client_id,
      refresh_token: refreshToken,
    });
    if (isSuccessStatus(response.status)) {
      return this.parseTokenResponse(response);
    }
    throw this.buildOauthError(
      "OIDC refresh token request failed",
      response,
      this.parseOauthError(response),
    );
  }

  /** 撤销 token；provider 未暴露 revocation endpoint 时返回 `attempted: false`。 */
  async revokeToken(
    token: string,
    options: { tokenTypeHint?: string | null } = {},
  ): Promise<OidcTokenRevocationResult> {
    const document = await this.getDiscoveryDocument();
    if (!document.revocation_endpoint) {
      return OidcTokenRevocationResultSchema.parse({
        attempted: false,
        revoked: false,
        reason: "revocation_endpoint_unavailable",
      });
    }
    const endpoint = this.requiredEndpoint(
      document.revocation_endpoint,
      "revocation_endpoint",
      "OIDC provider does not expose revocation_endpoint",
      OidcClientError,
    );
    const payload: Record<string, string> = {
      client_id: this.clientConfig.client_id,
      token,
    };
    if (options.tokenTypeHint) {
      payload.token_type_hint = options.tokenTypeHint;
    }
    const response = await this.requestForm(endpoint, payload);
    if (isSuccessStatus(response.status)) {
      return OidcTokenRevocationResultSchema.parse({ attempted: true, revoked: true });
    }
    throw this.buildOauthError(
      "OIDC token revocation failed",
      response,
      this.parseOauthError(response),
    );
  }

  private async fetchDiscoveryDocument(): Promise<OidcDiscoveryDocument> {
    const { issuer, require_https: requireHttps } = this.clientConfig;
    const document = await fetchDiscoveryDocument({
      issuer,
      requireHttps,
      fetch: this.fetchImpl,
      httpTimeoutSeconds: this.clientConfig.http_timeout_seconds,
    });
    for (const endpointName of DISCOVERY_ENDPOINT_FIELDS) {
      const endpoint = document[endpointName];
      if (endpoint) {
        enforceHttps(requireHttps, endpoint, endpointName);
      }
    }
    return document;
  }

  private requiredEndpoint(
    endpoint: string | undefined,
    kind: string,
    unsupportedMessage: string,
    ErrorClass: new (message: string) => OidcClientError,
  ): string {
    if (!endpoint) {
      throw new ErrorClass(unsupportedMessage);
    }
    enforceHttps(this.clientConfig.require_https, endpoint, kind);
    return endpoint;
  }

  private async requestForm(url: string, payload: Record<string, string>): Promise<OidcHttpResult> {
    try {
      return await sendHttpRequest(
        this.fetchImpl,
        url,
        {
          method: "POST",
          headers: {
            Accept: "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
          },
          body: new URLSearchParams(payload).toString(),
        },
        this.clientConfig.http_timeout_seconds,
      );
    } catch (error) {
      throw new OidcClientError(`OIDC request to ${url} failed`, { cause: error });
    }
  }

  private async postForm(
    url: string,
    payload: Record<string, string>,
    endpointName: string,
  ): Promise<Record<string, unknown>> {
    const response = await this.requestForm(url, payload);
    if (isSuccessStatus(response.status)) {
      return this.parseJsonObject(response, `OIDC ${endpointName} response is invalid`);
    }
    throw this.buildOauthError(
      `OIDC ${endpointName} request failed`,
      response,
      this.parseOauthError(response),
    );
  }

  private parseTokenResponse(response: OidcHttpResult): OidcTokenResponse {
    const payload = this.parseJsonObject(response, "OIDC token response is invalid");
    const parsed = OidcTokenResponseSchema.safeParse(payload);
    if (!parsed.success) {
      throw new OidcClientError("OIDC token response is invalid", { cause: parsed.error });
    }
    return parsed.data;
  }

  private parseOauthError(response: OidcHttpResult): OAuthError {
    const payload = this.tryParseJsonObject(response);
    if (payload === null) {
      return {};
    }
    const error = getOwn(payload, "error");
    const errorDescription = getOwn(payload, "error_description");
    const result: OAuthError = {};
    if (typeof error === "string" && pyStrip(error)) {
      result.error = pyStrip(error);
    }
    if (typeof errorDescription === "string" && pyStrip(errorDescription)) {
      result.error_description = pyStrip(errorDescription);
    }
    return result;
  }

  private tryParseJsonObject(response: OidcHttpResult): Record<string, unknown> | null {
    let payload: unknown;
    try {
      payload = JSON.parse(response.text);
    } catch {
      return null;
    }
    return isPlainObject(payload) ? payload : null;
  }

  private parseJsonObject(response: OidcHttpResult, message: string): Record<string, unknown> {
    const payload = this.tryParseJsonObject(response);
    if (payload === null) {
      throw new OidcClientError(message, { statusCode: response.status });
    }
    return payload;
  }

  private buildOauthError(
    message: string,
    response: OidcHttpResult,
    oauthError: OAuthError,
  ): OidcClientError {
    return new OidcClientError(message, {
      error: oauthError.error,
      errorDescription: oauthError.error_description,
      statusCode: response.status,
    });
  }

  private requiredPositiveInt(payload: Record<string, unknown>, fieldName: string): number {
    const value = asPyInt(getOwn(payload, fieldName));
    if (value === null || value <= 0) {
      throw new OidcClientError(`OIDC response field ${fieldName} must be a positive integer`);
    }
    return value;
  }

  private optionalPositiveInt(
    payload: Record<string, unknown>,
    fieldName: string,
    defaultValue: number,
  ): number {
    const raw = Object.hasOwn(payload, fieldName) ? payload[fieldName] : defaultValue;
    const value = asPyInt(raw);
    if (value === null || value <= 0) {
      throw new OidcClientError(`OIDC response field ${fieldName} must be a positive integer`);
    }
    return value;
  }
}
