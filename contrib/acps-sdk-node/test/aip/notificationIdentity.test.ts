/**
 * 对应 Python `tests/test_aip_notification_identity.py`。
 *
 * Python 用 FastAPI 中间件从 `x-peer-aic` 头写入 `request.state.peer_aic`，经 ASGITransport 调用路由；
 * Node 在 127.0.0.1 上起明文 HTTP 服务挂 `createAipNotificationRequestListener` / 接收器监听函数，
 * 由测试服务器把 `x-peer-aic` 通过 `setRequestPeerIdentity` 注入（见 `_helpers.ts`）。
 */

import { afterEach, describe, expect, it } from "vitest";

import { type TaskResult, TaskState } from "../../src/aip/baseModel.js";
import {
  AUTHENTICATION_REQUIRED_CODE,
  AUTHORIZATION_FAILED_CODE,
  SenderIdentityMismatchError,
} from "../../src/aip/identity.js";
import { AipNotificationClient, NotificationReceiver } from "../../src/aip/notificationClient.js";
import { NOTIFICATION_TOKEN_HEADER } from "../../src/aip/notificationModel.js";
import {
  NotificationService,
  createAipNotificationRequestListener,
} from "../../src/aip/notificationServer.js";
import {
  LEADER_AIC,
  OTHER_AIC,
  PARTNER_AIC,
  type TestHttpServer,
  captureLogs,
  certFor,
  flushAsync,
  memoryTransport,
  startHttpServer,
} from "./_helpers.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- 测试里按路径读取任意 JSON 响应
type Json = any;

const NOW = "2026-06-24T00:00:00Z";

function fixedStatusTransport(status = 200) {
  return memoryTransport(() => ({ status, rawBody: "{}" }));
}

function notifTransport(responseBody: Record<string, unknown>, certAic: string | null) {
  return memoryTransport((req) => ({
    body: { ...responseBody, id: (req.json as { id?: unknown }).id },
    peerCertificate: certAic === null ? null : certFor(certAic),
  }));
}

function taskResult(senderId: string = PARTNER_AIC): TaskResult {
  return {
    type: "task-result",
    id: "tr-1",
    sentAt: NOW,
    senderRole: "partner",
    senderId,
    taskId: "task-1",
    status: { state: TaskState.Completed, stateChangedAt: NOW },
  };
}

const servers: TestHttpServer[] = [];

async function serve(listener: Parameters<typeof startHttpServer>[0]): Promise<string> {
  const server = await startHttpServer(listener);
  servers.push(server);
  return server.url;
}

afterEach(async () => {
  await Promise.all(servers.splice(0).map((server) => server.close()));
});

async function post(url: string, body: unknown, headers: Record<string, string> = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
  return { status: response.status, json: (await response.json()) as Record<string, Json> };
}

const setBody = {
  jsonrpc: "2.0",
  method: "notification/set",
  id: "1",
  params: { url: "http://cb", token: "tok", taskId: "task-1" },
};

