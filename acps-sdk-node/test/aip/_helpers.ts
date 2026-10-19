/**
 * AIP 单元测试共用的假对象（对应 Python 测试里的 `_cert` / `_FakeNetworkStream` / `httpx.MockTransport` 等）。
 */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { Readable } from "node:stream";

import type { PeerCertificateLike } from "../../src/aip/identity.js";
import { PeerCertificateInfo, setRequestPeerIdentity } from "../../src/aip/peerCert.js";
import type {
  HttpRequestOptions,
  HttpResponseData,
  HttpStreamResponse,
  HttpStreamTransport,
  HttpTransport,
} from "../../src/internal/http.js";
import { type Logger, setLoggerFactory } from "../../src/internal/logger.js";

export const LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4";
export const PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5";
export const OTHER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF548.0JU6";
export const VALID_AIC = LEADER_AIC;

/** Node `getPeerCertificate()` 形状的证书：`CN=<aic>`，可选 `URI:acps://<aic>` SAN。 */
export function nodeCert({
  commonName = VALID_AIC as string | null,
  sanAic = null as string | null,
  sanEntries = null as Array<[string, string]> | null,
} = {}): PeerCertificateLike {
  const cert: PeerCertificateLike = {};
  if (commonName !== null) {
    cert.subject = { CN: commonName };
  }
  const entries =
    sanEntries ?? (sanAic !== null ? [["URI", `acps://${sanAic}`] as [string, string]] : null);
  if (entries) {
    cert.subjectaltname = entries.map(([kind, value]) => `${kind}:${value}`).join(", ");
  }
  return cert;
}

/** 同时带 CN 与 SAN 的证书（对应 Python 测试的 `_cert(aic)`）。 */
export function certFor(aic: string): PeerCertificateLike {
  return nodeCert({ commonName: aic, sanAic: aic });
}

export interface RecordedRequest extends HttpRequestOptions {
  json: unknown;
}

/** 内存传输层（对应 httpx.MockTransport）。 */
export function memoryTransport(
  handler: (request: RecordedRequest) => {
    status?: number;
    body?: unknown;
    rawBody?: string;
    peerCertificate?: PeerCertificateLike | null;
  },
): HttpTransport & { requests: RecordedRequest[] } {
  const requests: RecordedRequest[] = [];
  const transport = (async (options: HttpRequestOptions): Promise<HttpResponseData> => {
    const raw = options.body === undefined ? "" : options.body.toString();
    const recorded: RecordedRequest = {
      ...options,
      json: raw ? (JSON.parse(raw) as unknown) : undefined,
    };
    requests.push(recorded);
    const reply = handler(recorded);
    const text = reply.rawBody ?? (reply.body === undefined ? "" : JSON.stringify(reply.body));
    return {
      status: reply.status ?? 200,
      headers: {},
      body: Buffer.from(text, "utf-8"),
      peerCertificate: reply.peerCertificate ?? null,
    };
  }) as HttpTransport & { requests: RecordedRequest[] };
  transport.requests = requests;
  return transport;
}

/** 返回固定 SSE 字节流的流式传输层；可按调用序号返回错误（对应 `_FailOnceSSETransport`）。 */
export function sseTransport(
  body: string | (() => string),
  {
    peerCertificate = null as PeerCertificateLike | null,
    status = 200,
    failCalls = [] as number[],
  } = {},
): HttpStreamTransport & { calls: number; requests: RecordedRequest[] } {
  const state = { calls: 0, requests: [] as RecordedRequest[] };
  const transport = (async (options: HttpRequestOptions): Promise<HttpStreamResponse> => {
    state.calls += 1;
    const raw = options.body === undefined ? "" : options.body.toString();
    state.requests.push({ ...options, json: raw ? (JSON.parse(raw) as unknown) : undefined });
    if (failCalls.includes(state.calls)) {
      throw Object.assign(new Error("Connection dropped"), { code: "ECONNRESET" });
    }
    const text = typeof body === "function" ? body() : body;
    return {
      status,
      headers: { "content-type": "text/event-stream" },
      peerCertificate,
      body: Readable.from([Buffer.from(text, "utf-8")]),
      close: () => undefined,
    };
  }) as HttpStreamTransport & { calls: number; requests: RecordedRequest[] };
  Object.defineProperty(transport, "calls", { get: () => state.calls });
  Object.defineProperty(transport, "requests", { get: () => state.requests });
  return transport;
}

/** 捕获 warn 日志（对应 pytest caplog）。返回的 `restore` 恢复静默。 */
export function captureLogs(): { messages: string[]; restore: () => void } {
  const messages: string[] = [];
  const logger: Logger = {
    debug: () => undefined,
    info: () => undefined,
    warn: (message) => messages.push(message),
    error: (message) => messages.push(message),
  };
  setLoggerFactory(() => logger);
  return { messages, restore: () => setLoggerFactory(undefined) };
}

/** 让 fire-and-forget 的 Promise 有机会执行（对应 `await asyncio.sleep(0)`）。 */
export function flushAsync(): Promise<void> {
  return new Promise((resolve) => setImmediate(resolve));
}

/** 用注入的对端 AIC 构造 PeerCertificateInfo（对应 Python 测试里写 `request.state.peer_aic`）。 */
export function injectedPeer(aic: string): PeerCertificateInfo {
  return new PeerCertificateInfo(null, null, { aic, commonName: null, sanAic: null });
}

export interface TestHttpServer {
  url: string;
  close: () => Promise<void>;
}

/**
 * 在 127.0.0.1:0 起一个明文 HTTP 服务；请求头 `x-peer-aic` 会通过 `setRequestPeerIdentity` 注入为对端身份
 * （对应 Python 测试里用中间件写 `request.state.peer_aic`）。
 */
export async function startHttpServer(
  listener: (req: IncomingMessage, res: ServerResponse) => unknown,
): Promise<TestHttpServer> {
  const server: Server = createServer((req, res) => {
    const peer = req.headers["x-peer-aic"];
    if (typeof peer === "string") {
      setRequestPeerIdentity(req, injectedPeer(peer));
    }
    Promise.resolve(listener(req, res)).then(
      (handled) => {
        if (handled === false && !res.headersSent) {
          res.statusCode = 404;
          res.end();
        }
      },
      (error: unknown) => {
        res.statusCode = 500;
        res.end(String(error));
      },
    );
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  return {
    url: `http://127.0.0.1:${port}`,
    close: () =>
      new Promise<void>((resolve) => {
        server.closeAllConnections();
        server.close(() => resolve());
      }),
  };
}

/** 把 TaskCommandType.X（Python 3.12 的枚举 f-string 形式）换成枚举值，便于与 Node 的消息比对。 */
export function pythonEnumToValue(text: string): string {
  const values: Record<string, string> = {
    Get: "get",
    Start: "start",
    Continue: "continue",
    Cancel: "cancel",
    Complete: "complete",
    ReStream: "re-stream",
  };
  return text.replace(/TaskCommandType\.(\w+)/g, (_m, name: string) => values[name] ?? name);
}
