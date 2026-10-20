/**
 * 审计签名的跨语言对照（样例 signatures.json 由 scripts/fixtures/gen_amp.py 生成，不含任何私钥）。
 *
 * - Ed25519：两边都用 RFC 8032 §7.1 TEST 1 公开测试向量作私钥，签名确定性 → 签名行逐字节相同；
 * - RSA / RSA-PSS：Node 验证 Python 的签名；再用测试时现场生成的密钥 + 证书（与 Python 证书同序列号）签名，
 *   比对「除签名值以外」的整行（规范化签名载荷、alg、kid）并自验签；
 * - ECDSA：只做验签（两个 SDK 都没有 ES256 签名器，见 issue #10）。
 */

// @peculiar/x509 依赖 Reflect metadata API，必须先于它加载
import "reflect-metadata";

import {
  type KeyObject,
  constants,
  createPrivateKey,
  createPublicKey,
  generateKeyPairSync,
  verify,
  webcrypto,
} from "node:crypto";

import * as x509 from "@peculiar/x509";
import { beforeAll, describe, expect, it } from "vitest";

import { loadPythonFixture } from "../fixtures.js";
import { formatAuditLine } from "../../src/amp/emitter.js";
import { canonicalize, canonicalizeToString } from "../../src/amp/jcs.js";
import { AUDIT_LOG_RECORD_DUMP, AuditLogRecordSchema, modelDump } from "../../src/amp/models.js";
import { CertificateAuditSigner, Ed25519AuditSigner } from "../../src/amp/signer.js";

type X509Crypto = Parameters<typeof x509.cryptoProvider.set>[0];
type KeyPair = Parameters<typeof x509.X509CertificateGenerator.createSelfSigned>[0]["keys"];
x509.cryptoProvider.set(webcrypto as unknown as X509Crypto);

type PyResult = { ok: true; value: unknown } | { ok: false; type: string; message: string };

const fx = loadPythonFixture<{
  rfc8032_test1_ed25519: { secret_key_hex: string; public_key_hex: string };
  public_keys: Record<"ed25519" | "rsa" | "ec", string>;
  certs: Record<"ed25519" | "rsa" | "serial_1" | "ec", string>;
  serials_hex: Record<"ed25519" | "rsa" | "serial_1" | "ec", string>;
  kids: Record<"ed25519" | "rsa" | "serial_1", string>;
  record_input: Record<string, unknown>;
  record_dict: Record<string, unknown>;
  canonical: string;
  lines: Record<"ed25519" | "cert_ed25519" | "cert_rsa" | "cert_rsa_pss", string>;
  ecdsa_der_sig_b64url: string;
  errors: Record<string, PyResult>;
}>("amp", "signatures");

const record = AuditLogRecordSchema.parse(fx.record_input);
const canonicalBytes = (): Buffer => canonicalize(modelDump(record, AUDIT_LOG_RECORD_DUMP));

// ─── 测试时现场生成的密钥材料（只在内存中） ─────────────────────────────────────

/** RFC 8032 种子 → PKCS#8 PEM（Ed25519 PKCS#8 = 固定前缀 + 32 字节种子）。 */
function ed25519PemFromSeed(seedHex: string): string {
  const der = Buffer.concat([
    Buffer.from("302e020100300506032b657004220420", "hex"),
    Buffer.from(seedHex, "hex"),
  ]);
  return pkcs8Pem(createPrivateKey({ key: der, format: "der", type: "pkcs8" }));
}

function pkcs8Pem(key: KeyObject): string {
  return key.export({ format: "pem", type: "pkcs8" }).toString();
}

function derTlv(tag: number, content: Buffer): Buffer {
  if (content.length < 0x80) {
    return Buffer.concat([Buffer.from([tag, content.length]), content]);
  }
  const length = Buffer.from(content.length.toString(16).padStart(4, "0"), "hex");
  return Buffer.concat([Buffer.from([tag, 0x80 | length.length]), length, content]);
}

/** 把普通 RSA 私钥包装成 OID 为 id-RSASSA-PSS 的 PKCS#8（与 gen_amp.py 的 _rsa_pss_pem 相同）。 */
function rsaPssPem(key: KeyObject): string {
  const pkcs1 = key.export({ format: "der", type: "pkcs1" });
  const rsassaPssOid = Buffer.from("06092a864886f70d01010a", "hex");
  const pkcs8 = derTlv(
    0x30,
    Buffer.concat([Buffer.from("020100", "hex"), derTlv(0x30, rsassaPssOid), derTlv(0x04, pkcs1)]),
  );
  const pssKey = createPrivateKey({ key: pkcs8, format: "der", type: "pkcs8" });
  expect(pssKey.asymmetricKeyType).toBe("rsa-pss");
  return pkcs8Pem(pssKey);
}

