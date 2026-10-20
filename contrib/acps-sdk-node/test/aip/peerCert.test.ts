/**
 * 对应 Python `tests/test_aip_peer_cert.py`。
 *
 * Python 版用自定义 H11Protocol 在连接建立时按 (client, server) 地址登记证书，再由 Starlette 中间件写入
 * `request.state`。Node 的 `req.socket` 就是 TLSSocket，所以 `PeerCertificateRegistry` 以 socket 为键惰性读取；
 * “中间件注入”对应 `getRequestPeerCertificateInfo`（以及代理场景的 `setRequestPeerIdentity`）。
 */

import { describe, expect, it } from "vitest";

import {
  InvalidPeerCertificateError,
  type PeerCertificateLike,
  extractPeerIdentity,
} from "../../src/aip/identity.js";
import {
  PeerCertificateRegistry,
  buildPeerCertificateInfo,
  getRequestPeerAic,
  getRequestPeerCertificateInfo,
  registry as globalRegistry,
  setRequestPeerIdentity,
} from "../../src/aip/peerCert.js";
import { OTHER_AIC, VALID_AIC, injectedPeer, nodeCert } from "./_helpers.js";

/** 带计数的假 TLS socket（authorized 对应真实 TLSSocket 的证书链校验结果）。 */
function fakeSocket(cert: PeerCertificateLike, authorized = true) {
  const socket = {
    authorized,
    authorizationError: authorized ? undefined : "UNABLE_TO_VERIFY_LEAF_SIGNATURE",
    calls: 0,
    getPeerCertificate() {
      socket.calls += 1;
      return cert;
    },
  };
  return socket;
}

function fakeRequest(socket: unknown) {
  return { socket } as unknown as Parameters<typeof getRequestPeerAic>[0];
}

describe("test_aip_peer_cert", () => {
  it("test_registry_register_lookup_unregister", () => {
    const registry = new PeerCertificateRegistry();
    const socket = fakeSocket(nodeCert());
    const info = registry.lookup(socket);
    expect(info).not.toBeNull();
    expect(info?.peerAic).toBe(VALID_AIC);

    // 显式登记 / 注销（Node 版以 socket 为键）
    const other = {};
    expect(registry.lookup(other)).toBeNull(); // 未登记的非 TLS socket
    registry.register(other, buildPeerCertificateInfo(nodeCert({ commonName: OTHER_AIC })));
    expect(registry.lookup(other)?.peerAic).toBe(OTHER_AIC);
    registry.unregister(other);
    expect(registry.lookup(other)).toBeNull();
    registry.unregister(socket);
    expect(registry.lookup(socket)?.peerAic).toBe(VALID_AIC); // 注销后重新从 socket 读取
    expect(socket.calls).toBe(2);
  });

  it("caches the certificate per socket", () => {
    const registry = new PeerCertificateRegistry();
    const socket = fakeSocket(nodeCert());
    registry.lookup(socket);
    registry.lookup(socket);
    expect(socket.calls).toBe(1);
  });

  it("test_registry_stores_peer_identity", () => {
    const registry = new PeerCertificateRegistry();
    const info = registry.lookup(fakeSocket(nodeCert({ sanAic: VALID_AIC })));
    expect(info).not.toBeNull();
    expect(info?.peerIdentity).toEqual(extractPeerIdentity(nodeCert({ sanAic: VALID_AIC })));
    expect(info?.peerAic).toBe(VALID_AIC);
  });

  it("test_registry_preserves_identity_error", () => {
    const registry = new PeerCertificateRegistry();
    const info = registry.lookup(
      fakeSocket(nodeCert({ commonName: VALID_AIC, sanAic: OTHER_AIC })),
    );
    expect(info).not.toBeNull();
    expect(info?.identityError).toBeInstanceOf(InvalidPeerCertificateError);
    expect(info?.peerIdentity).toBeNull();
    expect(info?.commonName).toBe(VALID_AIC);
  });

  it("test_middleware_injects_peer_cert_state", () => {
    const cert = nodeCert({ sanAic: VALID_AIC });
    const request = fakeRequest(fakeSocket(cert));
    const info = getRequestPeerCertificateInfo(request);
    expect(info?.certificate).toEqual(cert);
    expect(info?.commonName).toBe(VALID_AIC);
    expect(info?.peerIdentity).not.toBeNull();
    expect(info?.peerAic).toBe(VALID_AIC);
    expect(info?.identityError).toBeNull();
    expect(getRequestPeerAic(request)).toBe(VALID_AIC);
    expect(globalRegistry.lookup(request.socket)).toBe(info);
  });

  it("injected identity takes precedence over the socket (reverse-proxy adapter)", () => {
    const request = fakeRequest(fakeSocket(nodeCert({ commonName: OTHER_AIC })));
    setRequestPeerIdentity(request, injectedPeer(VALID_AIC));
    expect(getRequestPeerAic(request)).toBe(VALID_AIC);
  });

  it("test_get_request_peer_aic_raises_identity_error", () => {
    const request = fakeRequest(fakeSocket(nodeCert({ commonName: VALID_AIC, sanAic: OTHER_AIC })));
    expect(() => getRequestPeerAic(request)).toThrow(InvalidPeerCertificateError);
  });

  it("test_get_request_peer_aic_returns_none_without_state", () => {
    expect(getRequestPeerAic(fakeRequest({}))).toBeNull(); // 明文连接
    expect(getRequestPeerAic(fakeRequest(fakeSocket({})))).toBeNull(); // TLS 但未出示证书
  });

  it("test_registry_rejects_unverified_certificate", () => {
    const registry = new PeerCertificateRegistry();
    const info = registry.lookup(fakeSocket(nodeCert({ commonName: VALID_AIC }), false));
    expect(info?.peerAic).toBeNull();
    expect(info?.identityError?.name).toBe("InvalidPeerCertificateError");
    expect(() =>
      getRequestPeerAic(fakeRequest(fakeSocket(nodeCert({ commonName: VALID_AIC }), false))),
    ).toThrow("peer certificate is not trusted: UNABLE_TO_VERIFY_LEAF_SIGNATURE");
  });
});
