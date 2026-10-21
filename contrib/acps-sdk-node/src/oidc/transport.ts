/**
 * OIDC 模块共用的 HTTP 辅助（内部模块）。
 *
 * OIDC 端点（discovery、JWKS、token、device authorization、revocation）不需要对端证书身份绑定，
 * 因此直接用全局 `fetch`；调用方可以注入自己的 `fetch`，测试用内存实现替换
 * （对应 Python 的 `httpx.MockTransport`）。
 */

/** 可注入的 fetch 函数。全局 `fetch` 与常见的 polyfill 都满足这个签名。 */
export type OidcFetch = (input: string, init?: RequestInit) => Promise<Response>;

/** 一次已经读完响应体的 HTTP 交互结果。 */
export interface OidcHttpResult {
  status: number;
  text: string;
}

/** Python 版默认的 httpx 超时（`OidcProviderConfig.http_timeout_seconds` 的默认值）。 */
export const DEFAULT_HTTP_TIMEOUT_SECONDS = 5;

/** Node 定时器的上限（2^31 − 1 毫秒，约 24.8 天）；更大的值会被 Node 改成 1 毫秒或直接抛 RangeError。 */
export const MAX_TIMEOUT_MS = 2_147_483_647;

/**
 * 把超时秒数换算成 `AbortSignal.timeout` 可接受的毫秒数：取整（Node 要求整数），并夹在 [1, MAX_TIMEOUT_MS]。
 */
export function timeoutMilliseconds(timeoutSeconds: number): number {
  const milliseconds = Math.round(timeoutSeconds * 1000);
  if (!Number.isFinite(milliseconds)) {
    return MAX_TIMEOUT_MS;
  }
  return Math.min(Math.max(milliseconds, 1), MAX_TIMEOUT_MS);
}

function defaultFetch(input: string, init?: RequestInit): Promise<Response> {
  return fetch(input, init);
}

/**
 * 发送请求并读完响应体。连接失败、超时、读取响应体失败都会以异常形式抛出，
 * 由调用方包装成对应的 OIDC 异常（对应 Python 捕获 `httpx.HTTPError`）。
 *
 * - `redirect: "manual"`：httpx 默认不跟随重定向，3xx 按非 2xx 处理；
 * - 超时对应 httpx 客户端的 `timeout`，但这里是整个请求（连接 + 读完响应体）的总时限，
 *   而 httpx 的 `timeout=5.0` 是连接、读、写、取连接池各阶段分别 5 秒。
 */
export async function sendHttpRequest(
  fetchImpl: OidcFetch | undefined,
  url: string,
  init: RequestInit,
  timeoutSeconds: number,
): Promise<OidcHttpResult> {
  const response = await (fetchImpl ?? defaultFetch)(url, {
    ...init,
    redirect: "manual",
    signal: AbortSignal.timeout(timeoutMilliseconds(timeoutSeconds)),
  });
  const text = await response.text();
  return { status: response.status, text };
}

/** 2xx 判断，对应 `200 <= status_code < 300`。 */
export function isSuccessStatus(status: number): boolean {
  return status >= 200 && status < 300;
}

/** 单调时钟（秒），对应 Python `time.monotonic()`。 */
export function monotonicSeconds(): number {
  return performance.now() / 1000;
}
