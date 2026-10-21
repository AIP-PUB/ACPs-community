/**
 * AMP Audit 签名器协议与实现（对应 Python `acps_sdk.amp.signer`）。
 *
 * 提供 AuditSigner 协议与两种开箱即用的实现：
 * - Ed25519AuditSigner：用于 audit_keys.json（开发/mock）流程，显式指定 kid。
 * - CertificateAuditSigner：用于生产流程，从 X.509 证书自动提取序列号作为 kid，
 *   并自动检测私钥类型（Ed25519 → EdDSA，RSA → RS256）。
 *
 * 签名只用 `node:crypto`，无需额外依赖（Python 版需安装 amp-sign extra）。
 *
 * 典型使用（生产/CA 流程）：
 *
 * ```ts
 * const signer = new CertificateAuditSigner(readFileSync("client.key", "utf-8"), readFileSync("client.pem", "utf-8"));
 * const emitter = new AuditEmitter(logFile, aic, { signer });
 * ```
 *
 * 典型使用（开发/mock 流程）：
 *
 * ```ts
 * const signer = loadSignerFromKeysJson("/path/to/audit_keys.json", aic);
 * const emitter = new AuditEmitter(logFile, aic, { signer });
 * ```
 */

import {
  type KeyObject,
  X509Certificate,
  constants as cryptoConstants,
  createPrivateKey,
  sign as cryptoSign,
} from "node:crypto";
import { existsSync, readFileSync } from "node:fs";

import { getLogger } from "../internal/logger.js";
import { pyTypeName } from "../internal/pyCompat.js";

const logger = getLogger("acps_sdk.amp.signer");

/**
 * AMP 日志签名器协议。
 *
 * 实现此协议的对象可传入 AuditEmitter 构造函数，在写入日志前对记录内容（不含 integrity 字段）进行签名。
 */
export interface AuditSigner {
  /** 密钥 ID，写入 integrity.kid 字段。 */
  readonly kid: string;
  /** 签名算法标识，写入 integrity.alg 字段（'EdDSA' / 'ES256' / 'RS256'）。 */
  readonly alg: string;
  /** 对 data 签名，返回原始签名字节（Emitter 负责 Base64url 编码）。 */
  sign(data: Uint8Array): Uint8Array;
}

/** Node `asymmetricKeyType` → Python cryptography 私钥类名（用于与 Python 版一致的错误信息）。 */
const PY_PRIVATE_KEY_CLASS: Readonly<Record<string, string>> = {
  rsa: "RSAPrivateKey",
  dsa: "DSAPrivateKey",
  dh: "DHPrivateKey",
  ec: "ECPrivateKey",
  ed25519: "Ed25519PrivateKey",
  ed448: "Ed448PrivateKey",
  x25519: "X25519PrivateKey",
  x448: "X448PrivateKey",
};

function pyKeyClassName(key: KeyObject): string {
  const type = key.asymmetricKeyType ?? "unknown";
  return PY_PRIVATE_KEY_CLASS[type] ?? type;
}

/** 读取一个 DER TLV，返回 [tag, 内容起点, 内容终点]。 */
function readDerTlv(der: Buffer, offset: number): [number, number, number] {
  const tag = der[offset] ?? -1;
  let length = der[offset + 1] ?? 0;
  let start = offset + 2;
  if (length & 0x80) {
    const lengthBytes = length & 0x7f;
    length = 0;
    for (let i = 0; i < lengthBytes; i += 1) {
      length = length * 256 + (der[start + i] ?? 0);
    }
    start += lengthBytes;
  }
  if (start + length > der.length) {
    throw new Error("malformed PKCS#8 private key");
  }
  return [tag, start, start + length];
}

/**
 * RSA-PSS 受限私钥（OID id-RSASSA-PSS）→ 普通 RSA 私钥。
 *
 * Python cryptography 把这类私钥加载为普通 RSAPrivateKey（忽略 PSS 参数限制），可以用 PKCS#1 v1.5 签名；
 * OpenSSL / Node 则拒绝对 rsa-pss 密钥使用 PKCS#1 v1.5 填充。这里从 PKCS#8 中取出内层的 PKCS#1
 * RSAPrivateKey 重新导入，行为与 Python 一致。
 */
