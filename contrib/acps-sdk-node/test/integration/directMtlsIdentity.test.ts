/**
 * 对应 Python `tests/integration/test_aip_direct_mtls_identity.py`：真实 mTLS 下的 Direct RPC 身份绑定。
 */

import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { type TaskResult, TaskState } from "../../src/aip/baseModel.js";
import { AipRpcClient } from "../../src/aip/rpcClient.js";
import { createAipRpcRequestListener } from "../../src/aip/rpcServer.js";
import { createNodeTransport } from "../../src/internal/http.js";
import { utcNowIso } from "../../src/internal/time.js";
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

function buildTaskResult(taskId: string): TaskResult {
  const now = utcNowIso();
  return {
    type: "task-result",
    id: "result-1",
    sentAt: now,
    senderRole: "partner",
    senderId: PARTNER_AIC,
    taskId,
    sessionId: "sess-1",
    status: { state: TaskState.Accepted, stateChangedAt: now },
  };
}

async function startPartner({ requireClientCert = true } = {}): Promise<string> {
  const rpc = createAipRpcRequestListener(
    { onStart: async (command) => buildTaskResult(command.taskId as string) },
    { localAic: PARTNER_AIC, identityBindingEnabled: true },
  );
  const server = await runTlsServer(route({ "POST /rpc": rpc }), {
    pki,
    identity: partner,
    requireClientCert,
  });
  servers.push(server);
  return server.baseUrl;
}

function rawRequestBody(senderId: string, id: string) {
  return {
    jsonrpc: "2.0",
    id,
    method: "rpc",
    params: {
      command: {
        type: "task-command",
        id: "cmd-1",
        sentAt: utcNowIso(),
        senderRole: "leader",
        senderId,
        taskId: "task-1",
        sessionId: "sess-1",
        command: "start",
        dataItems: [{ type: "text", text: "hello" }],
      },
    },
  };
}

describe("test_aip_direct_mtls_identity", () => {
  it("test_direct_rpc_mtls_identity_binding_accepts_matching_sender", async () => {
    const baseUrl = await startPartner();
    const client = new AipRpcClient(`${baseUrl}/rpc`, LEADER_AIC, {
      tls: clientTls(pki, leader),
      expectedPartnerAic: PARTNER_AIC,
    });
    try {
      const result = await client.startTask("sess-1", "hello", "task-1");
      expect(result.senderId).toBe(PARTNER_AIC);
      expect(result.taskId).toBe("task-1");
    } finally {
      await client.close();
    }
  });

  it("test_direct_rpc_mtls_identity_binding_rejects_forged_sender", async () => {
    const baseUrl = await startPartner();
    const transport = createNodeTransport(clientTls(pki, leader));
    const response = await transport({
      url: `${baseUrl}/rpc`,
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(rawRequestBody("1.2.156.3088.1.1.FAKEID.FORGED.1.XXXX", "rpc-1")),
      timeoutMs: 10_000,
    });
    expect(response.status).toBe(200);
    expect((JSON.parse(response.body.toString()) as { error: { code: number } }).error.code).toBe(
      -32009,
    );
  });

  it("test_direct_rpc_identity_binding_rejects_when_peer_certificate_is_unavailable", async () => {
    // Python 版未挂中间件时拿不到证书；Node 直接读 socket，对应场景是服务端不请求客户端证书
    const baseUrl = await startPartner({ requireClientCert: false });
    const transport = createNodeTransport(clientTls(pki, leader));
    const response = await transport({
      url: `${baseUrl}/rpc`,
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(rawRequestBody(LEADER_AIC, "rpc-2")),
      timeoutMs: 10_000,
    });
    expect(response.status).toBe(200);
    expect((JSON.parse(response.body.toString()) as { error: { code: number } }).error.code).toBe(
      -32008,
    );
  });

  it("rejects TLS clients without a certificate at the handshake when mTLS is required", async () => {
    const baseUrl = await startPartner();
    const transport = createNodeTransport(clientTls(pki));
    await expect(
      transport({ url: `${baseUrl}/rpc`, method: "POST", body: "{}", timeoutMs: 10_000 }),
    ).rejects.toThrow();
  });

  it("client rejects a partner whose certificate AIC differs from expectedPartnerAic", async () => {
    const baseUrl = await startPartner();
    const client = new AipRpcClient(`${baseUrl}/rpc`, LEADER_AIC, {
      tls: clientTls(pki, leader),
      expectedPartnerAic: LEADER_AIC,
    });
    await expect(client.startTask("sess-1", "hello", "task-1")).rejects.toThrow(
      `TLS server AIC ${PARTNER_AIC} does not match expected_partner_aic ${LEADER_AIC}`,
    );
  });
});