const RSA_ALGORITHM = {
  name: "RSASSA-PKCS1-v1_5",
  modulusLength: 2048,
  publicExponent: new Uint8Array([1, 0, 1]),
  hash: "SHA-256",
} as const;

interface NodeRsaIdentity {
  privateKey: KeyObject;
  publicKey: KeyObject;
  certPem: string;
}

/** Node 现场生成 RSA 密钥与自签证书，序列号与 Python 样例证书相同。 */
async function generateRsaIdentity(serialHex: string): Promise<NodeRsaIdentity> {
  const keys = (await webcrypto.subtle.generateKey(RSA_ALGORITHM, true, [
    "sign",
    "verify",
  ])) as unknown as KeyPair;
  const cert = await x509.X509CertificateGenerator.createSelfSigned({
    serialNumber: serialHex,
    name: "CN=acps-amp-node-test-rsa",
    notBefore: new Date("2026-01-01T00:00:00Z"),
    notAfter: new Date("2036-01-01T00:00:00Z"),
    keys,
    signingAlgorithm: RSA_ALGORITHM,
  });
  const pkcs8 = await webcrypto.subtle.exportKey(
    "pkcs8",
    keys.privateKey as unknown as webcrypto.CryptoKey,
  );
  const privateKey = createPrivateKey({ key: Buffer.from(pkcs8), format: "der", type: "pkcs8" });
  return { privateKey, publicKey: createPublicKey(privateKey), certPem: cert.toString("pem") };
}

// ─── 工具 ────────────────────────────────────────────────────────────────────

interface SignedRecord {
  integrity: { alg: string; kid: string; sig: string };
}

const SIG_VALUE = /"sig": "[A-Za-z0-9_-]*"/;

/** 把签名值替换成占位符，用于比对「除签名值以外」的整行。 */
function withoutSignature(line: string): string {
  expect(line).toMatch(SIG_VALUE);
  return line.replace(SIG_VALUE, '"sig": "<signature>"');
}

function signatureOf(line: string): Buffer {
  return Buffer.from((JSON.parse(line) as SignedRecord).integrity.sig, "base64url");
}

function verifyRsa(publicKey: KeyObject | string, line: string): boolean {
  const key = typeof publicKey === "string" ? createPublicKey(publicKey) : publicKey;
  return verify(
    "sha256",
    canonicalBytes(),
    { key, padding: constants.RSA_PKCS1_PADDING },
    signatureOf(line),
  );
}

function capture(fn: () => unknown): PyResult {
  try {
    return { ok: true, value: fn() };
  } catch (error) {
    const err = error as Error;
    return {
      ok: false,
      type: err instanceof TypeError ? "TypeError" : "ValueError",
      message: err.message,
    };
  }
}

// ─── 测试 ────────────────────────────────────────────────────────────────────

describe("signatures.json: 签名载荷", () => {
  it("model_dump 与 JCS 规范化结果一致", () => {
    const dump = modelDump(record, AUDIT_LOG_RECORD_DUMP);
    expect(JSON.parse(JSON.stringify(dump))).toEqual(fx.record_dict);
    expect(canonicalizeToString(dump)).toBe(fx.canonical);
  });
});

