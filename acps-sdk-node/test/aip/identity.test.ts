/**
 * 对应 Python `tests/test_aip_identity.py`。
 *
 * Python 的证书结构是 `ssl.getpeercert()` 的元组；Node 版读取 `getPeerCertificate()` 的对象
 * （`subject.CN` 与 `subjectaltname` 字符串），见 `_helpers.ts` 的 `nodeCert`。
 * `extract_peer_aic_from_httpx_response` 对应 `extractPeerAicFromResponse`（读取响应 socket 的证书）。
 */

import type { TLSSocket } from "node:tls";

import { describe, expect, it } from "vitest";

import type { TaskCommand } from "../../src/aip/baseModel.js";
import {
  AUTHENTICATION_REQUIRED_CODE,
  AUTHORIZATION_FAILED_CODE,
  InvalidPeerCertificateError,
  PeerAicMissingError,
  type PeerCertificateLike,
  SenderIdentityMismatchError,
  assertAicMatchesExpected,
  assertSenderMatchesExpected,
  assertSenderMatchesPeer,
  extractAcpsUriSan,
  extractCommonName,
  extractPeerAicFromResponse,
  extractPeerAicFromSocket,
  extractPeerIdentity,
  identityErrorToJsonRpc,
  normalizeAic,
  parseSubjectAltName,
} from "../../src/aip/identity.js";
import { OTHER_AIC, VALID_AIC, nodeCert } from "./_helpers.js";

function command(senderId: string = VALID_AIC): TaskCommand {
  return {
    type: "task-command",
    id: "cmd-1",
    sentAt: "2026-06-24T00:00:00Z",
    senderRole: "leader",
    senderId,
    command: "start",
    taskId: "task-1",
    sessionId: "session-1",
  };
}

/** 对应 Python 的 `_FakeNetworkStream`：带 `getPeerCertificate` 的假 TLS socket。 */
function fakeTlsSocket(
  cert: PeerCertificateLike,
  authorized = true,
): Pick<TLSSocket, "getPeerCertificate" | "authorized"> {
  // 真实 TLSSocket 在证书链校验通过时 authorized === true
  return {
    authorized,
    getPeerCertificate: (() => cert) as unknown as TLSSocket["getPeerCertificate"],
  };
}