function rsaPssToRsa(key: KeyObject): KeyObject {
  const der = key.export({ format: "der", type: "pkcs8" });
  const [outerTag, outerStart] = readDerTlv(der, 0); // PrivateKeyInfo SEQUENCE
  const [versionTag, , versionEnd] = readDerTlv(der, outerStart); // version INTEGER
  const [algTag, , algEnd] = readDerTlv(der, versionEnd); // AlgorithmIdentifier SEQUENCE
  const [keyTag, keyStart, keyEnd] = readDerTlv(der, algEnd); // privateKey OCTET STRING
  if (outerTag !== 0x30 || versionTag !== 0x02 || algTag !== 0x30 || keyTag !== 0x04) {
    throw new Error("malformed PKCS#8 private key");
  }
  return createPrivateKey({ key: der.subarray(keyStart, keyEnd), format: "der", type: "pkcs1" });
}

/** 等价于 `load_pem_private_key(pem.encode(), password=None)`。 */
function loadPemPrivateKey(privateKeyPem: string): KeyObject {
  const key = createPrivateKey({ key: privateKeyPem, format: "pem" });
  return key.asymmetricKeyType === "rsa-pss" ? rsaPssToRsa(key) : key;
}

/**
 * Ed25519 签名器，从 PKCS8 PEM 私钥构造。
 *
 * @throws TypeError 私钥类型不是 Ed25519
 */
export class Ed25519AuditSigner implements AuditSigner {
  private readonly key: KeyObject;
  readonly kid: string;
  readonly alg = "EdDSA";

  /**
   * @param privateKeyPem PKCS8 PEM 格式的 Ed25519 私钥字符串
   * @param kid 密钥 ID，写入 integrity.kid 字段
   */
  constructor(privateKeyPem: string, kid: string) {
    const loaded = loadPemPrivateKey(privateKeyPem);
    if (loaded.asymmetricKeyType !== "ed25519") {
      throw new TypeError(
        `期望 Ed25519PrivateKey，实际类型: ${pyKeyClassName(loaded)}。` +
          "请确保私钥由 gen_audit_keys.py 或 ed25519 算法生成。",
      );
    }
    this.key = loaded;
    this.kid = kid;
  }

  /** 使用 Ed25519 私钥对 data 签名，返回 64 字节原始签名。 */
  sign(data: Uint8Array): Uint8Array {
    return cryptoSign(null, data, this.key);
  }
}

/** Python `format(serial_number, "X")`：大写十六进制、无前导零（Node 的 serialNumber 按整字节补零）。 */
function formatSerialHex(serialNumber: string): string {
  const negative = serialNumber.startsWith("-");
  const digits = negative ? serialNumber.slice(1) : serialNumber;
  const value = BigInt(`0x${digits}`);
  return `${negative && value !== 0n ? "-" : ""}${value.toString(16).toUpperCase()}`;
}

/**
 * 从 X.509 证书 + 私钥构造的签名器，自动以证书序列号（uppercase hex）为 kid。
 *
 * 按 AMP spec §5.1.2 规定，kid 应为签名证书的 X.509 serialNumber（大写十六进制字符串），
 * 验签方通过 kid 向 CA 服务查询对应公钥。
 *
 * 支持的私钥类型：
 * - Ed25519 → alg = "EdDSA"（优先，AMP spec 推荐）
 * - RSA → alg = "RS256"（PKCS#1 v1.5 + SHA-256）
 *
 * 与 Python 版相同，暂不支持 EC 私钥（ES256），传入时抛 TypeError。
 *
 * @throws TypeError 私钥类型不受支持（非 Ed25519 / RSA）
 */
export class CertificateAuditSigner implements AuditSigner {
  private readonly key: KeyObject;
  readonly kid: string;
  readonly alg: "EdDSA" | "RS256";

