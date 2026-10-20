/**
 * 对应 Python `tests/integration/test_aip_notification_mtls_identity.py`：
 * 通知回调在真实 mTLS 下使用 Partner 客户端证书身份，接收端校验 senderId 与证书一致。
 */

import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { type TaskResult, TaskState } from "../../src/aip/baseModel.js";
import { NotificationReceiver } from "../../src/aip/notificationClient.js";
import { NOTIFICATION_TOKEN_HEADER } from "../../src/aip/notificationModel.js";
import { NotificationService } from "../../src/aip/notificationServer.js";
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
let partnerServer: TlsIdentity;
let partnerClient: TlsIdentity;
const servers: RunningTlsServer[] = [];

beforeAll(async () => {
  pki = await createTestPki();
  // 与 Python 一致：回调接收端用 partner 的服务端证书，Partner 发回调时用 partner 的客户端证书
  partnerServer = await pki.issue(PARTNER_AIC);
  partnerClient = await pki.issue(PARTNER_AIC);
});

afterEach(async () => {
  await Promise.all(servers.splice(0).map((server) => server.close()));
});

function buildTaskResult(senderId: string = PARTNER_AIC): TaskResult {
  const now = utcNowIso();
  return {
    type: "task-result",
    id: "task-result-1",
    sentAt: now,
    senderRole: "partner",
    senderId,
    taskId: "task-1",
    sessionId: "sess-1",
    status: { state: TaskState.Completed, stateChangedAt: now },
  };
}

async function startReceiver(onResult: (result: TaskResult) => void): Promise<string> {
  const receiver = new NotificationReceiver("token-1", async (result) => onResult(result), {
    identityBindingEnabled: true,
  });
  const server = await runTlsServer(
    route({ "POST /callbacks/*": receiver.createRequestListener() }),
    {
      pki,
      identity: partnerServer,
      requireClientCert: true,
    },
  );
  servers.push(server);
  return server.baseUrl;
}

describe("test_aip_notification_mtls_identity", () => {
  it("test_notification_callback_uses_partner_client_cert_identity", async () => {
    let resolveReceived: (result: TaskResult) => void = () => undefined;
    const received = new Promise<TaskResult>((resolve) => {
      resolveReceived = resolve;
    });
    const baseUrl = await startReceiver((result) => resolveReceived(result));

    const service = new NotificationService({
      localAic: PARTNER_AIC,
      callbackTls: clientTls(pki, partnerClient),
      identityBindingEnabled: true,
      maxRetries: 0,
    });
    service.store.set(
      { id: "cfg-1", taskId: "task-1", url: `${baseUrl}/callbacks/task-1`, token: "token-1" },
      { ownerAic: LEADER_AIC },
    );
    service.registry.add({ taskId: "task-1", configId: "cfg-1", ownerAic: LEADER_AIC });

    await service.dispatch(buildTaskResult());
    const result = await Promise.race([
      received,
      new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error("callback not received")), 5000),
      ),
    ]);
    await service.close();

    expect(result.senderId).toBe(PARTNER_AIC);
    expect(result.taskId).toBe("task-1");
  });

  it("test_notification_callback_rejects_forged_sender_body_under_real_mtls", async () => {
    const received: TaskResult[] = [];
    const baseUrl = await startReceiver((result) => received.push(result));
    const forged = buildTaskResult(LEADER_AIC);
    const response = await createNodeTransport(clientTls(pki, partnerClient))({
      url: `${baseUrl}/callbacks/task-1`,
      method: "POST",
      headers: { "Content-Type": "application/json", [NOTIFICATION_TOKEN_HEADER]: "token-1" },
      body: JSON.stringify(forged),
      timeoutMs: 10_000,
    });
    expect(response.status).toBe(403);
    expect((JSON.parse(response.body.toString()) as { detail: { code: number } }).detail.code).toBe(
      -32009,
    );
    await new Promise((resolve) => setImmediate(resolve));
    expect(received).toEqual([]);
  });
});
