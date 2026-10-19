/**
 * 读取入站请求的对端证书身份（对应 Python `acps_sdk.aip.aip_peer_cert`）
 *
 * Python 版因为 uvicorn 不向应用暴露 socket，需要自定义 H11Protocol 在连接建立时登记证书，
 * 再由 Starlette 中间件写入 `request.state`。Node 的 `req.socket` 直接就是 `TLSSocket`，
 * 所以这里按 socket 惰性读取并缓存，不需要替换 HTTP 协议实现。
 *
 * 同一 TLS 连接上的证书不会变化，缓存以 socket 为键（WeakMap，连接释放后自动回收）。
 *
 * 如果 TLS 在反向代理终止（不推荐，见 docs/porting-notes.md），可在进入 AIP 处理前调用
 * `setRequestPeerIdentity` 注入代理已验证过的身份，作用等同 Python 版预先写入 `request.state.peer_aic`。
 */

import type { IncomingMessage } from "node:http";

import {
  type AipIdentityError,
  type PeerCertificateLike,
  type PeerIdentity,
  AipIdentityError as AipIdentityErrorClass,
  extractCommonName,
  extractPeerIdentity,
  getVerifiedPeerCertificate,
} from "./identity.js";

/** Peer certificate information attached to an active TLS connection. */
export class PeerCertificateInfo {
  constructor(
    readonly commonName: string | null,
    readonly certificate: PeerCertificateLike | null,
    readonly peerIdentity: PeerIdentity | null,
    readonly identityError: AipIdentityError | null = null,
  ) {
    Object.freeze(this);
  }

  get peerAic(): string | null {
    return this.peerIdentity ? this.peerIdentity.aic : null;
  }
}

/** Build certificate info from a raw peer certificate. */
export function buildPeerCertificateInfo(
  certificate: PeerCertificateLike | null | undefined,
): PeerCertificateInfo {
  const cert = certificate && Object.keys(certificate).length > 0 ? certificate : null;
  const commonName = extractCommonName(cert);
  let identity: PeerIdentity | null = null;
  let identityError: AipIdentityError | null = null;
  try {
    identity = extractPeerIdentity(cert);
  } catch (error) {
    if (error instanceof AipIdentityErrorClass) {
      identityError = error;
    } else {
      throw error;
    }
  }
  return new PeerCertificateInfo(commonName, cert, identity, identityError);
}

interface SocketWithPeerCertificate {
  getPeerCertificate(detailed?: boolean): PeerCertificateLike;
}

function hasPeerCertificate(socket: unknown): socket is SocketWithPeerCertificate {
  return !!socket && typeof (socket as SocketWithPeerCertificate).getPeerCertificate === "function";
}

/** Socket 级证书信息缓存。 */
export class PeerCertificateRegistry {
  private entries = new WeakMap<object, PeerCertificateInfo>();

  /**
   * 读取 socket 上的对端证书信息：优先返回 `register` 登记过的信息，否则从 TLS socket 计算并缓存；
   * 既未登记又不是 TLS socket 时返回 null。
   */
  lookup(socket: unknown): PeerCertificateInfo | null {
    if (socket === null || typeof socket !== "object") {
      return null;
    }
    const cached = this.entries.get(socket);
    if (cached) {
      return cached;
    }
    if (!hasPeerCertificate(socket)) {
      return null;
    }
    let info: PeerCertificateInfo;
    try {
      info = buildPeerCertificateInfo(getVerifiedPeerCertificate(socket));
    } catch (error) {
      if (!(error instanceof AipIdentityErrorClass)) {
        throw error;
      }
      // 证书未通过 TLS 校验：不提取身份，记录错误供身份绑定返回 AuthenticationRequired
      info = new PeerCertificateInfo(null, null, null, error);
    }
    this.entries.set(socket, info);
    return info;
  }

  register(socket: object, info: PeerCertificateInfo): void {
    this.entries.set(socket, info);
  }

  unregister(socket: object): void {
    this.entries.delete(socket);
  }
}

export const registry = new PeerCertificateRegistry();

const injectedIdentity = new WeakMap<object, PeerCertificateInfo>();

/** 由外部（例如已验证 mTLS 的反向代理适配层）为某个请求注入对端身份。 */
export function setRequestPeerIdentity(request: object, info: PeerCertificateInfo): void {
  injectedIdentity.set(request, info);
}

/** 读取请求的对端证书信息：优先使用注入值，否则读取 TLS socket。 */
export function getRequestPeerCertificateInfo(
  request: Pick<IncomingMessage, "socket"> & object,
): PeerCertificateInfo | null {
  return injectedIdentity.get(request) ?? registry.lookup(request.socket);
}

/** Return the peer AIC of a request, or throw the stored identity error. */
export function getRequestPeerAic(
  request: Pick<IncomingMessage, "socket"> & object,
): string | null {
  const info = getRequestPeerCertificateInfo(request);
  if (!info) {
    return null;
  }
  if (info.identityError) {
    throw info.identityError;
  }
  return info.peerAic;
}