describe("test_aip_notification_identity", () => {
  it("test_notification_set_requires_peer_aic_when_binding_enabled", async () => {
    const service = new NotificationService({
      localAic: PARTNER_AIC,
      transport: fixedStatusTransport(),
      identityBindingEnabled: true,
    });
    const base = await serve(createAipNotificationRequestListener(service));
    const resp = await post(`${base}/notification/set`, setBody);
    expect(resp.status).toBe(401);
    expect(resp.json["detail"]["code"]).toBe(AUTHENTICATION_REQUIRED_CODE);
  });

  it("test_notification_configs_are_owner_scoped", async () => {
    const service = new NotificationService({
      localAic: PARTNER_AIC,
      transport: fixedStatusTransport(),
      identityBindingEnabled: true,
    });
    const base = await serve(createAipNotificationRequestListener(service));
    const setResp = await post(`${base}/notification/set`, setBody, { "x-peer-aic": LEADER_AIC });
    const configId = setResp.json["result"]["id"] as string;
    const getResp = await post(
      `${base}/notification/get`,
      {
        jsonrpc: "2.0",
        method: "notification/get",
        id: "2",
        params: { taskId: "task-1", notificationConfigId: configId },
      },
      { "x-peer-aic": OTHER_AIC },
    );
    expect(getResp.status).toBe(200);
    expect(getResp.json["result"]).toEqual([]);
  });

  it("test_notification_start_requires_sender_match_peer", async () => {
    const service = new NotificationService({
      localAic: PARTNER_AIC,
      transport: fixedStatusTransport(),
      identityBindingEnabled: true,
    });
    const base = await serve(createAipNotificationRequestListener(service));
    const setResp = await post(`${base}/notification/set`, setBody, { "x-peer-aic": LEADER_AIC });
    const configId = setResp.json["result"]["id"] as string;
    const startResp = await post(
      `${base}/notification/start`,
      {
        jsonrpc: "2.0",
        method: "notification/start",
        id: "2",
        params: {
          message: {
            type: "task-command",
            id: "cmd-1",
            sentAt: NOW,
            senderRole: "leader",
            senderId: OTHER_AIC,
            command: "start",
            taskId: "task-1",
            commandParams: { notificationConfigId: configId },
          },
        },
      },
      { "x-peer-aic": LEADER_AIC },
    );
    expect(startResp.status).toBe(403);
    expect(startResp.json["detail"]["code"]).toBe(AUTHORIZATION_FAILED_CODE);
  });

  it("test_notification_dispatch_rejects_outbound_sender_mismatch", async () => {
    const transport = fixedStatusTransport();
    const service = new NotificationService({
      localAic: PARTNER_AIC,
      transport,
      identityBindingEnabled: true,
    });
    service.store.set(
      { id: "cfg-1", url: "http://cb", token: "tok", taskId: "task-1" },
      { ownerAic: LEADER_AIC },
    );
    service.registry.add({ taskId: "task-1", configId: "cfg-1", ownerAic: LEADER_AIC });
    await expect(service.dispatch(taskResult(OTHER_AIC))).rejects.toBeInstanceOf(
      SenderIdentityMismatchError,
    );
    expect(transport.requests).toHaveLength(0);
  });

  it("test_notification_client_validates_tls_server_identity", async () => {
    const client = new AipNotificationClient("https://partner.local", LEADER_AIC, {
      expectedPartnerAic: PARTNER_AIC,
      identityBindingEnabled: true,
      transport: notifTransport(
        {
          jsonrpc: "2.0",
          result: { id: "cfg-1", url: "http://cb", token: "tok", taskId: "task-1" },
        },
        PARTNER_AIC,
      ),
    });
    const result = await client.setNotification("task-1", "http://cb", "tok");
    expect(result.id).toBe("cfg-1");
    await client.close();
  });

  it("test_notification_client_rejects_tls_server_mismatch", async () => {
    const client = new AipNotificationClient("https://partner.local", LEADER_AIC, {
      expectedPartnerAic: PARTNER_AIC,
      identityBindingEnabled: true,
      transport: notifTransport({ jsonrpc: "2.0", result: true }, OTHER_AIC),
    });
    await expect(client.deleteNotification("task-1", "cfg-1")).rejects.toBeInstanceOf(
      SenderIdentityMismatchError,
    );
    await client.close();
  });

  it("test_notification_receiver_validates_callback_sender_peer", async () => {
    const received: TaskResult[] = [];
    const receiver = new NotificationReceiver("tok", async (result) => void received.push(result), {
      identityBindingEnabled: true,
    });
    const base = await serve(receiver.createRequestListener());
    const resp = await post(`${base}/callback`, JSON.stringify(taskResult()), {
      "x-peer-aic": PARTNER_AIC,
      [NOTIFICATION_TOKEN_HEADER]: "tok",
    });
    expect(resp.status).toBe(200);
    await flushAsync();
    expect(received).toHaveLength(1);
  });

  it("test_notification_receiver_rejects_missing_peer_when_binding_enabled", async () => {
    const receiver = new NotificationReceiver(
      "tok",
      async () => {
        throw new Error("handler should not run");
      },
      { identityBindingEnabled: true },
    );
    const base = await serve(receiver.createRequestListener());
    const resp = await post(`${base}/callback`, JSON.stringify(taskResult()), {
      [NOTIFICATION_TOKEN_HEADER]: "tok",
    });
    expect(resp.status).toBe(401);
    expect(resp.json["detail"]["code"]).toBe(AUTHENTICATION_REQUIRED_CODE);
  });

  it("test_notification_client_logs_warning_when_identity_binding_disabled", () => {
    const logs = captureLogs();
    try {
      new AipNotificationClient("https://partner.local", LEADER_AIC, {
        identityBindingEnabled: false,
        transport: notifTransport({ jsonrpc: "2.0", result: true }, null),
      });
      expect(logs.messages.join("\n").toLowerCase()).toContain(
        "identity binding disabled for notification client",
      );
    } finally {
      logs.restore();
    }
  });

  it("test_notification_receiver_logs_warning_when_identity_binding_disabled", () => {
    const logs = captureLogs();
    try {
      new NotificationReceiver(
        "tok",
        async (result) => {
          throw new Error(`handler should not run: ${result.taskId}`);
        },
        { identityBindingEnabled: false },
      );
      expect(logs.messages.join("\n").toLowerCase()).toContain(
        "identity binding disabled for notification receiver",
      );
    } finally {
      logs.restore();
    }
  });
});
