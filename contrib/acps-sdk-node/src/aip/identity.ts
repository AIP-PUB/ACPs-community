/**
 * AIP 身份绑定（对应 Python `acps_sdk.aip.aip_identity`）
 *
 * 把 mTLS 连接身份（证书 CN / `URI:acps://{AIC}` SAN）与业务消息里的 `senderId` 绑定，
 * 防止持有合法证书的一方在 payload 里冒充其他智能体。
 *
 * 与 Python 版的差异：Python 读取 `ssl.getpeercert()` 的元组结构，Node 版读取
 * `tls.TLSSocket#getPeerCertificate()` 返回的对象（`subject.CN` 与 `subjectaltname` 字符串）。
 */

import type { IncomingMessage } from "node:http";
import type { TLSSocket } from "node:tls";

import { validateAicFormat } from "../aic/index.js";
import { pyStrip } from "../internal/pyCompat.js";
import type { JSONRPCError } from "./rpcModel.js";

export const AUTHENTICATION_REQUIRED_CODE = -32008;
export const AUTHORIZATION_FAILED_CODE = -32009;

/** Base error for AIP identity binding failures. */
export class AipIdentityError extends Error {
  override name = "AipIdentityError";
}

/** Raised when identity binding requires a peer AIC but none is available. */
export class PeerAicMissingError extends AipIdentityError {
  override name = "PeerAicMissingError";
}

/** Raised when a payload sender identity does not match the peer identity. */
export class SenderIdentityMismatchError extends AipIdentityError {
  override name = "SenderIdentityMismatchError";
}

/** Raised when a peer certificate cannot produce a valid AIC identity. */
export class InvalidPeerCertificateError extends AipIdentityError {
  override name = "InvalidPeerCertificateError";
}

/** Normalized peer identity extracted from a peer certificate. */
export interface PeerIdentity {
  readonly aic: string;
  readonly commonName: string | null;
  readonly sanAic: string | null;
}

/** Global identity binding switch. */
export interface AipIdentityBindingConfig {
  readonly enabled: boolean;
}

/**
 * 证书对象的最小形状，兼容 `tls.PeerCertificate` / `tls.DetailedPeerCertificate`。
 * 未提供客户端证书时 Node 返回空对象 `{}`，按“无证书”处理。
 */
export interface PeerCertificateLike {
  subject?: Record<string, string | string[] | undefined> | null;
  subjectaltname?: string | null;
}

/** Normalize an AIC for consistent comparison. */
export function normalizeAic(aic: string | null | undefined): string | null {
  if (aic === null || aic === undefined) {
    return null;
  }
  // 按 Python `str.strip()` 的空白集合去除两端空白
  const normalized = pyStrip(String(aic)).toUpperCase();
  return normalized || null;
}

/** Normalize and validate an AIC carried in a protocol payload. */
export function normalizeAndValidateAic(
  aic: string | null | undefined,
  { fieldName = "senderId" }: { fieldName?: string } = {},
): string {
  const normalized = normalizeAic(aic);
  if (normalized === null) {
    throw new SenderIdentityMismatchError(`${fieldName} is required`);
  }
  const [valid, error] = validateAicFormat(normalized);
  if (!valid) {
    throw new SenderIdentityMismatchError(`${fieldName} is invalid: ${error}`);
  }
  return normalized;
}

/** Normalize and validate a configured local/expected AIC. 配置错误抛普通 Error（对应 Python ValueError）。 */
export function normalizeAndValidateExpectedAic(
  aic: string | null | undefined,
  { fieldName = "expected_aic" }: { fieldName?: string } = {},
): string {
  const normalized = normalizeAic(aic);
  if (normalized === null) {
    throw new Error(`${fieldName} is required`);
  }
  const [valid, error] = validateAicFormat(normalized);
  if (!valid) {
    throw new Error(`${fieldName} is invalid: ${error}`);
  }
  return normalized;
}

function isEmptyCertificate(cert: PeerCertificateLike | null | undefined): boolean {
  return !cert || Object.keys(cert).length === 0;
}

/** Extract the peer certificate common name. */
export function extractCommonName(
  peerCertificate: PeerCertificateLike | null | undefined,
): string | null {
  if (isEmptyCertificate(peerCertificate)) {
    return null;
  }
  const cn = peerCertificate?.subject?.CN;
  if (Array.isArray(cn)) {
    return typeof cn[0] === "string" ? cn[0] : null;
  }
  return typeof cn === "string" ? cn : null;
}

