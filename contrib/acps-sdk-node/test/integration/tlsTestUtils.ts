/**
 * 集成测试用的 TLS 工具（对应 Python `tests/integration/_tls_test_utils.py`）。
 *
 * Python 版读取 demo 目录里提交的证书；Node 版用 `@peculiar/x509` + WebCrypto 在测试进程内现场生成
 * 一个 CA 和若干叶子证书（`CN=<AIC>`、`URI:acps://<AIC>`、`IP:127.0.0.1`、`DNS:localhost`，
 * 同时带 serverAuth 与 clientAuth），私钥只存在内存中，不落盘、不提交。
 */

// @peculiar/x509 依赖 Reflect metadata API，必须先于它加载
import "reflect-metadata";

import { webcrypto } from "node:crypto";
import { type IncomingMessage, type ServerResponse } from "node:http";
import { type Server, createServer } from "node:https";
import type { AddressInfo } from "node:net";

import * as x509 from "@peculiar/x509";

import type { TlsClientOptions } from "../../src/internal/http.js";

/** 与 Python 测试 demo 证书对应的两个身份（任意合法 AIC 即可）。 */
export const PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5";
export const LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4";
export const OTHER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF548.0JU6";

const ALGORITHM = { name: "ECDSA", namedCurve: "P-256", hash: "SHA-256" } as const;

type X509Crypto = Parameters<typeof x509.cryptoProvider.set>[0];
type KeyPair = Parameters<typeof x509.X509CertificateGenerator.createSelfSigned>[0]["keys"];

x509.cryptoProvider.set(webcrypto as unknown as X509Crypto);

async function generateKeyPair(): Promise<KeyPair> {
  return (await webcrypto.subtle.generateKey(ALGORITHM, true, [
    "sign",
    "verify",
  ])) as unknown as KeyPair;
}

export interface TlsIdentity {
  aic: string;
  cert: string;
  key: string;
}

export interface TestPki {
  /** 信任锚（PEM），对应 Python 的 trust-bundle.pem */
  ca: string;
  /** 生成指定 AIC 的叶子证书（serverAuth + clientAuth） */
  issue(aic: string): Promise<TlsIdentity>;
}

function validity(): { notBefore: Date; notAfter: Date } {
  const now = Date.now();
  return {
    notBefore: new Date(now - 24 * 3600 * 1000),
    notAfter: new Date(now + 24 * 3600 * 1000),
  };
}

async function exportKey(key: KeyPair["privateKey"]): Promise<string> {
  const pkcs8 = await webcrypto.subtle.exportKey("pkcs8", key as unknown as webcrypto.CryptoKey);
  return x509.PemConverter.encode(pkcs8, "PRIVATE KEY");
}

let serial = 1;
const nextSerial = (): string => (serial++).toString(16).padStart(2, "0");

/** 生成一个测试 CA。 */
export async function createTestPki(): Promise<TestPki> {
  const caKeys = await generateKeyPair();
  const caCert = await x509.X509CertificateGenerator.createSelfSigned({
    serialNumber: nextSerial(),
    name: "CN=ACPs SDK Test CA",
    ...validity(),
    keys: caKeys,
    signingAlgorithm: ALGORITHM,
    extensions: [
      new x509.BasicConstraintsExtension(true, undefined, true),
      new x509.KeyUsagesExtension(
        x509.KeyUsageFlags.keyCertSign | x509.KeyUsageFlags.cRLSign,
        true,
      ),
      await x509.SubjectKeyIdentifierExtension.create(caKeys.publicKey),
    ],
  });

  return {
    ca: caCert.toString("pem"),
    async issue(aic: string): Promise<TlsIdentity> {
      const keys = await generateKeyPair();
      const cert = await x509.X509CertificateGenerator.create({
        serialNumber: nextSerial(),
        subject: `CN=${aic}`,
        issuer: caCert.subject,
        ...validity(),
        signingAlgorithm: ALGORITHM,
        publicKey: keys.publicKey,
        signingKey: caKeys.privateKey,
        extensions: [
          new x509.BasicConstraintsExtension(false, undefined, true),
          new x509.KeyUsagesExtension(
            x509.KeyUsageFlags.digitalSignature | x509.KeyUsageFlags.keyAgreement,
            true,
          ),
          new x509.ExtendedKeyUsageExtension([
            x509.ExtendedKeyUsage.serverAuth,
            x509.ExtendedKeyUsage.clientAuth,
          ]),
          new x509.SubjectAlternativeNameExtension([
            { type: "url", value: `acps://${aic}` },
            { type: "ip", value: "127.0.0.1" },
            { type: "dns", value: "localhost" },
          ]),
          await x509.SubjectKeyIdentifierExtension.create(keys.publicKey),
          await x509.AuthorityKeyIdentifierExtension.create(caCert),
        ],
      });
      return { aic, cert: cert.toString("pem"), key: await exportKey(keys.privateKey) };
    },
  };
}

/** 客户端 TLS 选项（对应 Python `build_client_ssl_context`）。 */
export function clientTls(pki: TestPki, identity?: TlsIdentity): TlsClientOptions {
  return identity ? { cert: identity.cert, key: identity.key, ca: pki.ca } : { ca: pki.ca };
}

export interface RunningTlsServer {
  baseUrl: string;
  server: Server;
  close(): Promise<void>;
}

/**
 * 在 127.0.0.1:0 起一个真实的 https 服务（对应 Python `run_tls_app` + `build_server_ssl_context`）。
 *
 * `requireClientCert` 为 true 时 `requestCert: true, rejectUnauthorized: true`，即强制 mTLS。
 */
export async function runTlsServer(
  listener: (req: IncomingMessage, res: ServerResponse) => unknown,
  {
    pki,
    identity,
    requireClientCert,
  }: { pki: TestPki; identity: TlsIdentity; requireClientCert: boolean },
): Promise<RunningTlsServer> {
  const server = createServer(
    {
      cert: identity.cert,
      key: identity.key,
      ca: pki.ca,
      requestCert: requireClientCert,
      rejectUnauthorized: requireClientCert,
      minVersion: "TLSv1.2",
    },
    (req, res) => {
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
    },
  );
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  return {
    baseUrl: `https://127.0.0.1:${port}`,
    server,
    close: () =>
      new Promise<void>((resolve) => {
        server.closeAllConnections();
        server.close(() => resolve());
      }),
  };
}

/** 简单路由：按 `METHOD path` 精确匹配，或以 `*` 结尾的前缀匹配。 */
export function route(
  routes: Record<string, (req: IncomingMessage, res: ServerResponse) => unknown>,
): (req: IncomingMessage, res: ServerResponse) => unknown {
  return (req, res) => {
    const path = new URL(req.url ?? "/", "https://localhost").pathname;
    const key = `${req.method ?? "GET"} ${path}`;
    const exact = routes[key];
    if (exact) return exact(req, res);
    for (const [pattern, handler] of Object.entries(routes)) {
      if (pattern.endsWith("*") && key.startsWith(pattern.slice(0, -1))) return handler(req, res);
    }
    return false;
  };
}
