/**
 * OIDC 测试共用工具：内存 fetch（对应 Python 测试里的 `httpx.MockTransport`）、现场生成的密钥与 token。
 */

import { exportJWK, generateKeyPair, SignJWT, type CryptoKey, type JWK } from "jose";

import type { OidcFetch, OidcMiddleware, OidcMiddlewareRequest } from "../../src/oidc/index.js";

/** 记录下来的一次请求。 */
export interface RecordedRequest {
  url: string;
  path: string;
  method: string;
  headers: Headers;
  body: string;
  form: Record<string, string>;
}

export type FakeHandler = (request: RecordedRequest) => Response | Promise<Response>;

/** 构造内存 fetch；`requests` 里按顺序记录每次调用。 */
export function fakeFetch(handler: FakeHandler): OidcFetch & { requests: RecordedRequest[] } {
  const requests: RecordedRequest[] = [];
  const fn = async (input: string, init?: RequestInit): Promise<Response> => {
    const url = new URL(input);
    const body = typeof init?.body === "string" ? init.body : "";
    const request: RecordedRequest = {
      url: input,
      path: url.pathname,
      method: init?.method ?? "GET",
      headers: new Headers(init?.headers),
      body,
      form: Object.fromEntries(new URLSearchParams(body)),
    };
    requests.push(request);
    return handler(request);
  };
  return Object.assign(fn, { requests });
}

/** JSON 响应，对应 `httpx.Response(status, json=...)`。 */
export function jsonResponse(status: number, payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** 文本响应，对应 `httpx.Response(status, text=...)`。 */
export function textResponse(status: number, text: string): Response {
  return new Response(text, { status });
}

/** 标准 provider：discovery + JWKS，其它路径 404。 */
export function providerFetch(options: {
  issuer: string;
  jwks: () => Record<string, unknown>;
  discovery?: Record<string, unknown>;
}) {
  const discovery = options.discovery ?? {
    issuer: options.issuer,
    jwks_uri: `${options.issuer}/protocol/openid-connect/certs`,
  };
  return fakeFetch((request) => {
    if (request.path.endsWith("/.well-known/openid-configuration")) {
      return jsonResponse(200, discovery);
    }
    if (request.path.endsWith("/protocol/openid-connect/certs")) {
      return jsonResponse(200, options.jwks());
    }
    return jsonResponse(404, { detail: "not found" });
  });
}

export interface TestKey {
  privateKey: CryptoKey;
  publicJwk: JWK;
}

/** 生成签名密钥并导出公钥 JWK（不含 kid/alg/use）。 */
export async function generateTestKey(
  algorithm: "EdDSA" | "ES256" | "PS256" | "RS256",
): Promise<TestKey> {
  const { privateKey, publicKey } = await generateKeyPair(
    algorithm,
    algorithm === "EdDSA" ? { crv: "Ed25519" } : {},
  );
  return { privateKey, publicJwk: await exportJWK(publicKey) };
}

/** 对应 Python 测试的 `_ed25519_jwk` / `_ec_p256_jwk` / `_rsa_jwk`：补上 kid、use、alg。 */
export function signingJwk(
  key: TestKey,
  options: { kid: string; alg?: string | null },
): Record<string, unknown> {
  const jwk: Record<string, unknown> = { ...key.publicJwk, kid: options.kid, use: "sig" };
  if (options.alg !== null && options.alg !== undefined) {
    jwk.alg = options.alg;
  }
  return jwk;
}

/** 对应 Python 测试的 `_encode_token`：按给定 claims 原样签名（不做 claim 校验）。 */
export async function encodeToken(options: {
  privateKey: CryptoKey;
  algorithm: string;
  kid: string;
  claims: Record<string, unknown>;
}): Promise<string> {
  return new SignJWT(options.claims)
    .setProtectedHeader({ alg: options.algorithm, kid: options.kid, typ: "JWT" })
    .sign(options.privateKey);
}

export const nowSeconds = (): number => Math.floor(Date.now() / 1000);

/** 记录写入内容的最小 `ServerResponse` 替身。 */
export class FakeResponse {
  statusCode = 200;
  readonly headers = new Map<string, string>();
  body: string | null = null;

  setHeader(name: string, value: string): this {
    this.headers.set(name.toLowerCase(), value);
    return this;
  }

  end(chunk: string): this {
    this.body = chunk;
    return this;
  }

  json(): unknown {
    return this.body === null ? null : JSON.parse(this.body);
  }
}

/** 中间件跑完后的结果：要么写了响应，要么调用了 `next(error?)`。 */
export interface MiddlewareOutcome {
  res: FakeResponse;
  req: { headers: Record<string, string>; principal?: unknown };
  nextCalled: boolean;
  nextError: unknown;
}

/** 用替身 req/res 运行 Express 风格中间件，直到它写出响应或调用 next。 */
export function runMiddleware(
  middleware: OidcMiddleware,
  headers: Record<string, string> = {},
): Promise<MiddlewareOutcome> {
  return new Promise((resolve) => {
    const req: MiddlewareOutcome["req"] = { headers };
    const res = new FakeResponse();
    const originalEnd = res.end.bind(res);
    res.end = (chunk: string) => {
      originalEnd(chunk);
      resolve({ res, req, nextCalled: false, nextError: undefined });
      return res;
    };
    middleware(req as OidcMiddlewareRequest, res, (error?: unknown) => {
      resolve({ res, req, nextCalled: true, nextError: error });
    });
  });
}