/**
 * 解析 Node 的 `subjectaltname` 字符串，例如 `DNS:a.example.com, URI:acps://1.2.156...`。
 * 含特殊字符的值会被 Node 以 JSON 字符串形式加引号，这里一并处理。
 */
export function parseSubjectAltName(
  subjectAltName: string | null | undefined,
): Array<[string, string]> {
  if (!subjectAltName) {
    return [];
  }
  const entries: Array<[string, string]> = [];
  let index = 0;
  const text = subjectAltName;
  while (index < text.length) {
    while (text[index] === " " || text[index] === ",") index += 1;
    if (index >= text.length) break;
    const colon = text.indexOf(":", index);
    if (colon < 0) break;
    const kind = text.slice(index, colon).trim();
    index = colon + 1;
    let value: string;
    if (text[index] === '"') {
      let end = index + 1;
      while (end < text.length) {
        if (text[end] === "\\") {
          end += 2;
          continue;
        }
        if (text[end] === '"') break;
        end += 1;
      }
      const quoted = text.slice(index, end + 1);
      try {
        value = JSON.parse(quoted) as string;
      } catch {
        value = quoted.slice(1, -1);
      }
      index = end + 1;
    } else {
      const comma = text.indexOf(", ", index);
      const end = comma < 0 ? text.length : comma;
      value = text.slice(index, end);
      index = end;
    }
    entries.push([kind, value]);
  }
  return entries;
}

/** Extract the SAN URI AIC (`acps://{AIC}`) from a peer certificate. */
export function extractAcpsUriSan(
  peerCertificate: PeerCertificateLike | null | undefined,
): string | null {
  if (isEmptyCertificate(peerCertificate)) {
    return null;
  }
  const acpsUris: string[] = [];
  for (const [kind, value] of parseSubjectAltName(peerCertificate?.subjectaltname)) {
    if (kind !== "URI") continue;
    if (value.toLowerCase().startsWith("acps://")) {
      acpsUris.push(value.slice("acps://".length));
    }
  }
  if (acpsUris.length === 0) {
    return null;
  }
  const normalizedValues = new Set(
    acpsUris.map((value) => normalizeAic(value)).filter((value): value is string => value !== null),
  );
  if (normalizedValues.size > 1) {
    throw new InvalidPeerCertificateError(
      "peer certificate contains multiple distinct ACPs SAN URI AIC values",
    );
  }
  return acpsUris[0] ?? null;
}

/** Extract a normalized peer identity from a TLS peer certificate. */
export function extractPeerIdentity(
  peerCertificate: PeerCertificateLike | null | undefined,
): PeerIdentity | null {
  const commonName = extractCommonName(peerCertificate);
  const sanAic = extractAcpsUriSan(peerCertificate);

  if (commonName === null) {
    return null;
  }

  const normalizedCn = normalizeAic(commonName);
  const [validCn, cnError] = validateAicFormat(normalizedCn ?? "");
  if (!validCn || normalizedCn === null) {
    throw new InvalidPeerCertificateError(`invalid peer certificate CN: ${cnError}`);
  }

  let normalizedSan: string | null = null;
  if (sanAic !== null) {
    normalizedSan = normalizeAic(sanAic);
    const [validSan, sanError] = validateAicFormat(normalizedSan ?? "");
    if (!validSan) {
      throw new InvalidPeerCertificateError(`invalid peer certificate SAN URI: ${sanError}`);
    }
    if (normalizedSan !== normalizedCn) {
      throw new InvalidPeerCertificateError("peer certificate CN and SAN URI AIC do not match");
    }
  }

  return Object.freeze({ aic: normalizedCn, commonName, sanAic: normalizedSan });
}

type SenderSource = string | { senderId?: unknown } | null | undefined;

function extractSenderId(messageOrSender: SenderSource): string | null {
  if (typeof messageOrSender === "string") {
    return messageOrSender;
  }
  if (messageOrSender && typeof messageOrSender === "object") {
    const sender = messageOrSender.senderId;
    return sender === null || sender === undefined ? null : String(sender);
  }
  return null;
}

/** Assert that a payload senderId matches the authenticated peer AIC. */
export function assertSenderMatchesPeer(
  messageOrSender: SenderSource,
  peerAic: string | null | undefined,
): string {
  const normalizedPeer = normalizeAic(peerAic);
  if (normalizedPeer === null) {
    throw new PeerAicMissingError("peer AIC is required for identity binding");
  }
  const senderId = normalizeAndValidateAic(extractSenderId(messageOrSender), {
    fieldName: "senderId",
  });
  if (senderId !== normalizedPeer) {
    throw new SenderIdentityMismatchError(
      `senderId ${senderId} does not match peer AIC ${normalizedPeer}`,
    );
  }
  return senderId;
}

