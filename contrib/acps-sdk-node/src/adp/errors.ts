/**
 * ADP (Agent Discovery Protocol) 错误码定义与错误处理工具（对应 Python `acps_sdk.adp.errors`）
 *
 * 基于 ACPs-spec-ADP 协议规范定义的错误码。
 */

import { z } from "zod";

// =============================================================================
// ADP 错误码枚举
// =============================================================================

/**
 * ADP 协议错误码枚举。
 *
 * 命名规则：HTTP 状态码前缀 + 两位序号。
 * - 307xx: 重定向相关
 * - 400xx: 客户端请求参数错误
 * - 401xx: 认证相关
 * - 429xx: 限流相关
 * - 508xx: 转发链路相关
 * - 500xx: 服务端内部错误
 *
 * Python 版 `ADPErrorCode` 是 `IntEnum`，成员上的 `is_redirect()` 等方法在 Node 版中对应
 * 独立函数 {@link isRedirectErrorCode}、{@link isRetryableErrorCode}、
 * {@link isClientErrorCode}、{@link isForwardErrorCode}。
 */
export const ADPErrorCode = {
  // ── 307 Temporary Redirect ──
  /** CapacityRedirect: 当前发现服务器负载/维护受限，307 重定向到同权威机构的候选服务器。 */
  CAPACITY_REDIRECT: 30701,
  /** RegionRedirect: 因地域或合规策略不覆盖该查询，307 指引到覆盖区域的发现服务器。 */
  REGION_REDIRECT: 30702,
  /** MaxRedirectsExceeded: 客户端连续重定向次数超过限制（建议最大 5 次），请求被拒绝。 */
  MAX_REDIRECTS_EXCEEDED: 30703,

  // ── 400 Bad Request ──
  /** MissingQuery: type=explicit 时缺少 query，或文本为空字符串。 */
  MISSING_QUERY: 40001,
  /** ForwardDepthLimitInvalid: forwardDepthLimit 不在 1-5 区间。 */
  FORWARD_DEPTH_LIMIT_INVALID: 40002,
  /** ForwardChainInvalid: 客户端携带的 forwardChain 包含非法 AIC。 */
  FORWARD_CHAIN_INVALID: 40003,
  /** FilterInvalid: filter 中的条件不合法（如不支持的字段路径、运算符与字段类型不兼容、嵌套深度超限），或条件互相矛盾。 */
  FILTER_INVALID: 40004,
  /** ForwardFanoutLimitInvalid: forwardFanoutLimit 不在 1-5 区间，或小于 1。 */
  FORWARD_FANOUT_LIMIT_INVALID: 40005,

  // ── 401 Unauthorized ──
  /** CertificateInvalid: mTLS 证书无效、过期或被吊销。 */
  CERTIFICATE_INVALID: 40101,

  // ── 429 Too Many Requests ──
  /** CallerRateLimited: 针对单个请求智能体的调用频率超限，需遵守 Retry-After。 */
  CALLER_RATE_LIMITED: 42901,
  /** TenantRateLimited: 上层租户/组织的配额已用尽，需遵守 Retry-After。 */
  TENANT_RATE_LIMITED: 42902,

  // ── 508 Loop Detected ──
  /** ForwardLoopDetected: 在 forwardChain 中发现自身 AIC，判定存在环路。 */
  FORWARD_LOOP_DETECTED: 50801,
  /** ForwardDepthExceeded: 转发深度达到 forwardDepthLimit，不再继续转发。 */
  FORWARD_DEPTH_EXCEEDED: 50802,
  /** ForwardChainTampered: 校验 forwardChain 最后一个 AIC 与当前请求证书内嵌 AIC 不同，链条完整性受到破坏。 */
  FORWARD_CHAIN_TAMPERED: 50803,
  /** ForwardFanoutExceeded: 聚合转发分支数量超过剩余额度，或剩余额度分配之和超出 forwardFanoutLimit。 */
  FORWARD_FANOUT_EXCEEDED: 50804,
  /** ForwardSignatureInvalid: forwardSignatures 中的签名验证失败，可能被篡改或签名不匹配。 */
  FORWARD_SIGNATURE_INVALID: 50805,

  // ── 500 Internal Server Error ──
  /** InternalError: 发现服务器自身异常，无法完成查询。 */
  INTERNAL_ERROR: 50001,
} as const;
export type ADPErrorCode = (typeof ADPErrorCode)[keyof typeof ADPErrorCode];
export const ADPErrorCodeSchema = z.enum(ADPErrorCode);

const KNOWN_ERROR_CODES: ReadonlySet<number> = new Set<number>(Object.values(ADPErrorCode));