describe("test_aip_identity", () => {
  it("test_extract_common_name", () => {
    expect(extractCommonName(nodeCert())).toBe(VALID_AIC);
  });

  it("test_extract_acps_uri_san", () => {
    expect(extractAcpsUriSan(nodeCert({ sanAic: VALID_AIC }))).toBe(VALID_AIC);
  });

  it("test_extract_acps_uri_san_rejects_multiple_distinct_acps_values", () => {
    expect(() =>
      extractAcpsUriSan(
        nodeCert({
          sanEntries: [
            ["URI", `acps://${VALID_AIC}`],
            ["URI", `acps://${OTHER_AIC}`],
          ],
        }),
      ),
    ).toThrow(InvalidPeerCertificateError);
  });

  it("test_extract_peer_identity_with_matching_cn_and_san", () => {
    const peer = extractPeerIdentity(
      nodeCert({ commonName: VALID_AIC.toLowerCase(), sanAic: VALID_AIC }),
    );
    expect(peer).not.toBeNull();
    expect(peer?.aic).toBe(VALID_AIC);
    expect(peer?.sanAic).toBe(VALID_AIC);
  });

  it("test_extract_peer_identity_rejects_cn_san_mismatch", () => {
    expect(() =>
      extractPeerIdentity(nodeCert({ commonName: VALID_AIC, sanAic: OTHER_AIC })),
    ).toThrow(InvalidPeerCertificateError);
  });

  it("test_extract_peer_identity_returns_none_when_cn_missing", () => {
    expect(extractPeerIdentity(nodeCert({ commonName: null, sanAic: VALID_AIC }))).toBeNull();
  });

  it("test_extract_peer_identity_rejects_invalid_cn", () => {
    expect(() => extractPeerIdentity(nodeCert({ commonName: "not-aic" }))).toThrow(
      InvalidPeerCertificateError,
    );
  });

  it("test_normalize_aic_strips_whitespace_and_uppercases", () => {
    expect(normalizeAic(`  ${VALID_AIC.toLowerCase()} \n`)).toBe(VALID_AIC);
  });

  it("test_normalize_aic_preserves_internal_whitespace_for_validation", () => {
    const spaced = `${VALID_AIC.slice(0, 10)} ${VALID_AIC.slice(10)}`;
    expect(normalizeAic(spaced)).toBe(spaced);
  });

  it("test_assert_sender_matches_peer_passes", () => {
    expect(assertSenderMatchesPeer(command(), VALID_AIC.toLowerCase())).toBe(VALID_AIC);
  });

  it("test_assert_sender_matches_peer_rejects_missing_peer_aic", () => {
    expect(() => assertSenderMatchesPeer(command(), null)).toThrow(PeerAicMissingError);
  });

  it("test_assert_sender_matches_peer_rejects_missing_sender_id", () => {
    expect(() => assertSenderMatchesPeer({ senderId: null }, VALID_AIC)).toThrow(
      SenderIdentityMismatchError,
    );
  });

  it("test_assert_sender_matches_peer_rejects_invalid_sender_id", () => {
    expect(() => assertSenderMatchesPeer(command("bad-aic"), VALID_AIC)).toThrow(
      SenderIdentityMismatchError,
    );
  });

  it("test_assert_sender_matches_peer_rejects_sender_id_with_internal_whitespace", () => {
    expect(() =>
      assertSenderMatchesPeer(
        command(`${VALID_AIC.slice(0, 10)} ${VALID_AIC.slice(10)}`),
        VALID_AIC,
      ),
    ).toThrow(SenderIdentityMismatchError);
  });

  it("test_assert_sender_matches_peer_rejects_mismatch", () => {
    expect(() => assertSenderMatchesPeer(command(OTHER_AIC), VALID_AIC)).toThrow(
      SenderIdentityMismatchError,
    );
  });

  it("test_assert_sender_matches_expected_passes", () => {
    expect(assertSenderMatchesExpected(command(), VALID_AIC.toLowerCase())).toBe(VALID_AIC);
  });

  it("test_assert_aic_matches_expected_passes", () => {
    expect(assertAicMatchesExpected(VALID_AIC.toLowerCase(), VALID_AIC)).toBe(VALID_AIC);
  });

  it("test_extract_peer_aic_from_httpx_response", () => {
    const response = { socket: fakeTlsSocket(nodeCert({ sanAic: VALID_AIC })) };
    expect(extractPeerAicFromResponse(response as never)).toBe(VALID_AIC);
  });

  it("test_extract_peer_aic_from_httpx_response_without_network_stream", () => {
    expect(extractPeerAicFromResponse({ socket: null } as never)).toBeNull();
  });

  it("test_extract_peer_aic_from_httpx_response_without_ssl_object", () => {
    // 明文 socket 没有 getPeerCertificate
    expect(extractPeerAicFromResponse({ socket: {} } as never)).toBeNull();
    // TLS socket 但对端未出示证书（Node 返回空对象）
    expect(extractPeerAicFromSocket(fakeTlsSocket({}))).toBeNull();
  });

  it("test_identity_error_to_jsonrpc_authentication_mapping", () => {
    const error = identityErrorToJsonRpc(new PeerAicMissingError("missing peer"));
    expect(error.code).toBe(AUTHENTICATION_REQUIRED_CODE);
    expect(error.message).toBe("AuthenticationRequiredError");
  });

  it("test_identity_error_to_jsonrpc_authorization_mapping", () => {
    const error = identityErrorToJsonRpc(new SenderIdentityMismatchError("sender mismatch"));
    expect(error.code).toBe(AUTHORIZATION_FAILED_CODE);
    expect(error.message).toBe("AuthorizationFailedError");
  });
});

describe("Node-specific: parseSubjectAltName", () => {
  it("splits Node's subjectaltname string", () => {
    expect(
      parseSubjectAltName(
        `DNS:partner.local, IP Address:127.0.0.1, URI:acps://${VALID_AIC}, email:a@b.c`,
      ),
    ).toEqual([
      ["DNS", "partner.local"],
      ["IP Address", "127.0.0.1"],
      ["URI", `acps://${VALID_AIC}`],
      ["email", "a@b.c"],
    ]);
  });

  it("unquotes JSON-escaped values that contain separators", () => {
    expect(parseSubjectAltName('URI:"acps://a, b", DNS:x')).toEqual([
      ["URI", "acps://a, b"],
      ["DNS", "x"],
    ]);
    expect(parseSubjectAltName(null)).toEqual([]);
    expect(parseSubjectAltName("")).toEqual([]);
  });

  it("treats an empty certificate object as no certificate", () => {
    expect(extractCommonName({})).toBeNull();
    expect(extractAcpsUriSan({})).toBeNull();
    expect(extractPeerIdentity({})).toBeNull();
    expect(extractPeerIdentity(null)).toBeNull();
  });

  it("uses the first CN when the subject carries several", () => {
    expect(extractCommonName({ subject: { CN: [VALID_AIC, OTHER_AIC] } })).toBe(VALID_AIC);
  });
});