describe("signatures.json: Ed25519（RFC 8032 公开测试向量，签名行逐字节一致）", () => {
  const edPem = ed25519PemFromSeed(fx.rfc8032_test1_ed25519.secret_key_hex);

  it("种子派生的公钥与 RFC 8032 TEST 1 / 样例公钥一致", () => {
    const raw = createPublicKey(edPem).export({ format: "der", type: "spki" }).subarray(-32);
    expect(raw.toString("hex")).toBe(fx.rfc8032_test1_ed25519.public_key_hex);
    expect(createPublicKey(edPem).export({ format: "pem", type: "spki" })).toBe(
      fx.public_keys.ed25519,
    );
  });

  it("Ed25519AuditSigner 签名行一致", () => {
    const signer = new Ed25519AuditSigner(edPem, "audit-key-test");
    expect(formatAuditLine(record, signer)).toBe(fx.lines.ed25519);
  });

  it("CertificateAuditSigner（Ed25519）签名行一致", () => {
    const signer = new CertificateAuditSigner(edPem, fx.certs.ed25519);
    expect(signer.alg).toBe("EdDSA");
    expect(formatAuditLine(record, signer)).toBe(fx.lines.cert_ed25519);
  });

  it("CertificateAuditSigner 的 kid 为无前导零的大写十六进制序列号", () => {
    expect(new CertificateAuditSigner(edPem, fx.certs.ed25519).kid).toBe(fx.kids.ed25519);
    expect(new CertificateAuditSigner(edPem, fx.certs.rsa).kid).toBe(fx.kids.rsa);
    expect(new CertificateAuditSigner(edPem, fx.certs.serial_1).kid).toBe(fx.kids.serial_1);
  });
});

describe("signatures.json: RSA / RSA-PSS（验签 + 现场密钥比对签名以外的内容）", () => {
  let identity: NodeRsaIdentity;

  beforeAll(async () => {
    identity = await generateRsaIdentity(fx.serials_hex.rsa);
  });

  it("Node 验证 Python 的 RS256 签名（普通 RSA 与 RSA-PSS 受限私钥）", () => {
    expect(verifyRsa(fx.public_keys.rsa, fx.lines.cert_rsa)).toBe(true);
    expect(verifyRsa(fx.public_keys.rsa, fx.lines.cert_rsa_pss)).toBe(true);
  });

  it("Node 现场证书的 kid 与 Python 一致", () => {
    expect(new CertificateAuditSigner(pkcs8Pem(identity.privateKey), identity.certPem).kid).toBe(
      fx.kids.rsa,
    );
  });

  it("CertificateAuditSigner（RSA）：除签名值外整行一致，签名可验", () => {
    const signer = new CertificateAuditSigner(pkcs8Pem(identity.privateKey), identity.certPem);
    expect(signer.alg).toBe("RS256");
    const line = formatAuditLine(record, signer);
    expect(withoutSignature(line)).toBe(withoutSignature(fx.lines.cert_rsa));
    expect(verifyRsa(identity.publicKey, line)).toBe(true);
    // PKCS#1 v1.5 是确定性的
    expect(formatAuditLine(record, signer)).toBe(line);
  });

  it("RSA-PSS 受限 PKCS#8 私钥按普通 RSA 处理（同 Python cryptography）", () => {
    const signer = new CertificateAuditSigner(rsaPssPem(identity.privateKey), identity.certPem);
    expect(signer.alg).toBe("RS256");
    const line = formatAuditLine(record, signer);
    expect(withoutSignature(line)).toBe(withoutSignature(fx.lines.cert_rsa_pss));
    expect(verifyRsa(identity.publicKey, line)).toBe(true);
    expect(signatureOf(line)).toEqual(
      signatureOf(
        formatAuditLine(
          record,
          new CertificateAuditSigner(pkcs8Pem(identity.privateKey), identity.certPem),
        ),
      ),
    );
  });
});

describe("signatures.json: ECDSA 与不支持的密钥类型", () => {
  it("Python 生成的 ECDSA P-256 签名可在 Node 侧对同一规范化字节验签", () => {
    const signature = Buffer.from(fx.ecdsa_der_sig_b64url, "base64url");
    expect(verify("sha256", canonicalBytes(), createPublicKey(fx.public_keys.ec), signature)).toBe(
      true,
    );
  });

  it("不支持的私钥类型报错信息一致（含 issue #10：CertificateAuditSigner 不支持 EC）", () => {
    const rsaPem = pkcs8Pem(generateKeyPairSync("rsa", { modulusLength: 2048 }).privateKey);
    const ecPem = pkcs8Pem(generateKeyPairSync("ec", { namedCurve: "P-256" }).privateKey);
    expect(capture(() => new Ed25519AuditSigner(rsaPem, "k"))).toEqual(
      fx.errors["ed25519_signer_with_rsa"],
    );
    expect(capture(() => new Ed25519AuditSigner(ecPem, "k"))).toEqual(
      fx.errors["ed25519_signer_with_ec"],
    );
    expect(capture(() => new CertificateAuditSigner(ecPem, fx.certs.ec))).toEqual(
      fx.errors["cert_signer_with_ec"],
    );
  });
});