/**
 * 判断整数是否为已定义的 ADP 错误码（对应 Python `ADPErrorCode(value)` 是否成功）。
 */
export function isADPErrorCode(code: number): code is ADPErrorCode {
  return KNOWN_ERROR_CODES.has(code);
}

/**
 * 判断该错误码是否表示重定向（30701, 30702）。
 *
 * 对应 Python `ADPErrorCode.is_redirect()`；未定义的错误码返回 false（与 `ADPError.is_redirect()` 一致）。
 */
export function isRedirectErrorCode(code: number): boolean {
  return isADPErrorCode(code) && (code === 30701 || code === 30702);
}

/**
 * 判断该错误码是否表示可重试（限流类: 42901, 42902）。
 *
 * 对应 Python `ADPErrorCode.is_retryable()`；未定义的错误码返回 false。
 */
export function isRetryableErrorCode(code: number): boolean {
  return isADPErrorCode(code) && (code === 42901 || code === 42902);
}

/**
 * 判断该错误码是否为客户端参数错误（400xx）。
 *
 * 对应 Python `ADPErrorCode.is_client_error()`；未定义的错误码返回 false。
 */
export function isClientErrorCode(code: number): boolean {
  return isADPErrorCode(code) && code >= 40000 && code < 40100;
}

/**
 * 判断该错误码是否为转发链路错误（508xx）。
 *
 * 对应 Python `ADPErrorCode.is_forward_error()`；未定义的错误码返回 false。
 */
export function isForwardErrorCode(code: number): boolean {
  return isADPErrorCode(code) && code >= 50800 && code < 50900;
}

// =============================================================================
// 错误码 → 友好名称映射
// =============================================================================

export const ADP_ERROR_NAMES: Readonly<Record<number, string>> = Object.freeze({
  [ADPErrorCode.CAPACITY_REDIRECT]: "CapacityRedirect",
  [ADPErrorCode.REGION_REDIRECT]: "RegionRedirect",
  [ADPErrorCode.MAX_REDIRECTS_EXCEEDED]: "MaxRedirectsExceeded",
  [ADPErrorCode.MISSING_QUERY]: "MissingQuery",
  [ADPErrorCode.FORWARD_DEPTH_LIMIT_INVALID]: "ForwardDepthLimitInvalid",
  [ADPErrorCode.FORWARD_CHAIN_INVALID]: "ForwardChainInvalid",
  [ADPErrorCode.FILTER_INVALID]: "FilterInvalid",
  [ADPErrorCode.FORWARD_FANOUT_LIMIT_INVALID]: "ForwardFanoutLimitInvalid",
  [ADPErrorCode.CERTIFICATE_INVALID]: "CertificateInvalid",
  [ADPErrorCode.CALLER_RATE_LIMITED]: "CallerRateLimited",
  [ADPErrorCode.TENANT_RATE_LIMITED]: "TenantRateLimited",
  [ADPErrorCode.FORWARD_LOOP_DETECTED]: "ForwardLoopDetected",
  [ADPErrorCode.FORWARD_DEPTH_EXCEEDED]: "ForwardDepthExceeded",
  [ADPErrorCode.FORWARD_CHAIN_TAMPERED]: "ForwardChainTampered",
  [ADPErrorCode.FORWARD_FANOUT_EXCEEDED]: "ForwardFanoutExceeded",
  [ADPErrorCode.FORWARD_SIGNATURE_INVALID]: "ForwardSignatureInvalid",
  [ADPErrorCode.INTERNAL_ERROR]: "InternalError",
});

// =============================================================================
// 错误码 → HTTP 状态码映射
// =============================================================================

export const ADP_ERROR_HTTP_STATUS: Readonly<Record<number, number>> = Object.freeze({
  [ADPErrorCode.CAPACITY_REDIRECT]: 307,
  [ADPErrorCode.REGION_REDIRECT]: 307,
  [ADPErrorCode.MAX_REDIRECTS_EXCEEDED]: 400,
  [ADPErrorCode.MISSING_QUERY]: 400,
  [ADPErrorCode.FORWARD_DEPTH_LIMIT_INVALID]: 400,
  [ADPErrorCode.FORWARD_CHAIN_INVALID]: 400,
  [ADPErrorCode.FILTER_INVALID]: 400,
  [ADPErrorCode.FORWARD_FANOUT_LIMIT_INVALID]: 400,
  [ADPErrorCode.CERTIFICATE_INVALID]: 401,
  [ADPErrorCode.CALLER_RATE_LIMITED]: 429,
  [ADPErrorCode.TENANT_RATE_LIMITED]: 429,
  [ADPErrorCode.FORWARD_LOOP_DETECTED]: 508,
  [ADPErrorCode.FORWARD_DEPTH_EXCEEDED]: 508,
  [ADPErrorCode.FORWARD_CHAIN_TAMPERED]: 508,
  [ADPErrorCode.FORWARD_FANOUT_EXCEEDED]: 508,
  [ADPErrorCode.FORWARD_SIGNATURE_INVALID]: 508,
  [ADPErrorCode.INTERNAL_ERROR]: 500,
});