  /**
   * @param privateKeyPem PKCS8 PEM 格式私钥（Ed25519 或 RSA）
   * @param certPem PEM 格式 X.509 证书，用于提取序列号作为 kid
   */
  constructor(privateKeyPem: string, certPem: string) {
    const cert = new X509Certificate(certPem);
    // AMP spec §5.1.2: kid 为证书序列号大写十六进制字符串（RFC 5280 §4.1.2.2）
    this.kid = formatSerialHex(cert.serialNumber);

    const key = loadPemPrivateKey(privateKeyPem);
    if (key.asymmetricKeyType === "ed25519") {
      this.alg = "EdDSA";
    } else if (key.asymmetricKeyType === "rsa") {
      this.alg = "RS256";
    } else {
      throw new TypeError(
        `不支持的私钥类型: ${pyKeyClassName(key)}。` +
          "CertificateAuditSigner 仅支持 Ed25519 和 RSA 私钥。",
      );
    }
    this.key = key;
  }

  /** 对 data 签名，返回原始签名字节（Emitter 负责 Base64url 编码）。 */
  sign(data: Uint8Array): Uint8Array {
    if (this.alg === "EdDSA") {
      return cryptoSign(null, data, this.key);
    }
    // RS256: PKCS#1 v1.5 + SHA-256
    return cryptoSign("sha256", data, {
      key: this.key,
      padding: cryptoConstants.RSA_PKCS1_PADDING,
    });
  }
}

/**
 * 从 audit_keys.json 文件按 AIC 查找私钥，构造 Ed25519AuditSigner。
 *
 * 文件格式（由 monitor-server/scripts/gen_audit_keys.py 生成）：
 *
 * ```json
 * {
 *   "agent_name": {
 *     "aic": "1.2.156.3088...",
 *     "kid": "audit-key-agent_name",
 *     "public_key": "-----BEGIN PUBLIC KEY-----\n...",
 *     "private_key": "-----BEGIN PRIVATE KEY-----\n..."
 *   }
 * }
 * ```
 *
 * 文件不存在、无法解析或找不到匹配 AIC 时记录 WARNING 并返回 null，AuditEmitter 将以未签名模式运行。
 * 与 Python 版相同，第一个 aic 匹配的条目即为结果（构造失败也不再继续查找）。
 *
 * @param keysJsonPath audit_keys.json 文件路径
 * @param aic Agent Instance Context 标识符，用于在文件中定位条目
 * @throws TypeError 文件顶层或条目不是 JSON 对象（对应 Python 的 AttributeError）
 */
export function loadSignerFromKeysJson(
  keysJsonPath: string,
  aic: string,
): Ed25519AuditSigner | null {
  if (!existsSync(keysJsonPath)) {
    logger.warn(
      `audit_keys.json 不存在，AuditEmitter 将以未签名模式运行 (path=${keysJsonPath})。` +
        "运行 uv run python monitor-server/scripts/gen_audit_keys.py 生成。",
    );
    return null;
  }

  let entries: unknown;
  try {
    const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(
      readFileSync(keysJsonPath),
    );
    entries = JSON.parse(text);
  } catch (error) {
    logger.warn(`读取 audit_keys.json 失败，AuditEmitter 将以未签名模式运行: ${String(error)}`);
    return null;
  }

  if (entries === null || typeof entries !== "object" || Array.isArray(entries)) {
    throw new TypeError(`'${pyTypeName(entries)}' object has no attribute 'values'`);
  }

  for (const entry of Object.values(entries as Record<string, unknown>)) {
    if (entry === null || typeof entry !== "object" || Array.isArray(entry)) {
      throw new TypeError(`'${pyTypeName(entry)}' object has no attribute 'get'`);
    }
    const record = entry as Record<string, unknown>;
    if (record["aic"] === aic) {
      try {
        if (!("private_key" in record)) {
          throw new Error("KeyError: 'private_key'");
        }
        if (!("kid" in record)) {
          throw new Error("KeyError: 'kid'");
        }
        return new Ed25519AuditSigner(record["private_key"] as string, record["kid"] as string);
      } catch (error) {
        logger.warn(
          `构造 Ed25519AuditSigner 失败，AuditEmitter 将以未签名模式运行: ${String(error)}`,
        );
        return null;
      }
    }
  }

  logger.warn(
    `audit_keys.json 中未找到 aic=${aic} 的条目，AuditEmitter 将以未签名模式运行。` +
      "如需签名，请在 gen_audit_keys.py 的 AGENTS 字典中添加该 Agent。",
  );
  return null;
}
