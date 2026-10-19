/**
 * 最小 HTTP(S) 客户端传输层。
 *
 * 为什么不用全局 fetch：AIP 身份绑定需要读取服务端 TLS 证书（CN / `URI:acps://` SAN），
 * fetch 不暴露底层 socket；`node:https` 可以在 `response` 事件时拿到 `socket.getPeerCertificate()`。
 *
 * `HttpTransport` 是可注入的函数类型，测试可以用内存实现替换（对应 Python 的 `httpx.MockTransport`）。
 */

import { request as httpRequest, type IncomingHttpHeaders, type IncomingMessage } from "node:http";
import { request as httpsRequest } from "node:https";
import type { TLSSocket } from "node:tls";

import type { PeerCertificateLike } from "../aip/identity.js";

/** 客户端 TLS 选项，对应 Python 的 `ssl.SSLContext`。 */
export interface TlsClientOptions {
  /** 客户端证书（PEM），mTLS 时必填 */
  cert?: string | Buffer;
  /** 客户端私钥（PEM），mTLS 时必填 */
  key?: string | Buffer;
  /** 信任的 CA（PEM，可多个） */
  ca?: string | Buffer | Array<string | Buffer>;
  /** SNI / 主机名校验使用的名称 */
  servername?: string;
  /** 是否校验服务端证书链，默认 true */
  rejectUnauthorized?: boolean;
}

export interface HttpRequestOptions {
  url: string;
  method: string;
  headers?: Record<string, string>;
  body?: string | Buffer;
  /** 超时（毫秒），默认 30000，对应 Python httpx 的 `timeout=30.0` */
  timeoutMs?: number;
  /** 取消信号 */
  signal?: AbortSignal;
}

export interface HttpResponseData {
  status: number;
  headers: IncomingHttpHeaders;
  body: Buffer;
  /** 服务端证书；非 TLS 或未提供时为 null */
  peerCertificate: PeerCertificateLike | null;
}

export interface HttpStreamResponse {
  status: number;
  headers: IncomingHttpHeaders;
  peerCertificate: PeerCertificateLike | null;
  /** 响应体分块 */
  body: AsyncIterable<Buffer>;
  /** 主动关闭连接 */
  close(): void;
}

export type HttpTransport = (request: HttpRequestOptions) => Promise<HttpResponseData>;
export type HttpStreamTransport = (request: HttpRequestOptions) => Promise<HttpStreamResponse>;

/** 默认的响应体大小上限（64 MiB）；Python 的 httpx 没有上限，这里防止异常对端耗尽内存。 */
export const DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024;

/** 响应体超过上限。 */
export class ResponseTooLargeError extends Error {
  override name = "ResponseTooLargeError";
  constructor(readonly limitBytes: number) {
    super(`Response body exceeds ${limitBytes} bytes`);
  }
}

/** HTTP 非 2xx 状态错误，对应 Python 的 `httpx.HTTPStatusError`。 */
export class HttpStatusError extends Error {
  override name = "HttpStatusError";
  constructor(
    readonly status: number,
    readonly body: Buffer,
  ) {
    super(`HTTP Error: ${status} - ${body.toString("utf-8")}`);
  }

  get text(): string {
    return this.body.toString("utf-8");
  }
}

function readPeerCertificate(response: IncomingMessage): PeerCertificateLike | null {
  const socket = response.socket as Partial<TLSSocket> | null;
  if (!socket || typeof socket.getPeerCertificate !== "function") {
    return null;
  }
  const cert = socket.getPeerCertificate();
  if (!cert || Object.keys(cert).length === 0) {
    return null;
  }
  // 只信任通过证书链校验的服务端证书（rejectUnauthorized: false 时 Node 仍会返回未校验的证书）
  return socket.authorized === true ? cert : null;
}

function startRequest(
  options: HttpRequestOptions,
  tls: TlsClientOptions | undefined,
  onResponse: (response: IncomingMessage, peerCertificate: PeerCertificateLike | null) => void,
  onError: (error: Error) => void,
): void {
  const url = new URL(options.url);
  const isHttps = url.protocol === "https:";
  const send = isHttps ? httpsRequest : httpRequest;
  const headers: Record<string, string> = { ...(options.headers ?? {}) };
  if (options.body !== undefined && headers["Content-Length"] === undefined) {
    headers["Content-Length"] = String(Buffer.byteLength(options.body));
  }
  const req = send(
    url,
    {
      method: options.method,
      headers,
      timeout: options.timeoutMs ?? 30_000,
      signal: options.signal,
      // 每个请求独立连接，确保读取到的是本次连接的证书
      agent: false,
      ...(isHttps
        ? {
            cert: tls?.cert,
            key: tls?.key,
            ca: tls?.ca,
            servername: tls?.servername,
            rejectUnauthorized: tls?.rejectUnauthorized ?? true,
          }
        : {}),
    },
    (response) => onResponse(response, readPeerCertificate(response)),
  );
  req.on("timeout", () =>
    req.destroy(new Error(`Request timed out after ${options.timeoutMs ?? 30_000}ms`)),
  );
  req.on("error", onError);
  if (options.body !== undefined) {
    req.write(options.body);
  }
  req.end();
}

/** 创建基于 node:http / node:https 的一次性请求传输层。 */
export function createNodeTransport(
  tls?: TlsClientOptions,
  { maxResponseBytes = DEFAULT_MAX_RESPONSE_BYTES }: { maxResponseBytes?: number } = {},
): HttpTransport {
  return (options) =>
    new Promise<HttpResponseData>((resolve, reject) => {
      startRequest(
        options,
        tls,
        (response, peerCertificate) => {
          const chunks: Buffer[] = [];
          let size = 0;
          response.on("data", (chunk: Buffer) => {
            size += chunk.length;
            if (size > maxResponseBytes) {
              response.destroy(new ResponseTooLargeError(maxResponseBytes));
              return;
            }
            chunks.push(chunk);
          });
          response.on("error", reject);
          response.on("end", () =>
            resolve({
              status: response.statusCode ?? 0,
              headers: response.headers,
              body: Buffer.concat(chunks),
              peerCertificate,
            }),
          );
        },
        reject,
      );
    });
}

/** 创建流式响应传输层（用于 SSE）。 */
export function createNodeStreamTransport(tls?: TlsClientOptions): HttpStreamTransport {
  return (options) =>
    new Promise<HttpStreamResponse>((resolve, reject) => {
      startRequest(
        { ...options, timeoutMs: options.timeoutMs ?? 0 },
        tls,
        (response, peerCertificate) =>
          resolve({
            status: response.statusCode ?? 0,
            headers: response.headers,
            peerCertificate,
            body: response as AsyncIterable<Buffer>,
            close: () => response.destroy(),
          }),
        reject,
      );
    });
}