function lookup<V>(table: Readonly<Record<number, V>>, code: number): V | undefined {
  return Object.hasOwn(table, code) ? table[code] : undefined;
}

// =============================================================================
// ADP 异常类
// =============================================================================

/** ADP 响应体中的 error 对象结构（CommonResponse.error）。 */
export interface ADPErrorBody {
  code: number;
  message: string;
  data?: unknown;
}

/** 完整的 ADP 错误响应体（CommonResponse，仅含 error 字段）。 */
export interface ADPErrorResponseBody {
  error: ADPErrorBody;
}

/**
 * ADP 协议异常基类。
 *
 * 属性与 Python 版对应：`code`（ADP 错误码）、`message`（错误描述）、
 * `data`（可选的附加错误数据）、`httpStatus`（对应的 HTTP 状态码，Python 为 `http_status`）。
 */
export class ADPError extends Error {
  /** ADP 错误码；未定义的错误码保持原始整数值。 */
  readonly code: number;
  /** 可选的附加错误数据。 */
  readonly data: unknown;
  /** 对应的 HTTP 状态码，未知错误码为 500。 */
  readonly httpStatus: number;

  /**
   * @param code ADP 错误码
   * @param message 错误描述；缺省（或空字符串）时使用错误码对应的默认名称，未知错误码为 "UnknownError"
   * @param data 可选的附加错误数据
   */
  constructor(code: number, message?: string | null, data?: unknown) {
    super(message || (lookup(ADP_ERROR_NAMES, code) ?? "UnknownError"));
    this.name = "ADPError";
    this.code = code;
    this.data = data ?? undefined;
    this.httpStatus = lookup(ADP_ERROR_HTTP_STATUS, code) ?? 500;
  }

  /** 转换为 ADP 响应体中的 error 对象结构（符合 CommonResponse.error）。 */
  toErrorBody(): ADPErrorBody {
    const error: ADPErrorBody = { code: Math.trunc(this.code), message: this.message };
    if (this.data !== undefined && this.data !== null) {
      error.data = this.data;
    }
    return error;
  }

  /** 转换为完整的 ADP 错误响应体（符合 CommonResponse，包含 error 字段）。 */
  toResponseDict(): ADPErrorResponseBody {
    return { error: this.toErrorBody() };
  }

  /** 判断该错误是否表示重定向。 */
  isRedirect(): boolean {
    return isRedirectErrorCode(this.code);
  }

  /** 判断该错误是否可重试（限流类）。 */
  isRetryable(): boolean {
    return isRetryableErrorCode(this.code);
  }

  /** 判断该错误是否为客户端参数错误。 */
  isClientError(): boolean {
    return isClientErrorCode(this.code);
  }

  /** 判断该错误是否为转发链路错误。 */
  isForwardError(): boolean {
    return isForwardErrorCode(this.code);
  }
}

// =============================================================================
// 便捷工厂函数
// =============================================================================

/**
 * 快速构建 ADP 错误响应体。
 *
 * @param code ADP 错误码
 * @param message 可选的错误描述，缺省使用错误码对应的默认名称
 * @param data 可选的附加错误数据
 *
 * @example
 * ```ts
 * makeErrorResponse(ADPErrorCode.MISSING_QUERY);
 * // { error: { code: 40001, message: "MissingQuery" } }
 *
 * makeErrorResponse(ADPErrorCode.FORWARD_FANOUT_EXCEEDED, "Fan-out budget exhausted", {
 *   availableBudget: 1,
 *   requiredBranches: 3,
 * });
 * // { error: { code: 50804, message: "Fan-out budget exhausted", data: { availableBudget: 1, requiredBranches: 3 } } }
 * ```
 */
export function makeErrorResponse(
  code: number,
  message?: string | null,
  data?: unknown,
): ADPErrorResponseBody {
  return new ADPError(code, message, data).toResponseDict();
}

/** 获取 ADP 错误码对应的 HTTP 状态码；未知错误码返回 500。 */
export function getHttpStatusForError(code: number): number {
  return lookup(ADP_ERROR_HTTP_STATUS, code) ?? 500;
}