/** Assert that a payload senderId matches a configured expected/local AIC. */
export function assertSenderMatchesExpected(
  messageOrSender: SenderSource,
  expectedAic: string | null | undefined,
): string {
  const normalizedExpected = normalizeAndValidateExpectedAic(expectedAic, {
    fieldName: "expected_aic",
  });
  const senderId = normalizeAndValidateAic(extractSenderId(messageOrSender), {
    fieldName: "senderId",
  });
  if (senderId !== normalizedExpected) {
    throw new SenderIdentityMismatchError(
      `senderId ${senderId} does not match expected AIC ${normalizedExpected}`,
    );
  }
  return senderId;
}

/** Assert that an actual AIC matches a configured expected AIC. */
export function assertAicMatchesExpected(
  actualAic: string | null | undefined,
  expectedAic: string | null | undefined,
  {
    actualLabel = "peer AIC",
    expectedLabel = "expected_aic",
  }: { actualLabel?: string; expectedLabel?: string } = {},
): string {
  const normalizedExpected = normalizeAndValidateExpectedAic(expectedAic, {
    fieldName: expectedLabel,
  });
  const normalizedActual = normalizeAic(actualAic);
  if (normalizedActual === null) {
    throw new PeerAicMissingError(`${actualLabel} is required for identity binding`);
  }
  const [validActual, actualError] = validateAicFormat(normalizedActual);
  if (!validActual) {
    throw new InvalidPeerCertificateError(`${actualLabel} is invalid: ${actualError}`);
  }
  if (normalizedActual !== normalizedExpected) {
    throw new SenderIdentityMismatchError(
      `${actualLabel} ${normalizedActual} does not match ${expectedLabel} ${normalizedExpected}`,
    );
  }
  return normalizedActual;
}

function isTlsSocket(socket: unknown): socket is TLSSocket {
  return !!socket && typeof (socket as Partial<TLSSocket>).getPeerCertificate === "function";
}

/**
 * 返回已通过 TLS 证书链校验的对端证书。
 *
 * Node 的 `getPeerCertificate()` 即使在证书未通过校验时（例如服务端设置了 `rejectUnauthorized: false`）
 * 也会返回证书内容；Python 的 `ssl.getpeercert()` 对未校验的证书返回空。这里以 `socket.authorized`
 * 为准，保证身份绑定只信任 CA 签发的证书：
 * - 非 TLS socket 或对端未提供证书：返回 null；
 * - 提供了证书但未通过校验：抛 InvalidPeerCertificateError。
 */
export function getVerifiedPeerCertificate(socket: unknown): PeerCertificateLike | null {
  if (!isTlsSocket(socket)) {
    return null;
  }
  const certificate = socket.getPeerCertificate();
  if (!certificate || Object.keys(certificate).length === 0) {
    return null;
  }
  if (socket.authorized !== true) {
    const reason = socket.authorizationError ? String(socket.authorizationError) : "unknown";
    throw new InvalidPeerCertificateError(`peer certificate is not trusted: ${reason}`);
  }
  return certificate;
}

/** Extract the normalized TLS peer AIC from a TLS socket. Non-TLS sockets yield null. */
export function extractPeerAicFromSocket(socket: unknown): string | null {
  const identity = extractPeerIdentity(getVerifiedPeerCertificate(socket));
  return identity ? identity.aic : null;
}

/**
 * Extract the normalized TLS peer AIC from an HTTP client response
 * （对应 Python `extract_peer_aic_from_httpx_response`）。
 */
export function extractPeerAicFromResponse(
  response: Pick<IncomingMessage, "socket">,
): string | null {
  return extractPeerAicFromSocket(response.socket);
}

/** Map identity binding errors into stable JSON-RPC error objects. */
export function identityErrorToJsonRpc(error: AipIdentityError): JSONRPCError {
  if (error instanceof PeerAicMissingError || error instanceof InvalidPeerCertificateError) {
    return {
      code: AUTHENTICATION_REQUIRED_CODE,
      message: "AuthenticationRequiredError",
      data: error.message,
    };
  }
  return {
    code: AUTHORIZATION_FAILED_CODE,
    message: "AuthorizationFailedError",
    data: error.message,
  };
}
