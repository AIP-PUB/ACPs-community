/**
 * 对应 Python `tests/integration/test_aip_httpx_response_cert.py`：从真实 TLS 响应读取服务端证书身份。
 *
 * Python 读取 httpx 响应的 `network_stream`；Node 客户端用 node:https，在 `response` 事件时从 socket
 * 读取证书（`HttpResponseData.peerCertificate`，或直接 `extractPeerAicFromResponse(res)`）。
 */

import { createServer, type Server } from "node:http";
import { request as httpsRequest } from "node:https";
import type { AddressInfo } from "node:net";

import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { extractPeerAicFromResponse, extractPeerIdentity } from "../../src/aip/identity.js";
import { createNodeStreamTransport, createNodeTransport } from "../../src/internal/http.js";
import {
  LEADER_AIC,
  PARTNER_AIC,
  type RunningTlsServer,
  type TestPki,
  type TlsIdentity,
  clientTls,
  createTestPki,
  route,
  runTlsServer,
} from "./tlsTestUtils.js";

let pki: TestPki;
let partner: TlsIdentity;
let leader: TlsIdentity;
const servers: RunningTlsServer[] = [];

beforeAll(async () => {
  pki = await createTestPki();
  partner = await pki.issue(PARTNER_AIC);
  leader = await pki.issue(LEADER_AIC);
});

afterEach(async () => {
  await Promise.all(servers.splice(0).map((server) => server.close()));
});

const app = route({
  "GET /json": (_req, res) => {
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ ok: true }));
  },
  "GET /fail": (_req, res) => {
    res.statusCode = 500;
    res.setHeader("Content-Type", "text/plain");
    res.end("boom");
  },
  "GET /stream": async (_req, res) => {
    res.setHeader("Content-Type", "text/plain");
    res.write("chunk-1\n");
    await new Promise((resolve) => setImmediate(resolve));
    res.end("chunk-2\n");
  },
});

describe("test_aip_httpx_response_cert", () => {
  it("test_extract_peer_aic_from_real_tls_responses", async () => {
    const server = await runTlsServer(app, { pki, identity: partner, requireClientCert: false });
    servers.push(server);
    const tls = clientTls(pki, leader);
    const transport = createNodeTransport(tls);

    const ok = await transport({ url: `${server.baseUrl}/json`, method: "GET" });
    const fail = await transport({ url: `${server.baseUrl}/fail`, method: "GET" });
    const reuse = await transport({ url: `${server.baseUrl}/json`, method: "GET" });
    expect(extractPeerIdentity(ok.peerCertificate)?.aic).toBe(PARTNER_AIC);
    expect(extractPeerIdentity(fail.peerCertificate)?.aic).toBe(PARTNER_AIC);
    expect(extractPeerIdentity(reuse.peerCertificate)?.aic).toBe(PARTNER_AIC);

    const stream = await createNodeStreamTransport(tls)({
      url: `${server.baseUrl}/stream`,
      method: "GET",
    });
    expect(extractPeerIdentity(stream.peerCertificate)?.aic).toBe(PARTNER_AIC);
    const chunks: Buffer[] = [];
    for await (const chunk of stream.body) chunks.push(chunk);
    stream.close();

    expect(ok.status).toBe(200);
    expect(fail.status).toBe(500);
    expect(Buffer.concat(chunks).toString()).toBe("chunk-1\nchunk-2\n");
  });

  it("extractPeerAicFromResponse reads the certificate from a raw node:https response", async () => {
    const server = await runTlsServer(app, { pki, identity: partner, requireClientCert: false });
    servers.push(server);
    const aic = await new Promise<string | null>((resolve, reject) => {
      const req = httpsRequest(`${server.baseUrl}/json`, { ca: pki.ca, agent: false }, (res) => {
        const peer = extractPeerAicFromResponse(res);
        res.resume();
        res.on("end", () => resolve(peer));
      });
      req.on("error", reject);
      req.end();
    });
    expect(aic).toBe(PARTNER_AIC);
  });

  it("test_extract_peer_aic_returns_none_for_asgi_transport", async () => {
    // Python 的 ASGITransport 没有 TLS；Node 对应明文 HTTP 连接
    const server: Server = createServer((_req, res) => res.end("{}"));
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    try {
      const { port } = server.address() as AddressInfo;
      const response = await createNodeTransport()({
        url: `http://127.0.0.1:${port}/json`,
        method: "GET",
      });
      expect(response.peerCertificate).toBeNull();
      expect(extractPeerIdentity(response.peerCertificate)).toBeNull();
    } finally {
      server.closeAllConnections();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  });
});
